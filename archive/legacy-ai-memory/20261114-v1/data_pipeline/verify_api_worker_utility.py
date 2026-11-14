"""Standard-library audit of shared authored worker outcomes and prompt links."""
from __future__ import annotations
import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'training/memory_selector'))
from prepare_api_worker import messages,PINS,SYSTEMS


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def text_sha(s):return hashlib.sha256(s.encode()).hexdigest()
def read(path):return json.loads(path.read_text())
def lines(path):return [json.loads(l) for l in path.read_text().splitlines()]


def equal(a,b):
    if type(a) is not type(b):return False
    if isinstance(a,dict):return a.keys()==b.keys() and all(equal(a[k],b[k]) for k in a)
    if isinstance(a,list):return len(a)==len(b) and all(equal(x,y) for x,y in zip(a,b))
    if isinstance(a,float):return (math.isnan(a) and math.isnan(b)) or abs(a-b)<=1e-8+1e-7*abs(b)
    return a==b


def verify(folder):
    manifest=read(folder/'manifest.json')
    if manifest['format']!='authored-api-worker-utility-v1':raise ValueError('Unknown worker data format')
    for name,value in manifest['files_sha256'].items():
        if Path(name).is_absolute() or '..' in Path(name).parts or sha(folder/name)!=value:raise ValueError('Worker artifact changed')
    ref=read(folder/'data-reference.json');data=ROOT/ref['source']
    if sha(data/'artifact-index.json')!=ref['artifact_index_sha256']:raise ValueError('Underlying API data index changed')
    rows={};by_state=defaultdict(list)
    for split,value in ref['splits_sha256'].items():
        path=data/(split+'.jsonl')
        if sha(path)!=value:raise ValueError('Underlying API split changed')
        for row in lines(path):rows[row['sample_id']]=row;by_state[row['state_id']].append(row)
    protocol=read(folder/'protocol.json');provenance=read(folder/'provenance.json');config=provenance['config']
    if PINS.get(config['model_id'])!=config['revision'] or protocol['pilot'] or protocol['protocol_version']!=3 or protocol['outcomes_included']:
        raise ValueError('Worker contract changed')
    if protocol['system_sha256']!=text_sha(SYSTEMS[3]):raise ValueError('Worker system prompt changed')
    for name,key in [('api_worker_judge.py','judge_source_sha256'),('api_worker_interpreter.py','interpreter_source_sha256')]:
        if sha(folder/'reproduction'/name)!=protocol[key]:raise ValueError('Judge differs from pre-generation contract')
    for summary in provenance['judgment_summaries'].values():
        if summary['files_sha256']['serialization_wrapper']!=sha(folder/'reproduction/api_worker_judge_serialized.py'):
            raise ValueError('Post-judgment serialization wrapper changed')
    jobs=lines(folder/'jobs.jsonl');outputs=lines(folder/'model-outputs.jsonl');judgments=lines(folder/'judgments.jsonl')
    job_map={r['job_id']:r for r in jobs};output_map={r['job_id']:r for r in outputs};judgment_map={r['job_id']:r for r in judgments}
    if any(len(records)!=len(lookup) for records,lookup in [(jobs,job_map),(outputs,output_map),(judgments,judgment_map)]) or set(job_map)!=set(output_map) or set(job_map)!=set(judgment_map):
        raise ValueError('Missing/duplicate worker prompt, output or judgment')
    base=defaultdict(set);repeats=0
    expected={}
    for execution in read(data/'evidence/executions.json'):
        key=execution['case_id'],execution['fixture_id'];values={k:execution[k] for k in ['before','expected']}
        if key in expected and expected[key]!=values:raise ValueError('Reference fixture mismatch')
        expected[key]=values
    execution_count=0;unsupported=0;truncated=0
    for key,job in job_map.items():
        rs=by_state[job['state_id']];rlookup={r['recipe']:r for r in rs};action=job['action']
        if action=='none':memory=[]
        elif action=='all_full':memory=[r['input']['candidate_text'] for r in sorted(rs,key=lambda r:text_sha(job['state_id']+'|'+r['recipe']))]
        else:
            recipe,view=action.rsplit('_',1)
            if view not in ['full','procedure']:raise ValueError('Invalid memory view')
            memory=[rlookup[recipe]['input']['candidate_text' if view=='full' else 'procedure_text']]
        if job['messages']!=messages(rs[0]['input']['state_text'],memory,3) or job['messages_sha256']!=text_sha(json.dumps(job['messages'],ensure_ascii=False,sort_keys=True)):
            raise ValueError('Prompt differs from predecision input/candidate grid')
        if job['split']!=rs[0]['split']:raise ValueError('Worker/source split differs')
        output=output_map[key];judgment=judgment_map[key]
        for field in ['state_id','action','split']:
            if output[field]!=job[field] or judgment[field]!=job[field]:raise ValueError('Outcome metadata misaligned')
        if output['messages_sha256']!=job['messages_sha256'] or judgment['code_sha256']!=text_sha(output['text']):raise ValueError('Generated code/prompt hash differs')
        for field in ['input_tokens','output_tokens','seconds']:
            if output[field]!=judgment[field]:raise ValueError('Worker cost changed')
        if output['output_tokens']>config['max_new_tokens'] or output['input_tokens']>config['max_input_tokens']:raise ValueError('Token budget exceeded')
        traces=judgment['executions'];wanted={(w,f) for w in rs[0]['world_ids'] for f in [0,1]}
        if {(e['case_id'],e['fixture_id']) for e in traces}!=wanted or len(traces)!=len(wanted):raise ValueError('Missing/duplicate fixture/world execution')
        for e in traces:
            ref_values=expected[e['case_id'],e['fixture_id']]
            if any(not equal(e[k],ref_values[k]) for k in ['before','expected']) or e['goal']!=rs[0]['goal']:raise ValueError('Execution fixture/goal changed')
            output_pass=e['exception'] is None and equal(e['output'],e['expected'])
            effect_pass=equal(e['after'],e['before'] if e['goal']=='preserve' else e['expected'])
            passed=output_pass and effect_pass and e['operation_calls']==1
            if (e['output_pass'],e['effect_pass'],e['passed'])!=(output_pass,effect_pass,passed):raise ValueError('Saved execution pass arithmetic differs')
            if e['unsupported']!=bool(e['exception'] and e['exception']['type']=='UnsupportedCode'):raise ValueError('Unsupported category differs')
            execution_count+=1
        if judgment['all_passed']!=all(e['passed'] for e in traces) or judgment['pass_fraction']!=sum(e['passed'] for e in traces)/len(traces) or judgment['unsupported']!=any(e['unsupported'] for e in traces):
            raise ValueError('Aggregated worker success differs')
        if job['repeat_of']:
            repeats+=1
            if job['messages']!=job_map[job['repeat_of']]['messages'] or output['text']!=output_map[job['repeat_of']]['text']:raise ValueError('Exact repeat changed')
        else:
            if action in base[job['state_id']]:raise ValueError('Duplicate base action')
            base[job['state_id']].add(action);unsupported+=judgment['unsupported'];truncated+=output['hit_token_limit']
    expected_actions={'none','all_full'}|{r+'_'+v for r in ['returned','receiver','copied_returned','copied_receiver'] for v in ['full','procedure']}
    splits=protocol.get('included_splits',['train','dev','test'])
    if splits not in [['train','dev','test'],['test']]:raise ValueError('Undeclared shared worker split')
    expected_states={state for state,rs in by_state.items() if rs[0]['split'] in splits}
    expected_count=96 if splits==['train','dev','test'] else 28
    if (len(expected_states)!=expected_count or set(base)!=expected_states or any(v!=expected_actions for v in base.values()) or
        repeats!=4 or len(outputs)!=expected_count*10+4 or protocol['states']!=expected_count or
        manifest['state_count']!=expected_count or manifest['base_generations']!=expected_count*10 or manifest['repeat_generations']!=4):
        raise ValueError('Worker cohort changed')
    if splits==['test']:
        freeze=read(folder/'transfer-policy-freeze.json');fence=read(folder/'transfer-before-judging.json')
        if freeze['status']!='frozen_before_transfer_generation' or fence['frozen_policy_sha256']!=sha(folder/'transfer-policy-freeze.json'):
            raise ValueError('Transfer policy was not frozen before judging')
        if freeze['source_sha256']['transfer_jobs']!=sha(folder/'jobs.jsonl') or freeze['source_sha256']['transfer_protocol']!=sha(folder/'protocol.json'):
            raise ValueError('Transfer prompts changed')
        for name in ['policy-selection.json','relation-selection.json','semantic-selection.json','development-policy-choice.json']:
            if freeze['source_sha256'][name]!=sha(folder/name):raise ValueError('Original policy choice changed before transfer')
        for key,name in [('judge','api_worker_judge.py'),('interpreter','api_worker_interpreter.py'),('serialization_wrapper','api_worker_judge_serialized.py')]:
            if freeze['source_sha256'][key]!=sha(folder/'reproduction'/name) or fence['source_sha256'][key]!=freeze['source_sha256'][key]:
                raise ValueError('Transfer grader changed after freeze')
    else:
        fence=read(folder/'selection-before-judging.json')
        if fence['utility_selection_sha256']!=sha(folder/'policy-selection.json') or fence['relation_selection_sha256']!=sha(folder/'relation-selection.json'):raise ValueError('Selection/test sequencing record differs')
        if 'semantic_selection_sha256' in fence and fence['semantic_selection_sha256']!=sha(folder/'semantic-selection.json'):
            raise ValueError('Semantic selection/test sequencing record differs')
        if 'development_policy_choice_sha256' in fence and fence['development_policy_choice_sha256']!=sha(folder/'development-policy-choice.json'):
            raise ValueError('Development policy choice changed after test judging')
    return {'status':'passed','states':len(base),'base_generations':expected_count*10,'exact_repeats':repeats,
        'execution_records_recomputed':execution_count,'unsupported_base_generations':unsupported,'truncated_base_generations':truncated,
        'requires_gpu_or_weights':False,'model_generation_repeated':False,'generated_code_executed':False,
        'scope':'Prompt/provenance and recorded value/effect arithmetic, not fresh model inference or interpreter execution.'}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('data',type=Path);print(json.dumps(verify(p.parse_args().data),indent=2))
