"""Prepare two inference-only comparisons. Gold annotations NEVER select candidates."""
import argparse
from collections import defaultdict
from functools import lru_cache
import json
from pathlib import Path

from tokenizers import Tokenizer
from evaluation.travel_reference_data import bm25
from training.memory_selector.selector_validation_protocol import (
    CONFIG, QWEN, identity, readl, sha, write, writel, validate_inputs)

ROOT = Path(__file__).resolve().parents[1]
DATA_SHA = 'd6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442'


def split_turn(text, prefix, tokenizer, limit=256):
    """Lossless character spans, each including its date/role prefix in token budget."""
    start = 0
    spans = []
    while start < len(text):
        encoded = tokenizer.encode(prefix + text[start:], add_special_tokens=False)
        if len(encoded.ids) <= limit:
            spans.append((start, len(text), prefix + text[start:], len(encoded.ids)))
            break
        # Token offsets identify a character boundary without lossy token decoding.
        # Re-tokenize the final substring because a boundary may change the BPE.
        end = start + encoded.offsets[limit - 1][1] - len(prefix)
        end = min(end, len(text))
        while end > start:
            n = len(tokenizer.encode(prefix + text[start:end], add_special_tokens=False).ids)
            if n <= limit:
                break
            end -= 1
        if end <= start:
            raise ValueError('Prefix too long')
        spans.append((start, end, prefix + text[start:end], n))
        start = end
    return spans


def public_external(row):
    """Explicit allowlist. Session IDs, answer flags, answer text never enter search."""
    return {'question': row['question'], 'question_date': row['question_date'],
            'sessions': [[{'role': t['role'], 'content': t['content']} for t in s]
                         for s in row['haystack_sessions']], 'dates': row['haystack_dates']}


def build_external(public, tokenizer, cached_split=None):
    candidates = []
    for si, (date, session) in enumerate(zip(public['dates'], public['sessions'])):
        for ti, turn in enumerate(session):
            prefix = f'Date: {date}\nSpeaker: {turn["role"]}\n'
            spans = (cached_split or (lambda text, p: split_turn(text, p, tokenizer)))(turn['content'], prefix)
            for start, end, text, tokens in spans:
                candidates.append({'session': si, 'turn': ti, 'start': start, 'end': end,
                                   'text': text, 'tokens': tokens})
    scores = bm25(public['question'], [c['text'] for c in candidates])
    # Stable input-only ties; no answer IDs, labels or sufficiency information.
    order = sorted(range(len(candidates)), key=lambda i: (-scores[i], identity(candidates[i]['text']), i))
    selected = [dict(candidates[i], bm25_rank=rank + 1) for rank, i in enumerate(order[:CONFIG['external_pool']])]
    return selected, len(candidates)


