"""Read-only stdlib audit of API history/target evidence and relation labels."""
from __future__ import annotations
import argparse
from collections import Counter,defaultdict
import json
from pathlib import Path
from data_pipeline.api_transfer import sha,LABELS,SPLITS,label_from_outcomes


def compatible(returned,mutated,recipe,goal):
    cloned=recipe.startswith('copied_');uses_return=recipe.endswith('returned')
    output=returned if uses_return else mutated
    effect=(cloned or not mutated) if goal=='preserve' else (not cloned and mutated)
    return bool(output and effect)


def input_ceiling(rows,field):
    groups=defaultdict(Counter)
    for r in rows:groups[r['input'][field]][r['label']]+=1
    return {'correct_upper_bound':sum(max(v.values()) for v in groups.values()),'n':len(rows),
            'ambiguous_text_groups':sum(len(v)>1 for v in groups.values()),'unique_texts':len(groups),
            'note':'Oracle majority for identical text groups, not a trained predictor or cross-source estimate.'}


def audit(data):
    artifact=json.loads((data/'artifact-index.json').read_text())
    for name,value in artifact['files_sha256'].items():
        if sha(data/name)!=value:raise ValueError('Frozen file changed: '+name)
    summary=json.loads((data/'summary.json').read_text())
    rows={s:[json.loads(l) for l in (data/(s+'.jsonl')).read_text().splitlines()] for s in ['train','dev','test']}
    sources={r['source_id']:r for r in json.loads((data/'sources.json').read_text())}
    states={r['state_id']:r for r in json.loads((data/'states.json').read_text())}
    records=json.loads((data/'evidence/executions.json').read_text())
    executions={(r['case_id'],r['fixture_id'],r['goal'],r['recipe']):r for r in records}
    if len(executions)!=len(records):raise ValueError('Duplicate execution keys')
    for key,r in executions.items():
        if r['exception'] is not None or r['operation_calls']!=1 or r['before']==r['expected']:
            raise ValueError('Unusable/no-op execution')
        if r['passed']!=(r['output_pass'] and r['effect_pass']):raise ValueError('Execution verdict differs from checks')
        if executions[r['case_id'],1-r['fixture_id'],r['goal'],r['recipe']]['passed']!=r['passed']:
            raise ValueError('Two confirmation inputs disagree')
        direct=executions[r['case_id'],r['fixture_id'],'mutate','returned']
        inferred=compatible(direct['output_pass'],direct['effect_pass'],r['recipe'],r['goal'])
        if inferred!=r['passed']:raise ValueError('Two-concept abstraction does not describe this execution')
    for source in sources.values():
        record=executions[tuple(source['evidence_key'])]
        if not record['passed'] or record['fixture_id']!=0 or record['recipe']!=source['recipe']:
            raise ValueError('Historical source was not successful')
        for field,marker in [('output','Observed returned value: '),('after','Observed original after execution: ')]:
            observed=source['text'].split(marker,1)[1].split('\n',1)[0]
            if json.loads(observed)!=record[field]:raise ValueError('Memory observations differ from source evidence')
    ids=set();pairs=set();by_state=defaultdict(list);families={};diagnostics={}
    for split,rs in rows.items():
        families[split]={r['family'] for r in rs}
        for r in rs:
            if r['sample_id'] in ids:raise ValueError('Duplicate example ID')
            ids.add(r['sample_id']);pair=(r['input']['state_text'],r['input']['candidate_text'])
            if pair in pairs:raise ValueError('Duplicate learner input pair')
            pairs.add(pair);by_state[r['state_id']].append(r)
            if r['split']!=split or SPLITS[r['family']]!=split:raise ValueError('Family crossed split')
            source=sources[r['source_id']];state=states[r['state_id']]
            if source['case_id'] in r['world_ids'] or source['family']!=r['family']:
                raise ValueError('Source equals current implementation or crossed family split')
            if r['input']['candidate_text']!=source['text'] or r['input']['state_text']!=state['state_text']:
                raise ValueError('Input/evidence linkage differs')
            if set(r['input'])!={'state_text','candidate_text','world_texts','goal_text','procedure_text'}:
                raise ValueError('Unreviewed learner input field')
            if any(word in r['input']['state_text'] for word in ['Observed returned value:','Observed original after execution:','passed']):
                raise ValueError('Target execution leaked into decision-time current state')
            outcomes=[executions[w,1,r['goal'],r['recipe']]['passed'] for w in r['world_ids']]
            if outcomes!=r['evidence']['target_outcomes'] or r['label']!=label_from_outcomes(outcomes) or r['label_name']!=LABELS[r['label']]:
                raise ValueError('Target outcomes/label differ')
            for world,concept in zip(r['world_ids'],r['evidence']['operation_concepts']):
                direct=executions[world,1,'mutate','returned']
                if concept!={'returns_transformed':direct['output_pass'],'mutates_original_to_transformed':direct['effect_pass']}:
                    raise ValueError('Auxiliary concept supervision differs from actual execution')
        diagnostics[split]={'rows':len(rs),'states':len({r['state_id'] for r in rs}),
            'label_counts':dict(Counter(r['label_name'] for r in rs)),
            'masked_label_counts':dict(Counter(r['label_name'] for r in rs if r['observation']=='mode_unobserved')),
            'current_text_only_ceiling':input_ceiling(rs,'state_text'),'memory_text_only_ceiling':input_ceiling(rs,'candidate_text')}
    for left in families:
        for right in families:
            if left!=right and families[left]&families[right]:raise ValueError('Family leakage')
    for key,rs in by_state.items():
        if len(rs)!=4 or len({r['recipe'] for r in rs})!=4 or len({r['input']['state_text'] for r in rs})!=1:
            raise ValueError('Current state lacks a consistent four-candidate pool')
    return {'status':'passed','execution_records_rechecked':len(records),'candidate_rows_rechecked':len(ids),
            'source_episodes_rechecked':len(sources),'family_count':len(set.union(*families.values())),
            'two_fixture_validity_agreement':True,'two_concept_logic_matches_executions':True,
            'split_diagnostics':diagnostics,'model_inference_performed':False,'real_swe_utility_demonstrated':False,
            'limitations':['34 authored API implementations; derived rows are not independent experiences.',
                           'Libraries share conventions/dependencies; only one dev and two held-out libraries.',
                           'Four reusable code skeletons; a narrow rule or program analysis may suffice.',
                           'Known possible operation alternatives define synthetic observation limits.',
                           'Value and original-object effect on two fixtures are not a universal correctness proof.']}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('data',type=Path)
    print(json.dumps(audit(p.parse_args().data),ensure_ascii=False,indent=2))
