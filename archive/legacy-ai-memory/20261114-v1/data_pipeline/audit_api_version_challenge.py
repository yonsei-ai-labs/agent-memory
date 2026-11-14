"""Read-only evidence audit for the small cross-version API panel."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from data_pipeline.api_transfer import sha,label_from_outcomes,LABELS


def audit(data):
    index=json.loads((data/'artifact-index.json').read_text())
    if index['status']!='frozen_before_model_predictions' or index['training_allowed']:raise ValueError('Wrong version panel contract')
    for name,digest in index['files_sha256'].items():
        if sha(data/name)!=digest:raise ValueError('Version panel hash mismatch')
    evidence={v:{(r['case_id'],r['fixture_id'],r['goal'],r['recipe']):r for r in json.loads((data/f'executions-pandas-{v}.json').read_text())}
              for v in ['2.3.3','3.1.0']}
    sources={s['source_id']:s for s in json.loads((data/'sources.json').read_text())};rows=[json.loads(l) for l in (data/'challenge.jsonl').read_text().splitlines()]
    for source in sources.values():
        r=evidence[source['pandas_version']][tuple(source['evidence_key'])]
        if not r['passed']:raise ValueError('Unsuccessful historical source')
        for field,marker in [('output','Observed returned value: '),('after','Observed original after execution: ')]:
            if json.loads(source['text'].split(marker,1)[1].split('\n',1)[0])!=r[field]:raise ValueError('Memory observation mismatch')
    for row in rows:
        if row['input']['candidate_text']!=sources[row['source_id']]['text'] or row['split']!='version_challenge':raise ValueError('Source or split mismatch')
        outcomes=[]
        for version,concept,world_text in zip(row['pandas_versions'],row['evidence']['operation_concepts'],row['input']['world_texts']):
            r=evidence[version][row['api_case'],1,row['goal'],row['recipe']]
            other=evidence[version][row['api_case'],0,row['goal'],row['recipe']]
            if r['passed']!=other['passed'] or r['exception'] is not None or r['operation_calls']!=1:raise ValueError('Target fixture/runtime mismatch')
            outcomes.append(r['passed']);direct=evidence[version][row['api_case'],1,'mutate','returned']
            if concept!={'returns_transformed':direct['output_pass'],'mutates_original_to_transformed':direct['effect_pass']}:
                raise ValueError('Version concept label mismatch')
            if not world_text.startswith('Environment: pandas '+version+'\n'):raise ValueError('Visible version differs from runtime evidence')
        label=label_from_outcomes(outcomes)
        if row['evidence']['target_outcomes']!=outcomes or row['label']!=label or row['label_name']!=LABELS[label]:raise ValueError('Version relation label mismatch')
    return {'status':'passed','rows':len(rows),'source_episodes':len(sources),'execution_records':sum(len(v) for v in evidence.values()),
            'version_change_mechanisms':1,'training_allowed':False,'model_predictions_used_in_audit':False,
            'independent_family_holdout':False}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('data',type=Path)
    print(json.dumps(audit(p.parse_args().data),indent=2))
