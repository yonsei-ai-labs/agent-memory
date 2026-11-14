"""Freeze answer-free paired memory prompts for a fixed code-generating worker."""
from __future__ import annotations
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import random
import sys

HERE=Path(__file__).resolve().parent;sys.path.insert(0,str(HERE.parents[1]))
FILES=['jobs.jsonl','protocol.json']
PINS={'Qwen/Qwen3.5-4B':'851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a',
      'Qwen/Qwen3.5-9B':'c202236235762e1c871ad0ccb60c8ee5ba337b9a'}
PILOT=['numpy-replace-nan-false-preserve','numpy-sort-return-mutate','pillow-resize-mutate','pillow-thumbnail-preserve',
       'scipy-multiply-mutate','scipy-sort-indices-preserve','stdlib-sorted-mutate','stdlib-dict-update-preserve']
SYSTEM='''Write a short Python function solve(data, operation) for the current task. Call the supplied operation exactly once; do not redefine it. Return the transformed value and satisfy the requirement about the original input object. Code must work for other inputs of the same kind, not just the displayed values.
You may adapt a past procedure, use its general idea, or ignore it. A past successful experience can have different conditions. Judge the current requirement and current implementation.
Return only Python code. Use assignments, if/else, return, ordinary indexing or slice assignment, and public copy/update/container methods. No loops, comprehensions, try/except, helper functions, file/network access, private attributes, or dynamic execution. copy, np, pd, sp, nx and Image refer to the usual already available modules. You may import copy or numpy if useful. The operation and fitted state are supplied; do not refit anything.'''
SYSTEMS={1:SYSTEM,2:SYSTEM.replace('Return only Python code.',
    'Return concise Python code only: no comments, explanations, or Markdown fences. Use as few statements as practical.'),
    3:SYSTEM.replace('Return only Python code.',
    'Return only the complete Python function definition, starting with def solve(data, operation): and its indented body. Do not omit the function header. Do not use a lambda. Do not write comments or explanations. The copy, np, pd, sp, nx and Image names are already available; imports are unnecessary.')}


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def text_sha(value):return hashlib.sha256(value.encode()).hexdigest()
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def messages(state,memories,protocol_version=1):
    block='No past experience is provided.' if not memories else '\n\n'.join('Past experience '+str(i+1)+':\n'+m for i,m in enumerate(memories))
    return [{'role':'system','content':SYSTEMS[protocol_version]},{'role':'user','content':'Current task:\n'+state+'\n\nAvailable past experience:\n'+block+'\n\nImplement solve(data, operation).'}]


def validate_bundle(data,config):
    if config['model_id'] not in PINS or config['revision']!=PINS[config['model_id']]:raise ValueError('Unreviewed worker revision')
    version=config.get('protocol_version',1)
    if version not in SYSTEMS or config['precision']!='bfloat16' or config['script_budget_seconds']<=0 or config['max_new_tokens']!={1:256,2:512,3:512}[version]:
        raise ValueError('Changed worker precision/budget/generation contract')
    if config.get('do_sample') is not False or config.get('enable_thinking') is not False:raise ValueError('Worker must use fixed non-thinking greedy decoding')
    manifest=json.loads((data/'manifest.json').read_text())
    if set(manifest['export_sha256'])!=set(FILES):raise ValueError('Unexpected worker files')
    for name,digest in manifest['export_sha256'].items():
        if sha(data/name)!=digest:raise ValueError('Worker prompt/protocol changed')
    protocol=json.loads((data/'protocol.json').read_text())
    if protocol.get('protocol_version',1)!=version:raise ValueError('Worker config/prompt versions differ')
    if protocol['outcomes_included'] or not protocol['frozen_before_generation']:raise ValueError('Worker input must precede outcomes')
    jobs=[json.loads(l) for l in (data/'jobs.jsonl').read_text().splitlines()]
    if len({r['job_id'] for r in jobs})!=len(jobs) or not jobs:raise ValueError('Empty/duplicate worker jobs')
    by_id={r['job_id']:r for r in jobs};base=[r for r in jobs if r['repeat_of'] is None]
    if len(base)!=protocol['base_jobs'] or len(jobs)-len(base)!=protocol['repeat_jobs'] or len({r['state_id'] for r in base})!=protocol['states']:
        raise ValueError('Worker job inventory differs')
    expected_actions={'none','all_full'}|{recipe+'_'+view for recipe in ['returned','receiver','copied_returned','copied_receiver'] for view in ['full','procedure']}
    groups=defaultdict(list)
    for row in jobs:
        if set(row)!={'job_id','state_id','action','split','messages','messages_sha256','repeat_of'}:raise ValueError('Unreviewed worker input fields')
        if row['messages_sha256']!=text_sha(json.dumps(row['messages'],ensure_ascii=False,sort_keys=True)):raise ValueError('Worker prompt hash differs')
        if row['messages'][0]!={'role':'system','content':SYSTEMS[version]}:raise ValueError('Worker system contract changed')
        if row['repeat_of'] is None:groups[row['state_id']].append(row)
        elif row['repeat_of'] not in by_id or any(row[k]!=by_id[row['repeat_of']][k] for k in ['messages','state_id','action','split']):
            raise ValueError('Repeat prompt differs from original')
    if any(len(rs)!=10 or {r['action'] for r in rs}!=expected_actions for rs in groups.values()):raise ValueError('Incomplete action grid')
    if protocol['pilot'] and (set(groups)!=set(PILOT) or any(r['split']!='train' for r in jobs)):raise ValueError('Pilot cohort changed')
    return jobs,protocol


