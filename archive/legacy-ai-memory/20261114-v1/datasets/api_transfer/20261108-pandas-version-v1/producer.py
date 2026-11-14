"""Freeze a small observed pandas-version contrast; never training data.

This is one release-contract change with two stable controls, not additional
independent libraries or a broad version benchmark. Same-implementation/source
matches are intentional positive controls under the unchanged version.
"""
from __future__ import annotations
import argparse
import copy
import json
from pathlib import Path
import shutil
from data_pipeline.api_transfer import sha,write,compact,goal_text,world_text,label_from_outcomes,LABELS

CASES=['pandas-fill-true','pandas-fill-false','pandas-sort-true']


def build(old,new,output):
    if output.exists():raise FileExistsError(output)
    evidence={};environments={};specs={};sources=[]
    for folder in [old,new]:
        env=json.loads((folder/'environment.json').read_text());version=env['packages']['pandas'];environments[version]=env
        records=json.loads((folder/'executions.json').read_text())
        evidence[version]={(r['case_id'],r['fixture_id'],r['goal'],r['recipe']):r for r in records if r['case_id'] in CASES}
        specs[version]={r['case_id']:r for r in json.loads((folder/'confirmation-fixtures.json').read_text()) if r['case_id'] in CASES}
        for r in evidence[version].values():
            if r['exception'] is not None or r['operation_calls']!=1:raise ValueError('Unusable version execution')
            if r['passed']!=evidence[version][r['case_id'],1-r['fixture_id'],r['goal'],r['recipe']]['passed']:
                raise ValueError('Version fixture disagreement')
    if set(evidence)!={'2.3.3','3.1.0'}:raise ValueError('Unexpected version pair')
    recipes=json.loads((new/'recipes.json').read_text());original={r['case_id']:r for r in json.loads((new/'catalog.json').read_text())}
    source_spec=original['pandas-fill-true']
    for recipe,code in recipes.items():
        goal='preserve' if recipe.startswith('copied_') else 'mutate'
        r=evidence['3.1.0']['pandas-fill-true',0,goal,recipe]
        if not r['passed']:raise ValueError('Past procedure was not successful')
        text=(f"Past successful experience (pandas 3.1.0):\nInput and fitted state:\n{source_spec['setup']}\n"
              f"Past operation(data):\n    {source_spec['operation']}\nPast requirement: {source_spec['description']} Return the transformed result. {goal_text(goal)}\n"
              f"Reusable procedure:\n{code}\nObserved returned value: {compact(r['output'])}\nObserved original after execution: {compact(r['after'])}\n"
              'The returned value and original-object requirement both passed in that past execution.')
        sources.append({'source_id':'version-source-'+recipe,'recipe':recipe,'text':text,'pandas_version':'3.1.0',
                        'evidence_key':['pandas-fill-true',0,goal,recipe]})
    rows=[]
    for case in CASES:
        for versions in [['2.3.3'],['3.1.0'],['2.3.3','3.1.0']]:
            visible=[world_text(specs[v][case],environments[v]) for v in versions]
            current=(visible[0] if len(visible)==1 else 'The installed pandas version has not been observed. Exactly one of these two environments is in use:\n\n'+
                     '\n\n'.join(f'Possible environment {i+1}:\n{text}' for i,text in enumerate(visible)))
            for goal in ['preserve','mutate']:
                state_id=case+'-version-'+('-'.join(versions))+'-'+goal
                for source in sources:
                    recipe=source['recipe'];outcomes=[evidence[v][case,1,goal,recipe]['passed'] for v in versions]
                    label=label_from_outcomes(outcomes)
                    concepts=[{'returns_transformed':evidence[v][case,1,'mutate','returned']['output_pass'],
                               'mutates_original_to_transformed':evidence[v][case,1,'mutate','returned']['effect_pass']} for v in versions]
                    rows.append({'sample_id':state_id+'-'+recipe,'state_id':state_id,'family':'pandas','split':'version_challenge',
                        'source_id':source['source_id'],'source_case_id':'3.1.0:pandas-fill-true','world_ids':[v+':'+case for v in versions],
                        'goal':goal,'recipe':recipe,'observation':'known' if len(versions)==1 else 'version_unobserved',
                        'input':{'state_text':current+'\nCurrent requirement: Return the transformed result. '+goal_text(goal),
                                 'candidate_text':source['text'],'world_texts':visible,'goal_text':goal_text(goal),'procedure_text':recipes[recipe]},
                        'label':label,'label_name':LABELS[label],'evidence':{'target_outcomes':outcomes,'target_fixture_id':1,'source_fixture_id':0,
                            'operation_concepts':concepts,'source_success':True},'pandas_versions':versions,'api_case':case,
                        'version_pair_group':case+'-'+goal+'-'+recipe,'same_implementation_history_is_intentional_version_control':True,
                        'scope':'One actual pandas return-contract change; same-family version stress test, not independent natural SWE sources.'})
    # Verify exact expected version sensitivity before freezing. Model outputs
    # have not been computed and are never used to select these rows.
    grouped={}
    for row in rows:
        if len(row['pandas_versions'])==1:grouped.setdefault(row['version_pair_group'],[]).append(row)
    flips=[key for key,rs in grouped.items() if rs[0]['label']!=rs[1]['label']]
    if sorted(flips)!=['pandas-fill-true-mutate-returned','pandas-fill-true-preserve-copied_returned']:
        raise ValueError('Unexpected library-version behavior')
    output.mkdir(parents=True);(output/'challenge.jsonl').write_text(''.join(compact(r)+'\n' for r in rows))
    write(output/'sources.json',sources)
    for version,records in evidence.items():write(output/('executions-pandas-'+version+'.json'),list(records.values()))
    write(output/'environments.json',environments)
    report={'rows':len(rows),'current_states':len({r['state_id'] for r in rows}),'historical_episodes':len(sources),
            'versions':['2.3.3','3.1.0'],'api_configurations':len(CASES),'distinct_api_functions':2,'version_change_mechanisms':1,
            'known_version_pairs':len(grouped),'label_flipping_version_pairs':flips,'unknown_version_mixed_rows':sum(r['label']==2 for r in rows),
            'training_allowed':False,'model_predictions_before_freeze':False,'producer_sha256':sha(Path(__file__)),
            'source_execution_files_sha256':{str(folder):sha(folder/'executions.json') for folder in [old,new]},
            'limits':'Pandas is already the development family. One version transition, two sensitive pairs; no independent-family performance claim.'}
    write(output/'summary.json',report);shutil.copyfile(Path(__file__),output/'producer.py')
    (output/'README.md').write_text('''# pandas version contrast, 2026-11-08

Evaluation only, frozen before any model prediction. This is one actual return-contract transition with two stable controls, **not** a new family holdout or broad version benchmark.

The successful historical procedure ran under pandas 3.1.0. The current environment may be 3.1.0 or 2.3.3, so this is cross-environment applicability, not necessarily a chronologically older memory. `fillna(inplace=True)` returns the DataFrame in the new runtime and None in the older runtime. The two independently varied fixture inputs agree. `fillna(inplace=False)` and `sort_values(inplace=True)` are stable controls. Both requirements (preserve/mutate original) and four procedures are tested. A version-unobserved condition enumerates the two possible environments.

72 rows represent 18 current states and four source episodes. Only **two known-version candidate pairs change label**; there are only two mixed/insufficient rows. All current pandas 3.1.0 states overlap the development API family. The same-implementation history in the unchanged-version condition is an intentional positive control with a different input fixture. Do not treat these rows as independent experiences or use them for checkpoint/threshold selection.

Source and target executions, exact package versions, row-to-evidence keys and hashes are included. The model sees `input` only. Future target outcomes and auxiliary concept targets are audit/supervision fields.

Primary contract references: [pandas 2.3.3 fillna](https://pandas.pydata.org/pandas-docs/version/2.3.3/reference/api/pandas.DataFrame.fillna.html), [current fillna](https://pandas.pydata.org/docs/dev/reference/api/pandas.DataFrame.fillna.html). The recorded executions, not a floating documentation URL, define this frozen test.
''')
    write(output/'artifact-index.json',{'status':'frozen_before_model_predictions','training_allowed':False,
        'files_sha256':{str(p.relative_to(output)):sha(p) for p in sorted(output.rglob('*')) if p.is_file()}})
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--old',type=Path,required=True);p.add_argument('--new',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();print(json.dumps(build(a.old,a.new,a.output),ensure_ascii=False,indent=2))
