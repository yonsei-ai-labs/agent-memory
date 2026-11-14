"""Source-grouped, evidence-grounded scale study. Never infer utility labels."""
import argparse
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor
import csv
import json
from pathlib import Path
import re
import shutil

from data_pipeline.collect_relation_source_pilot import (
    PM_ROOT, MA_REVISION, PM_REVISION, download, digest, file_sha, readl, writel, write,
    literal, groups_for_questions, ordered,
)

ROOT = Path(__file__).resolve().parents[1]
INTAKE = ROOT / 'runs/memory-relation-intake-20261111-v1'
PILOT = ROOT / 'datasets/memory_source_pilot/20261111-v1'
WORK = ROOT / 'runs/memory-source-scale-20261111-v1'


def source_rows():
    source_manifest = {r['file']:r for r in json.loads((INTAKE/'source-files.json').read_text())['files']}
    for name in ('persona-train.csv','mem2act-qa.jsonl','mem2act-sessions.jsonl'):
        if file_sha(INTAKE/'raw'/name) != source_manifest[name]['sha256']:
            raise ValueError('Pinned input changed: '+name)
    csv.field_size_limit(5_000_000)
    with (INTAKE/'raw/persona-train.csv').open() as f:
        persona = [{**r, '_csv_row': i} for i,r in enumerate(csv.DictReader(f),2)]
    questions = readl(INTAKE/'raw/mem2act-qa.jsonl')
    sessions = readl(INTAKE/'raw/mem2act-sessions.jsonl')
    groups, _, index = groups_for_questions(questions, sessions, True)
    assignment = {r['source_group']:r['split'] for r in readl(PILOT/'source-reservation.jsonl')}
    return persona, questions, sessions, groups, index, assignment


def collect_train_histories(heldout=False):
    persona, _, _, _, _, assignment = source_rows()
    selected = {}
    for r in persona:
        if assignment.get('persona:'+r['persona_id']) in (('dev','test') if heldout else ('train',)): selected[r['persona_id']] = r
    dest = WORK/'raw'; dest.mkdir(parents=True,exist_ok=True)
    manifest_path = WORK/'persona-history-manifest.json'
    records = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    def fetch(r):
        path = r['chat_history_32k_link']; name=Path(path).name; target=dest/name
        if target.exists():
            if name not in records or file_sha(target)!=records[name]['sha256']:raise ValueError('Unverified existing history')
            return records[name]
        old=INTAKE/'raw'/name
        if old.exists():
            original=next(x for x in json.loads((INTAKE/'history-files.json').read_text())['files'] if x['file']==name)
            if file_sha(old)!=original['sha256']:raise ValueError('Pilot history changed')
            shutil.copy2(old,target);return original
        return download(PM_ROOT+path,target,8_000_000)
    with ThreadPoolExecutor(max_workers=4) as pool:
        for r in pool.map(fetch,selected.values()):
            records[r['file']]=r;write(manifest_path,records)
    return {'requested_personas':len(selected),'histories':len(records)}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['histories']);p.add_argument('--heldout',action='store_true');a=p.parse_args()
    print(json.dumps(collect_train_histories(a.heldout),indent=2))
