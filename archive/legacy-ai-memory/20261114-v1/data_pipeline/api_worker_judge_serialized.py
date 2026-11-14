"""Use the frozen worker grader, then encode nonfinite snapshots as strict JSON.

The grading function and interpreter are not changed. Bad generated assignments
can create NaN in sparse matrices; that observation must not abort the cohort.
All pass/fail booleans are computed by the original execute() before encoding.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
from data_pipeline import api_worker_judge as original


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def nonfinite_paths(value,path=''):
    if isinstance(value,float) and not math.isfinite(value):return [path]
    if isinstance(value,dict):return [p for k,v in value.items() for p in nonfinite_paths(v,path+'/'+str(k))]
    if isinstance(value,list):return [p for i,v in enumerate(value) for p in nonfinite_paths(v,path+'/'+str(i))]
    return []


def judge(data,predictions_path,output,allowed_splits):
    if output.exists():raise FileExistsError(output)
    states={r['state_id']:r for split in allowed_splits for r in map(json.loads,(data/(split+'.jsonl')).read_text().splitlines())}
    fixtures=[{r['case_id']:r for r in json.loads((data/'evidence'/n).read_text())} for n in ['catalog.json','confirmation-fixtures.json']]
    records=[];encoding_events=[]
    for prediction in map(json.loads,predictions_path.read_text().splitlines()):
        state=states.get(prediction['state_id'])
        if state is None:continue
        executions=[]
        for world in state['world_ids']:
            for fixture_id,lookup in enumerate(fixtures):
                actual=original.execute(lookup[world],prediction['text'],state['goal'],fixture_id)
                paths=nonfinite_paths(actual)
                if paths:encoding_events.append({'job_id':prediction['job_id'],'world':world,'fixture_id':fixture_id,'paths':paths})
                encoded=original.runtime.plain(actual)
                for field in ['passed','output_pass','effect_pass','unsupported','operation_calls']:
                    if encoded[field]!=actual[field]:raise ValueError('Encoding changed an execution judgment')
                executions.append(encoded)
        records.append({**{k:prediction[k] for k in ['job_id','state_id','action','split','input_tokens','output_tokens','seconds']},
            'all_passed':all(r['passed'] for r in executions),'pass_fraction':sum(r['passed'] for r in executions)/len(executions),
            'unsupported':any(r['unsupported'] for r in executions),'executions':executions,
            'code_sha256':hashlib.sha256(prediction['text'].encode()).hexdigest()})
    base=[r for r in records if '-repeat-' not in r['job_id']];by_state=defaultdict(list);by_action=defaultdict(list)
    for r in base:by_state[r['state_id']].append(r);by_action[r['action']].append(r)
    if base and (any(len(rs)!=10 for rs in by_state.values()) or any(len(rs)!=len(by_state) for rs in by_action.values())):
        raise ValueError('Incomplete counterfactual worker action grid')
    fixed={a:sum(r['all_passed'] for r in rs)/len(rs) for a,rs in by_action.items()}
    oracle=sum(any(r['all_passed'] for r in rs) for rs in by_state.values())/len(by_state) if base else None
    learnable=sum(any(r['all_passed'] for r in rs if r['action']!='all_full') for rs in by_state.values())/len(by_state) if base else None
    best_fixed=max((v for a,v in fixed.items() if a!='all_full'),default=None)
    supported=sum(not r['unsupported'] for r in base)/len(base) if base else None
    summary={'status':'complete','states':len(by_state),'base_generations':len(base),'repeat_generations':len(records)-len(base),
        'allowed_splits':allowed_splits,'syntax_supported_fraction':supported,'strict_success_by_fixed_action':fixed,
        'per_state_action_oracle_success':oracle,'learnable_action_oracle_success':learnable,
        'best_fixed_learnable_action_success':best_fixed,'expand_gate':bool(base and supported>=.8 and learnable>best_fixed),
        'meaning':'Fixed-worker paired microtasks; not independent SWE outcomes. No selection model fitted here.',
        'files_sha256':{'predictions':sha(predictions_path),'judge':sha(Path(original.__file__)),
                       'serialization_wrapper':sha(Path(__file__))},'gpu_used':False,
        'serialization':'Original execution judgments unchanged; nonfinite snapshot floats encoded by runtime.plain after grading.',
        'nonfinite_snapshot_executions':len(encoding_events),'nonfinite_snapshot_values':sum(len(r['paths']) for r in encoding_events)}
    output.mkdir(parents=True)
    with (output/'judgments.jsonl').open('w') as stream:
        for record in records:stream.write(json.dumps(record,ensure_ascii=False,allow_nan=False)+'\n')
    for name,value in [('summary.json',summary),('serialization-events.json',encoding_events)]:
        (output/name).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser()
    for name in ['data','predictions','output']:p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--split',action='append',required=True);a=p.parse_args()
    print(json.dumps(judge(a.data,a.predictions,a.output,a.split),indent=2))
