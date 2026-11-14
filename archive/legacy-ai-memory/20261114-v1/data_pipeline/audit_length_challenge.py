"""Read-only checks for evidence, exact labels, disjointness and token shortcuts."""
from __future__ import annotations
import argparse
from collections import defaultdict
import json
from pathlib import Path
from tokenizers import Tokenizer
from data_pipeline.scope_expansion import construct,validate_rows,selected_scopes,sha

ROOT=Path(__file__).resolve().parents[1]


def audit(folder):
    review=json.loads((folder/'source-review.json').read_text())
    rows=[json.loads(l) for l in (folder/'challenge.jsonl').read_text().splitlines()]
    if rows!=construct(review['families'],['A','B','C']):raise ValueError('Challenge does not regenerate from reviewed families')
    validate_rows(rows)
    verified={(r['repository'],r['path']):r for r in review['upstream_verification']['files']}
    for f in review['families']:
        for scope,ids in f['scopes'].items():
            proof=verified[f['repository'],scope]
            if proof['status']!='verified_present' or not proof['commit'].startswith(f['source'].split('@')[-1]):raise ValueError('Upstream presence mismatch')
            for oid in ids:
                e=json.loads((folder/'evidence'/(oid+'.json')).read_text())
                args=json.loads(e['tool_call']['function']['arguments'])
                if args['command']!=e['command'] or scope not in selected_scopes(e['command']):raise ValueError('Command does not select declared whole scope')
                if e['tool_call']['id'] not in e['observation']['tool_call_ids'] or e['observation_index']<=e['message_index']:raise ValueError('Response linkage mismatch')
                if '/testbed/'+scope in e['created_paths_in_trace'] or scope in e['created_paths_in_trace']:raise ValueError('Scope was agent-created')
    original=ROOT/'datasets/scope_relations/20261108-expansion-v1'
    existing=[json.loads(l) for p in original.glob('*.jsonl') for l in p.read_text().splitlines()]
    if {r['sample_id'] for r in rows}&{r['sample_id'] for r in existing} or {r['repository'] for r in rows}&{r['repository'] for r in existing if 'repository' in r}:raise ValueError('Challenge overlaps existing repositories/inputs')
    tokenizer_path=ROOT/'training/memory_selector/outputs/20261107T144957Z/artifacts/tokenizer/tokenizer.json'
    t=Tokenizer.from_file(str(tokenizer_path));tokens=lambda text:len(t.encode(text,add_special_tokens=False).ids)
    config=json.loads((ROOT/'training/memory_selector/configs/scope-expansion-wave1-v1.json').read_text())
    instruction=config['query_instruction'];groups=defaultdict(list);correct=[];known_correct=[];hard_correct=[]
    lengths={}
    for r in rows:
        m=r['selected_scope'];q=r['controlled_state']['requested_scope'];c=r['controlled_state']['completed_scope']
        p=[2 if q is None else int(tokens(q)!=tokens(m)),2 if c is None else int(tokens(c)==tokens(m))]
        correct.append(p==r['labels'])
        if q is not None and c is not None:
            known_correct.append(p==r['labels']);groups[r['repository'],r['template']].append(r)
            if r['labels'] in [[0,0],[1,1]]:hard_correct.append(p==r['labels'])
    for (repo,template),values in groups.items():
        channels=defaultdict(set)
        for r in values:
            s,m=r['input']['state_text'],r['input']['candidate_text']
            channels['pair_state'].add(tokens(f'Instruct: {instruction}\nQuery: {s}'))
            channels['pair_memory'].add(tokens(m))
            channels['joint'].add(tokens(f'Instruct: {instruction}\nQuery: Current evidence:\n{s}\n\nHistorical memory:\n{m}'))
            for h,question in enumerate(['Is the historical scope exactly the currently requested scope?','Is the historical scope already in the complete record of scopes checked?']):
                channels[f'role_state_{h}'].add(tokens(f'Instruct: {instruction}\nRelation to classify: {question}\nQuery: {s}'))
            channels['state_characters'].add(len(s));channels['memory_characters'].add(len(m))
        if any(len(v)!=1 for v in channels.values()):raise ValueError('Whole model input lengths reveal scope assignment')
        lengths[repo+'|'+template]={k:next(iter(v)) for k,v in channels.items()}
    frozen=folder/'artifact-index.json'
    if frozen.exists():
        for name,digest in json.loads(frozen.read_text())['files_sha256'].items():
            if sha(folder/name)!=digest:raise ValueError('Frozen challenge changed: '+name)
    return {'status':'passed','rows':len(rows),'source_episodes':len({s for f in review['families'] for s in f['source_instances']}),
            'repositories':len(review['families']),'verified_commands':len(review['observation_ids']),
            'tokenizer_sha256':sha(tokenizer_path),'whole_known_input_lengths_by_repository_template':lengths,
            'length_rule':{'all_correct':sum(correct),'all_n':len(correct),'known_correct':sum(known_correct),'known_n':len(known_correct),'hard_correct':sum(hard_correct),'hard_n':len(hard_correct)},
            'regenerates_exactly':True,'existing_repositories_and_inputs_disjoint':True,'data_modified':False}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('folder',type=Path)
    print(json.dumps(audit(p.parse_args().folder),indent=2))