def prepare(source, output, tokenizer_path):
    if output.exists():
        raise FileExistsError(output)
    if sha(source) != DATA_SHA:
        raise ValueError('Wrong external source')
    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    jobs, targets = [], []
    fresh = ROOT / 'datasets/memory_decisions/20261110-refresh-v1'
    examples = {r['example_id']: r for r in readl(fresh / 'examples.jsonl')}
    groups = defaultdict(list)
    for r in readl(fresh / 'all-supervision.jsonl'):
        groups[r['state_id']].append(r)
    diagnostic = set()
    strata = defaultdict(list)
    for sid, rows in groups.items():
        strata[rows[0]['condition']].append(sid)
    for ids in strata.values():
        diagnostic.update(sorted(ids, key=lambda x: identity('diagnostic:' + x))[:2])
    for sid, rows in sorted(groups.items()):
        jid = identity('travel:' + sid)
        nonempty = [r for r in rows if r['ordered_selected_memory_ids']]
        texts = [examples[r['action_id']]['texts']['goal_and_observations'] for r in nonempty]
        currents = [t.split('\nProposed memory bundle:\n', 1)[0] for t in texts]
        if len(set(currents)) != 1:
            raise ValueError('Inconsistent current state')
        current = currents[0].split('\nCurrent goal:\n', 1)[1]
        rows = sorted(rows, key=lambda r: identity(jid + ':' + r['action_id']))
        cs, mapping = [], {}
        for i, r in enumerate(rows):
            cid = f'C{i + 1}'
            text = (examples[r['action_id']]['texts']['goal_and_observations'].split('\nProposed memory bundle:\n', 1)[1]
                    if r['ordered_selected_memory_ids'] else '(No past records supplied.)')
            cs.append({'id': cid, 'text': text, 'tokens': r['delivered_memory_tokens']})
            mapping[cid] = r['action_id']
        jobs.append({'job_id': jid, 'track': 'travel', 'current': current, 'candidates': cs, 'diagnostic': sid in diagnostic})
        targets.append({'job_id': jid, 'track': 'travel', 'state_id': sid, 'candidate_actions': mapping,
                        'family_id': rows[0]['family_id'], 'condition': rows[0]['condition'],
                        'none_action': next(r['action_id'] for r in rows if not r['ordered_selected_memory_ids'])})
    rows = json.loads(source.read_text())
    strata = defaultdict(list)
    for r in rows:
        strata['abstention' if r['question_id'].endswith('_abs') else r['question_type']].append(r['question_id'])
    external_diagnostic = set()
    for key, ids in strata.items():
        external_diagnostic.update(sorted(ids, key=lambda x: identity('diagnostic:' + x))[:4 if key == 'abstention' else 2])
    @lru_cache(maxsize=50000)
    def cached_split(text, prefix):
        return split_turn(text, prefix, tokenizer, CONFIG['chunk_tokens'])
    for n, r in enumerate(rows):
        jid = identity('external:' + r['question_id'])
        selected, population = build_external(public_external(r), tokenizer, cached_split)
        selected.sort(key=lambda c: identity(jid + ':' + str(c['session']) + ':' + str(c['turn']) + ':' + str(c['start'])))
        cs, provenance = [], {}
        for i, c in enumerate(selected):
            cid = f'C{i + 1}'
            cs.append({'id': cid, 'text': c['text'], 'tokens': c['tokens']})
            provenance[cid] = {k: v for k, v in c.items() if k != 'text'}
        answer_turns = [{'session': si, 'turn': ti, 'length': len(t['content'])}
                        for si, s in enumerate(r['haystack_sessions']) for ti, t in enumerate(s) if t.get('has_answer')]
        gold_sessions = [i for i, sid in enumerate(r['haystack_session_ids']) if sid in r['answer_session_ids']]
        jobs.append({'job_id': jid, 'track': 'external', 'current': 'Question date: ' + r['question_date'] + '\nQuestion: ' + r['question'],
                     'candidates': cs, 'diagnostic': r['question_id'] in external_diagnostic})
        targets.append({'job_id': jid, 'track': 'external', 'source_id': r['question_id'], 'question_type': r['question_type'],
                        'abstention': r['question_id'].endswith('_abs'), 'answer_turns': answer_turns,
                        'gold_sessions': gold_sessions, 'candidates': provenance, 'history_chunks': population})
        if n % 50 == 0:
            print(f'Prepared external {n}/500', flush=True)
    output.mkdir(parents=True)
    writel(output / 'inputs.jsonl', jobs)
    # Kept LOCAL. The Colab upload allowlist excludes this file and all targets.
    writel(output / 'targets.jsonl', targets)
    write(output / 'config.json', CONFIG)
    write(output / 'input-manifest.json', {'sha256': {n: sha(output / n) for n in ('inputs.jsonl', 'config.json')}})
    write(output / 'provenance.json', {
        'source_sha256': sha(source), 'tokenizer_sha256': sha(tokenizer_path),
        'external_revision': '98d7416c24c778c2fee6e6f3006e7a073259d48f',
        'external_url': 'https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned',
        'local_targets_sha256': sha(output / 'targets.jsonl'),
        'travel_source_sha256': {n: sha(fresh / n) for n in ('examples.jsonl', 'all-supervision.jsonl', 'manifest.json')},
        'preparer_sha256': sha(__file__), 'jobs': len(jobs), 'diagnostic_jobs': sum(j['diagnostic'] for j in jobs),
        'chunking': 'Lossless nonoverlapping character spans, 256 Qwen tokens including date/role; full annotated-turn coverage requires every character.',
        'candidate_selection': 'BM25 from public question and all date/role/content chunks, top8; no gold injection.',
        'gold_scope': 'Session recall is coarse; annotated answer-turn coverage is separate, not proof of semantic sufficiency or official QA success.'})
    return {'jobs': len(validate_inputs(output)), 'diagnostics': sum(j['diagnostic'] for j in jobs)}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--tokenizer', type=Path, required=True)
    a = p.parse_args()
    print(prepare(a.source, a.output, a.tokenizer))
