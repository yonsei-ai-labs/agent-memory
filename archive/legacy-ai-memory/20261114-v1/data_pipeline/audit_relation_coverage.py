"""Read-only audit of relation-type and question coverage in frozen repo folds."""
from __future__ import annotations
import argparse
from collections import Counter,defaultdict
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'datasets/memory_relations/20261107-discovery-v1'


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(with_local_predictions=False):
    paths=[DATA/'relations.jsonl',DATA/'group-splits.json',DATA/'learner-inputs.jsonl',DATA/'supervision.jsonl',Path(__file__)]
    original=[json.loads(line) for line in paths[0].read_text().splitlines()]
    rows={r['sample_id']:r for r in original}
    if len(rows)!=len(original):raise ValueError('Duplicate relation IDs')
    inputs={r['sample_id']:r for r in map(json.loads,paths[2].read_text().splitlines())}
    supervision={r['sample_id']:r for r in map(json.loads,paths[3].read_text().splitlines())}
    if set(inputs)!=set(rows) or set(supervision)!=set(rows):raise ValueError('Learner/supervision IDs differ')
    for sid,r in rows.items():
        if r['input']!=inputs[sid]['input'] or any(r[k]!=supervision[sid][k] for k in ['facet','label']):
            raise ValueError('Analysis differs from actual learner data')
    splits=json.loads(paths[1].read_text());assigned={};folds=[];facets=defaultdict(list)
    question=lambda r:r['input']['state_text'].split('\n\n',1)[0]
    for fold in splits['folds']:
        train=[rows[i] for i in fold['train_sample_ids']];held=[rows[i] for i in fold['validation_sample_ids']]
        train_facets=Counter(r['facet'] for r in train);train_questions=Counter(question(r) for r in train)
        records=[]
        for r in held:
            if r['sample_id'] in assigned:raise ValueError('Duplicate out-of-fold assignment')
            record={'sample_id':r['sample_id'],'held_repository':fold['held_repository'],'facet':r['facet'],
                    'label':r['label'],'training_rows_with_same_facet':train_facets[r['facet']],
                    'training_rows_with_identical_question_block':train_questions[question(r)]}
            assigned[r['sample_id']]=record;records.append(record);facets[r['facet']].append(record)
        folds.append({'held_repository':fold['held_repository'],'train_n':len(train),'validation_n':len(held),
                      'unseen_facet_rows':sum(r['training_rows_with_same_facet']==0 for r in records),
                      'unseen_exact_question_rows':sum(r['training_rows_with_identical_question_block']==0 for r in records),
                      'training_label_counts':dict(Counter(r['label'] for r in train))})
    if set(assigned)!=set(rows):raise ValueError('Missing out-of-fold rows')
    facet_table={name:{'n':len(rr),'repositories':sorted({r['held_repository'] for r in rr}),
                       'label_counts':dict(Counter(r['label'] for r in rr))} for name,rr in sorted(facets.items())}
    unseen={sid for sid,r in assigned.items() if r['training_rows_with_same_facet']==0}
    output={'status':'passed','analysis':'Posthoc coverage audit; original data/folds unchanged',
            'source_sha256':{str(p.relative_to(ROOT)):sha(p) for p in paths},'rows':len(rows),'facets':len(facets),
            'unseen_facet_rows':len(unseen),'unseen_exact_question_rows':sum(r['training_rows_with_identical_question_block']==0 for r in assigned.values()),
            'facets_present_in_multiple_repositories':sum(len(v['repositories'])>1 for v in facet_table.values()),
            'folds':folds,'facet_table':facet_table,'row_coverage':list(assigned.values()),
            'interpretation':'Facet identity and verbatim question overlap are coverage diagnostics, not proof that all abstract logical concepts are unseen or that coverage alone caused errors.',
            'learner_inputs_and_supervision_verified':True,'data_modified':False,'gpu_used':False,'predictions':{}}
    if with_local_predictions:
        base=ROOT/'training/memory_selector/outputs'
        # Keep the original model/view conditions explicit.
        # The untrained 9B reference did not use these folds, so the training
        # coverage interpretation does not apply to it.
        files=[base/'20261107T112302Z/artifacts/harrier'/view/'predictions.json'
               for view in ['full','state-only','question-memory']]
        for name in ['cpu-natural-heads-20261108-v1','cpu-natural-convergence-20261108-v1','cpu-natural-joint-20261108-v1-retry']:
            files.extend(sorted((base/name).glob('*/predictions-epoch-*.json')))
        files.extend(sorted((base/'cpu-natural-ridge-20261108-v1').glob('*/predictions.json')))
        for path in files:
            pred=json.loads(path.read_text())
            if len(pred)!=len(rows) or {r['sample_id'] for r in pred}!=set(rows):raise ValueError('Incomplete out-of-fold predictions')
            if any(r['label']!=rows[r['sample_id']]['label'] for r in pred):raise ValueError('Prediction labels differ')
            table={}
            for name,subset in [('seen_facet',set(rows)-unseen),('unseen_facet',unseen),('all',set(rows))]:
                correct=sum(max(range(3),key=r['probabilities'].__getitem__)==r['label'] for r in pred if r['sample_id'] in subset)
                table[name]={'n':len(subset),'correct':correct}
            output['predictions'][str(path.relative_to(base))]={'sha256':sha(path),'metrics':table}
    return output


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--with-local-predictions',action='store_true');a=p.parse_args()
    print(json.dumps(audit(a.with_local_predictions),ensure_ascii=False,indent=2))
