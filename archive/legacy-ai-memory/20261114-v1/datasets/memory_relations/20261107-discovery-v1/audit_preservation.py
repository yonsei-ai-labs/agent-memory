"""Check earlier frozen experiments and training artifacts without changing them."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'experiments/swe_memory/scripts'))
from records import now,write_json


def run(check_only=False):
    previous=json.loads((ROOT/'docs/reports/data/process-restoration-20261107/prior-artifacts-preservation.json').read_text())
    indices=[r['index'] for r in previous['indices']]+[
        'docs/reports/data/process-restoration-20261107/artifact-index.json',
        'training/memory_selector/reviews/2026-11-07-independent/artifact-index.json']
    checks=[]
    cache={}
    for name in indices:
        index=json.loads((ROOT/name).read_text());entries=index['files'];mismatches=[]
        for row in entries:
            path=ROOT/row['path']
            if row['path'] not in cache:
                cache[row['path']]=hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            if cache[row['path']]!=row['sha256']:
                mismatches.append(row['path'])
        checks.append(dict(index=name,entries=len(entries),mismatches=mismatches))
    output=ROOT/'training/memory_selector/outputs/20261107T074128Z'
    hashes=json.loads((output/'artifact-hashes.json').read_text());bad=[]
    for name,expected in hashes.items():
        path=output/name
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            bad.append(name)
    global_path=ROOT/'docs/reports/data/runs.json';records=json.loads(global_path.read_text())
    result=dict(at=now(),model_api_calls=0,indices=checks,
        index_entries_checked=sum(c['entries'] for c in checks),unique_indexed_files=len(cache),
        first_training_artifacts=dict(entries=len(hashes),mismatches=bad),
        global_manifest=dict(records=len(records),sha256=hashlib.sha256(global_path.read_bytes()).hexdigest()),
        all_preserved=not bad and not any(c['mismatches'] for c in checks))
    assert result['all_preserved'],result
    assert len(records)==318
    if not check_only:
        assert not (HERE/'artifact-index.json').exists(),'Frozen dataset; use --check-only.'
        write_json(HERE/'preservation-audit.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='indices'}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--check-only',action='store_true');args=parser.parse_args()
    run(args.check_only)
