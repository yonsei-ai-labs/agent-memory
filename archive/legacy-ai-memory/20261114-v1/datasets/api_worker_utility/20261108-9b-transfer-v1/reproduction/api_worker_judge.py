"""Execute bounded interpreted worker wrappers against frozen API fixtures."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
from pathlib import Path
import time
import warnings

from data_pipeline import api_transfer_runtime as runtime
from data_pipeline.api_worker_interpreter import Interpreter,UnsupportedCode,parse


def execute(spec,code,goal,fixture_id):
    modules={k:getattr(runtime,k) for k in ['np','pd','Image','sp','nx','copy']}
    namespace=dict(modules)|{k:getattr(runtime,k) for k in ['StandardScaler','Normalizer']}
    # Only frozen, authored fixtures enter Python execution. Model code never does.
    exec(spec['setup'],namespace);data=namespace['data']
    before=runtime.serialize(data);expected=runtime.serialize(eval(spec['expected'],namespace))
    exec('def underlying(data):\n    '+spec['operation'],namespace)
    interpreter=Interpreter(modules,data,namespace['underlying'])
    start=time.perf_counter();error=None;output=None;unsupported=False
    with warnings.catch_warnings(record=True) as observed:
        warnings.simplefilter('always')
        try:output=runtime.serialize(interpreter.run(code))
        except Exception as e:
            error={'type':type(e).__name__,'message':str(e)};unsupported=isinstance(e,UnsupportedCode)
    after=runtime.serialize(data);output_pass=error is None and runtime.equal(output,expected)
    effect_pass=runtime.equal(after,before) if goal=='preserve' else runtime.equal(after,expected)
    return {'case_id':spec['case_id'],'fixture_id':fixture_id,'goal':goal,'before':before,'expected':expected,
        'output':output,'after':after,'exception':error,'unsupported':unsupported,'operation_calls':interpreter.calls,
        'output_pass':output_pass,'effect_pass':effect_pass,'passed':output_pass and effect_pass and interpreter.calls==1,
        'elapsed_seconds':time.perf_counter()-start,'warnings':[str(w.message) for w in observed]}


def verify_interpreter(data):
    environment=json.loads((data/'evidence/environment.json').read_text())
    if hashlib.sha256(Path(runtime.__file__).read_bytes()).hexdigest()!=environment['runtime_code_sha256']:
        raise ValueError('Fixture runtime differs from frozen producer')
    for name,version in environment['packages'].items():
        if runtime.importlib.metadata.version(name)!=version:raise ValueError('Package differs: '+name)
    fixtures=[{r['case_id']:r for r in json.loads((data/'evidence'/n).read_text())} for n in ['catalog.json','confirmation-fixtures.json']]
    originals=json.loads((data/'evidence/executions.json').read_text());started=time.monotonic()
    fields=['before','expected','output','after','exception','operation_calls','output_pass','effect_pass','passed','warnings']
    for record in originals:
        code='def solve(data, operation):\n    '+runtime.RECIPES[record['recipe']].replace('\n','\n    ')
        actual=execute(fixtures[record['fixture_id']][record['case_id']],code,record['goal'],record['fixture_id'])
        for field in fields:
            if actual[field]!=record[field]:raise ValueError('Interpreter differs: '+record['case_id']+' '+record['recipe']+' '+field+' '+str(actual.get('exception')))
    adaptive='def solve(data, operation):\n    work = copy.deepcopy(data)\n    result = operation(work)\n    return work if result is None else result'
    control=[]
    for fixture_id,lookup in enumerate(fixtures):
        for spec in lookup.values():control.append(execute(spec,adaptive,'preserve',fixture_id))
    if not all(r['passed'] for r in control):raise ValueError('Adaptive preserve baseline failed')
    rejection={}
    for name,code in {'import':'import os\ndef solve(data, operation):\n    return data',
                     'private':'def solve(data, operation):\n    return data.__class__',
                     'exec':'def solve(data, operation):\n    return exec("pass")',
                     'loop':'def solve(data, operation):\n    while True:\n        pass'}.items():
        result=execute(next(iter(fixtures[0].values())),code,'preserve',0)
        rejection[name]=result['unsupported']
    if not all(rejection.values()):raise ValueError('Interpreter rejection check failed')
    return {'status':'passed','original_execution_records_reproduced':len(originals),'adaptive_preserve_control_passed':len(control),
        'unsupported_program_checks':rejection,'seconds':time.monotonic()-started,'gpu_used':False,'generated_code_exec_used':False,
        'interpreter_sha256':hashlib.sha256(Path(__file__).with_name('api_worker_interpreter.py').read_bytes()).hexdigest()}


def judge(data,predictions,output,allowed_splits):
    if output.exists():raise FileExistsError(output)
    all_rows=[json.loads(l) for split in allowed_splits for l in (data/(split+'.jsonl')).read_text().splitlines()]
    states={r['state_id']:r for r in all_rows}
    fixtures=[{r['case_id']:r for r in json.loads((data/'evidence'/n).read_text())} for n in ['catalog.json','confirmation-fixtures.json']]
    predictions_path=predictions
    predictions=[json.loads(l) for l in predictions_path.read_text().splitlines()];records=[]
    for prediction in predictions:
        state=states.get(prediction['state_id'])
        if state is None:continue # Explicit train/dev-only judging does not read test outcomes.
        executions=[]
        for world in state['world_ids']:
            for fixture_id,lookup in enumerate(fixtures):executions.append(execute(lookup[world],prediction['text'],state['goal'],fixture_id))
        records.append({**{k:prediction[k] for k in ['job_id','state_id','action','split','input_tokens','output_tokens','seconds']},
            'all_passed':all(r['passed'] for r in executions),'pass_fraction':sum(r['passed'] for r in executions)/len(executions),
            'unsupported':any(r['unsupported'] for r in executions),'executions':executions,
            'code_sha256':hashlib.sha256(prediction['text'].encode()).hexdigest()})
    output.mkdir(parents=True)
    with (output/'judgments.jsonl').open('w') as f:
        for record in records:f.write(json.dumps(record,ensure_ascii=False,allow_nan=False)+'\n')
    base=[r for r in records if '-repeat-' not in r['job_id']];by_state={};by_action={}
    for r in base:by_state.setdefault(r['state_id'],[]).append(r);by_action.setdefault(r['action'],[]).append(r)
    if base and (any(len(v)!=10 for v in by_state.values()) or any(len(v)!=len(by_state) for v in by_action.values())):
        raise ValueError('Incomplete counterfactual worker action grid')
    fixed={a:sum(r['all_passed'] for r in rs)/len(rs) for a,rs in by_action.items()}
    oracle=sum(any(r['all_passed'] for r in rs) for rs in by_state.values())/len(by_state) if base else None
    learnable_oracle=sum(any(r['all_passed'] for r in rs if r['action']!='all_full') for rs in by_state.values())/len(by_state) if base else None
    best_learnable_fixed=max((v for a,v in fixed.items() if a!='all_full'),default=None)
    accepted=sum(not r['unsupported'] for r in base)/len(base) if base else None
    report={'status':'complete','states':len(by_state),'base_generations':len(base),'repeat_generations':len(records)-len(base),
        'allowed_splits':allowed_splits,'syntax_supported_fraction':accepted,'strict_success_by_fixed_action':fixed,
        'per_state_action_oracle_success':oracle,'learnable_action_oracle_success':learnable_oracle,
        'best_fixed_learnable_action_success':best_learnable_fixed,
        'expand_gate':bool(base and accepted>=.8 and learnable_oracle>best_learnable_fixed),
        'meaning':'Fixed-worker paired microtasks; not independent SWE outcomes. No selection model fitted here.',
        'files_sha256':{'predictions':hashlib.sha256(predictions_path.read_bytes()).hexdigest(),
                       'judge':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        'gpu_used':False}
    (output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--data',type=Path,required=True);p.add_argument('--verify',action='store_true')
    p.add_argument('--predictions',type=Path);p.add_argument('--output',type=Path);p.add_argument('--split',action='append',default=[])
    a=p.parse_args();report=verify_interpreter(a.data) if a.verify else judge(a.data,a.predictions,a.output,a.split)
    print(json.dumps(report,ensure_ascii=False,indent=2))
