"""Judge held-out worker outputs only after an audited policy freeze exists."""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import shutil
from data_pipeline.api_worker_judge_serialized import judge


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def read(path):return json.loads(path.read_text())


def run(utility,output):
    result=read(utility/'results.json');selection=read(utility/'policy-selection.json');relations=read(utility/'relation-selection.json')
    if read(utility/'audit.json')['status']!='passed' or selection['results_sha256']!=sha(utility/'results.json'):
        raise ValueError('An audited frozen policy is required before test judging')
    if relations['utility_selection_sha256']!=sha(utility/'policy-selection.json') or relations['test_worker_outcomes_read']:
        raise ValueError('Relation policies not frozen before worker test')
    semantic_path=utility/'semantic-selection.json'
    if semantic_path.exists():
        semantic=read(semantic_path);source=Path(semantic['run'])
        if read(source/'audit.json')['status']!='passed' or semantic['results_sha256']!=sha(source/'results.json') or semantic['test_worker_outcomes_read']:
            raise ValueError('Semantic utility comparison is not audited/frozen')
    if output.exists():raise FileExistsError(output)
    worker=Path(result['worker']);data=Path(result['data']);protocol=read(worker/'artifacts/results.json')['protocol']
    root=Path(__file__).resolve().parents[1]
    if protocol['judge_source_sha256']!=sha(root/'data_pipeline/api_worker_judge.py') or protocol['interpreter_source_sha256']!=sha(root/'data_pipeline/api_worker_interpreter.py'):
        raise ValueError('Judge changed after prompt freeze')
    stamp={'recorded_before_test_judging_utc':datetime.now(timezone.utc).isoformat(),
           'utility_selection_sha256':sha(utility/'policy-selection.json'),'relation_selection_sha256':sha(utility/'relation-selection.json'),
           'utility_audit_sha256':sha(utility/'audit.json'),'worker_predictions_sha256':sha(worker/'artifacts/predictions.jsonl'),
           'serialization_wrapper_sha256':sha(root/'data_pipeline/api_worker_judge_serialized.py'),
           'source_sha256':sha(Path(__file__))}
    if semantic_path.exists():stamp['semantic_selection_sha256']=sha(semantic_path)
    choice_path=utility/'development-policy-choice.json'
    if choice_path.exists():
        choice=read(choice_path)
        if choice['test_worker_outcomes_read'] or any(sha(utility/name)!=digest for name,digest in choice['selection_files_sha256'].items()):
            raise ValueError('Development-only policy choice changed')
        stamp['development_policy_choice_sha256']=sha(choice_path)
    journal=output.with_name(output.name+'.selection-before-judging.json')
    if journal.exists():raise FileExistsError(journal)
    journal.write_text(json.dumps(stamp,indent=2)+'\n')
    summary=judge(data,worker/'artifacts/predictions.jsonl',output,['test'])
    shutil.copyfile(journal,output/'selection-before-judging.json')
    print(json.dumps(summary,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--utility',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();run(a.utility,a.output)
