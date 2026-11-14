"""Build versioned, execution-backed procedural memory selection diagnostics.

The unit is an API implementation/state, not an independent natural SWE task.
Each target has four historically successful procedural candidates. Labels are
derived only from fresh executions, never from model-generated judgments.
"""
from __future__ import annotations
import argparse
from collections import Counter,defaultdict
import hashlib
import json
from pathlib import Path
import shutil

SPLITS={'numpy':'train','scipy':'train','pillow':'train','stdlib':'train',
        'pandas':'dev','networkx':'test','sklearn':'test'}
PACKAGES={'numpy':'numpy','scipy':'scipy','pillow':'Pillow','pandas':'pandas',
          'networkx':'networkx','sklearn':'scikit-learn','stdlib':'python'}
LABELS=['supported','conflict','information_insufficient']


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')


def compact(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False)


def goal_text(goal):
    return ('The original input must remain unchanged.' if goal=='preserve' else
            'The original input object itself must contain the transformed result after the call.')


def version(family,environment):
    p=PACKAGES[family]
    return p+' '+(environment['python'] if p=='python' else environment['packages'][p])


def world_text(spec,environment):
    return (f"Environment: {version(spec['family'],environment)}\nInput and fitted state:\n{spec['setup']}\n"
            f"Implementation of operation(data):\n    {spec['operation']}\nTransformation: {spec['description']}")


def label_from_outcomes(values):
    if all(values):return 0
    if not any(values):return 1
    return 2


