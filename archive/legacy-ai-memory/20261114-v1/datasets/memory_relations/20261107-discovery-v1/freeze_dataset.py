"""Freeze this discovery version and its report/handoff; never overwrite an index."""
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[3]
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'experiments/swe_memory/scripts'))
from records import now,write_json


def main():
    target=HERE/'artifact-index.json'
    assert not target.exists(),'Already frozen; create a new version.'
    assert json.loads((HERE/'validation.json').read_text())['all_checks_passed']
    assert json.loads((HERE/'preservation-audit.json').read_text())['all_preserved']
    files=[p for p in HERE.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc']
    files += [ROOT/'docs/reports/2026-11-07-memory-relation-case-expansion.md',
              ROOT/'training/memory_selector/handoffs/2026-11-07-memory-relations.md']
    rows=[dict(path=str(p.relative_to(ROOT)),bytes=p.stat().st_size,
        sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in sorted(set(files))]
    write_json(target,dict(at=now(),model_api_calls=0,files=rows,
        exclusions=['artifact-index.json itself','__pycache__ and pyc','mutable root/training README and experiment-log navigation'],
        interpretation='Discovery evidence and data only. No new SWE actor efficacy experiment or neural training in this package.'))
    print(json.dumps(dict(frozen_files=len(rows),bytes=sum(r['bytes'] for r in rows),index=str(target.relative_to(ROOT)))))


if __name__=='__main__':
    main()