def prepare(source,output,pilot=True,protocol_version=1):
    from data_pipeline.audit_api_transfer import audit
    if audit(source)['status']!='passed':raise ValueError('Unaudited data')
    if output.exists():raise FileExistsError(output)
    groups=defaultdict(list)
    for split in (['train'] if pilot else ['train','dev','test']):
        for line in (source/(split+'.jsonl')).read_text().splitlines():
            row=json.loads(line)
            if not pilot or row['state_id'] in PILOT:groups[row['state_id']].append(row)
    if pilot and set(groups)!=set(PILOT):raise ValueError('Missing predeclared pilot state')
    jobs=[]
    for state_id,rows in sorted(groups.items()):
        if len(rows)!=4 or len({r['recipe'] for r in rows})!=4 or len({r['input']['state_text'] for r in rows})!=1:
            raise ValueError('Incomplete candidate grid')
        order=sorted(rows,key=lambda r:text_sha(state_id+'|'+r['recipe']))
        arms=[('none',[]),('all_full',[r['input']['candidate_text'] for r in order])]
        for row in sorted(rows,key=lambda r:r['recipe']):
            arms.extend([(row['recipe']+'_full',[row['input']['candidate_text']]),
                         (row['recipe']+'_procedure',[row['input']['procedure_text']])])
        for action,memory in arms:
            prompt=messages(rows[0]['input']['state_text'],memory,protocol_version)
            jobs.append({'job_id':state_id+'--'+action,'state_id':state_id,'action':action,'split':rows[0]['split'],
                         'messages':prompt,'messages_sha256':text_sha(json.dumps(prompt,ensure_ascii=False,sort_keys=True)),'repeat_of':None})
    random.Random(731).shuffle(jobs)
    for i,row in enumerate(jobs[:4]):jobs.append(row|{'job_id':row['job_id']+'-repeat-'+str(i),'repeat_of':row['job_id']})
    output.mkdir(parents=True)
    with (output/'jobs.jsonl').open('w') as f:
        for row in jobs:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    write(output/'protocol.json',{'name':'api-worker-utility-v'+str(protocol_version),'protocol_version':protocol_version,'frozen_before_generation':True,'outcomes_included':False,
        'source_index_sha256':sha(source/'artifact-index.json'),'system_sha256':text_sha(SYSTEMS[protocol_version]),'pilot':pilot,
        'states':len(groups),'base_jobs':len(jobs)-4,'repeat_jobs':4,'training_states_only':pilot,
        'judge_language':'Bounded interpreted straight-line Python with conditionals; unsupported syntax is a separate failure category.',
        'expansion_gate':'At least 80% syntax supported; statewise best strict success exceeds best fixed action within none plus eight single-memory actions. All-full is a separate benchmark. Training pilot only.',
        'judge_source_sha256':sha(HERE.parents[1]/'data_pipeline/api_worker_judge.py'),
        'interpreter_source_sha256':sha(HERE.parents[1]/'data_pipeline/api_worker_interpreter.py'),
        'interpretation':'Fixed-worker API wrapper microtasks, not SWE or real trajectory effects.'})
    write(output/'manifest.json',{'export_sha256':{name:sha(output/name) for name in FILES}})


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--full',action='store_true');p.add_argument('--protocol-version',type=int,choices=[1,2,3],default=1)
    a=p.parse_args();prepare(a.source,a.output,not a.full,a.protocol_version)