def build(review,output):
    if output.exists():raise FileExistsError(output)
    original=json.loads((review/'catalog.json').read_text());confirmation=json.loads((review/'confirmation-fixtures.json').read_text())
    environment=json.loads((review/'environment.json').read_text());recipes=json.loads((review/'recipes.json').read_text())
    executions=json.loads((review/'executions.json').read_text())
    specs={r['case_id']:r for r in original};targets={r['case_id']:r for r in confirmation}
    index={(r['case_id'],r['fixture_id'],r['goal'],r['recipe']):r for r in executions}
    if len(index)!=len(executions) or len(executions)!=len(specs)*16:raise ValueError('Execution grid incomplete or duplicate')
    if set(specs)!=set(targets) or set(SPLITS)!={r['family'] for r in original}:raise ValueError('Unplanned API family/case')
    for key,r in index.items():
        other=index[(r['case_id'],1-r['fixture_id'],r['goal'],r['recipe'])]
        if r['passed']!=other['passed'] or r['exception'] is not None or r['operation_calls']!=1:
            raise ValueError('Fixture-dependent validity or runtime error: '+r['case_id'])
    # Past episodes are successful executions of fixture 0 only. Targets expose
    # fixture 1, and a memory may not reuse the identical source implementation.
    sources=[]
    for r in executions:
        if r['fixture_id']!=0 or not r['passed']:continue
        spec=specs[r['case_id']];sid='source-'+r['case_id']+'-'+r['goal']+'-'+r['recipe']
        text=(f"Past successful experience ({version(spec['family'],environment)}):\n"
              f"Input and fitted state:\n{spec['setup']}\nPast operation(data):\n    {spec['operation']}\n"
              f"Past requirement: {spec['description']} Return the transformed result. {goal_text(r['goal'])}\n"
              f"Reusable procedure:\n{recipes[r['recipe']]}\n"
              f"Observed returned value: {compact(r['output'])}\nObserved original after execution: {compact(r['after'])}\n"
              'The returned value and original-object requirement both passed in that past execution.')
        sources.append({'source_id':sid,'case_id':r['case_id'],'family':spec['family'],'goal':r['goal'],
                        'recipe':r['recipe'],'text':text,'evidence_key':list((r['case_id'],0,r['goal'],r['recipe']))})
    sources.sort(key=lambda r:r['source_id'])
    contexts=[{'context_id':s['case_id'],'family':s['family'],'worlds':[s['case_id']],'observation':'known'} for s in original]
    masked=defaultdict(list)
    for s in original:
        if s['mask_group']:masked[s['family'],s['mask_group']].append(s['case_id'])
    for (family,group),worlds in sorted(masked.items()):
        if len(worlds)!=2:raise ValueError('Masked contexts require exactly two reviewed alternatives')
        if len({targets[w]['setup'] for w in worlds})!=1 or len({targets[w]['description'] for w in worlds})!=1:
            raise ValueError('Masked alternatives differ beyond the operation: '+group)
        contexts.append({'context_id':family+'-unobserved-'+group,'family':family,'worlds':sorted(worlds),'observation':'mode_unobserved'})
    rows={s:[] for s in ['train','dev','test']};used_sources=set();states=[]
    for context in contexts:
        family=context['family'];worlds=context['worlds']
        texts=[world_text(targets[w],environment) for w in worlds]
        state_prefix=(texts[0] if len(worlds)==1 else
                      'The operation mode has not been observed. Exactly one of these implementations is in use; no other implementation is possible.\n\n'+
                      '\n\n'.join(f'Possible implementation {i+1}:\n{text}' for i,text in enumerate(texts)))
        for goal in ['preserve','mutate']:
            state_id=context['context_id']+'-'+goal
            current=state_prefix+'\nCurrent requirement: Return the transformed result. '+goal_text(goal)
            states.append(context|{'state_id':state_id,'goal':goal,'state_text':current})
            for recipe,code in recipes.items():
                candidates=[s for s in sources if s['family']==family and s['recipe']==recipe and s['case_id'] not in worlds]
                if not candidates:raise ValueError('No independent successful source for '+state_id+' '+recipe)
                source=candidates[0];used_sources.add(source['source_id'])
                outcomes=[index[w,1,goal,recipe]['passed'] for w in worlds]
                concepts=[]
                for world in worlds:
                    direct=index[world,1,'mutate','returned']
                    concepts.append({'returns_transformed':direct['output_pass'],'mutates_original_to_transformed':direct['effect_pass']})
                row={'sample_id':state_id+'-'+recipe,'state_id':state_id,'family':family,'split':SPLITS[family],
                     'source_id':source['source_id'],'source_case_id':source['case_id'],'world_ids':worlds,'goal':goal,'recipe':recipe,
                     'observation':context['observation'],'input':{'state_text':current,'candidate_text':source['text'],
                         'world_texts':texts,'goal_text':goal_text(goal),'procedure_text':code},
                     'label':label_from_outcomes(outcomes),'label_name':LABELS[label_from_outcomes(outcomes)],
                     'evidence':{'target_outcomes':outcomes,'target_fixture_id':1,'source_fixture_id':0,
                                 'operation_concepts':concepts,'source_success':True},
                     'scope':'Exact authored procedure reuse on two fixed fixtures; not LLM-memory utility.'}
                rows[SPLITS[family]].append(row)
    output.mkdir(parents=True)
    for split,data in rows.items():
        (output/(split+'.jsonl')).write_text(''.join(compact(r)+'\n' for r in data))
    write(output/'sources.json',[s for s in sources if s['source_id'] in used_sources]);write(output/'states.json',states)
    evidence=output/'evidence';evidence.mkdir()
    for p in review.glob('*.json'):shutil.copyfile(p,evidence/p.name)
    summary={'origin':'Authored execution-backed API transfer diagnostic; not naturally collected SWE trajectories.',
             'cases':len(specs),'libraries':len(SPLITS),'fixtures_per_implementation':2,'execution_records':len(executions),
             'source_successes_available':len(sources),'distinct_source_episodes_used':len(used_sources),'current_states':len(states),
             'splits':{s:{'rows':len(rs),'states':len({r['state_id'] for r in rs}),
                          'families':sorted({r['family'] for r in rs}),'labels':dict(Counter(r['label_name'] for r in rs))} for s,rs in rows.items()},
             'data_builder_sha256':sha(Path(__file__)),'runtime_sha256':environment['runtime_code_sha256'],
             'test_available_during_training':False,'model_predictions_before_freeze':False}
    write(output/'summary.json',summary)
    write(output/'artifact-index.json',{'status':'review_required_before_training','files_sha256':{
        str(p.relative_to(output)):sha(p) for p in sorted(output.rglob('*')) if p.is_file()},
        'note':'No trained predictions exist. Freeze only after execution, source and leakage review.'})
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--review',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();print(json.dumps(build(a.review,a.output),indent=2))
