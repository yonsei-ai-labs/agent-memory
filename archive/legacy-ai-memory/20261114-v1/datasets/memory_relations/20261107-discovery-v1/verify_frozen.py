"""Read-only verification after freezing; no model or Docker calls."""
import hashlib
import json
from pathlib import Path
import re

from audit_dataset import expected_label,verify_refs

ROOT=Path(__file__).resolve().parents[3]
HERE=Path(__file__).resolve().parent


def main():
    index=json.loads((HERE/'artifact-index.json').read_text())
    for row in index['files']:
        path=ROOT/row['path']
        assert path.is_file(),row['path']
        assert path.stat().st_size==row['bytes'],row['path']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==row['sha256'],row['path']
    rows=[json.loads(x) for x in (HERE/'relations.jsonl').read_text().splitlines()]
    inputs=[json.loads(x) for x in (HERE/'learner-inputs.jsonl').read_text().splitlines()]
    labels=[json.loads(x) for x in (HERE/'supervision.jsonl').read_text().splitlines()]
    assert len(rows)==len(inputs)==len(labels)==74
    for row,x,y in zip(rows,inputs,labels):
        assert row['sample_id']==x['sample_id']==y['sample_id']
        assert row['input']==x['input'] and row['label']==y['label']==expected_label(row)
    references=set()
    for name in ['relations.jsonl','effect-observations.jsonl','transfer-candidates.jsonl']:
        verify_refs([json.loads(x) for x in (HERE/name).read_text().splitlines()],references)
    links=0
    for row in index['files']:
        path=ROOT/row['path']
        if path.suffix!='.md':continue
        for target in re.findall(r'\]\(([^\s)]+)\)',path.read_text()):
            if target.startswith(('https://','http://','#','mailto:','plugin://')):continue
            assert (path.parent/target.split('#')[0]).exists(),(str(path),target)
            links+=1
    print(json.dumps(dict(frozen_files_verified=len(index['files']),relation_labels_rechecked=len(rows),
        source_reference_files_verified=len(references),local_document_links_verified=links,all_passed=True)))


if __name__=='__main__':
    main()
