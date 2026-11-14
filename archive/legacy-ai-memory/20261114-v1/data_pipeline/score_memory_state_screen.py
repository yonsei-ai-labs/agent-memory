"""Freeze a goal-only reader baseline and worker messages without GPU allocation."""
import argparse
import json
import math
from pathlib import Path
import time
import zipfile

import numpy as np

from data_pipeline.memory_decision_dataset import ROOT, digest, readl, write, write_rows
from data_pipeline.prepare_memory_state_screen import SOURCE, memory_text
from training.memory_selector.travel_state_protocol import current_messages
from training.memory_selector.worker_contract_protocol import signature

MODEL = 'microsoft/harrier-oss-v1-0.6b'
REVISION = 'f9b9dc8d367d443f2479d27aa5d8d2850c0774ee'
READER = ROOT / 'training/memory_selector/data/travel-state-pilot-v2/reader'


def score(data, output):
    import torch
    from torch.nn import functional as F
    from transformers import AutoModel, AutoTokenizer
    from peft import PeftModel
    from tokenizers import Tokenizer
    if output.exists(): raise FileExistsError(output)
    manifest = json.loads((data / 'manifest.json').read_text())
    for name, expected in manifest['sha256'].items():
        if digest((data / name).read_bytes()) != expected: raise ValueError('Changed screen input')
    provenance = json.loads((data / 'source-provenance.json').read_text())
    for name, expected in provenance['frozen_retrieval_models'].items():
        if name.startswith('reader/') and digest((READER.parent / name).read_bytes()) != expected:
            raise ValueError('Reader checkpoint changed')
    with zipfile.ZipFile(SOURCE) as z:
        contract = json.loads(z.read('data/reader-contract.json'))
        worker_tokenizer = Tokenizer.from_str(z.read('artifacts/worker-tokenizer/tokenizer.json').decode())
    torch.set_num_threads(2)
    tokenizer = AutoTokenizer.from_pretrained(MODEL, revision=REVISION, local_files_only=True, padding_side='left')
    base = AutoModel.from_pretrained(MODEL, revision=REVISION, local_files_only=True, torch_dtype=torch.float32, attn_implementation='sdpa')
    model = PeftModel.from_pretrained(base, READER / 'adapter', is_trainable=False).eval().requires_grad_(False)
    with np.load(READER / 'head-weights.npz') as z:
        weights = {k: torch.from_numpy(z[k]) for k in z.files}
    cases = readl(data / 'cases.jsonl'); labels = {r['state_id']: r for r in readl(data / 'annotations.jsonl')}
    queries = {(c['family_id'], c['mode']): c for c in cases}
    output.mkdir(parents=True)
    started = time.monotonic(); scores = {}; lengths = {}; n = 0
    # CPU FP32 is recorded explicitly; this is the frozen old reader over all
    # candidates, not a claim of bit-identical replay of the old GPU hybrid search.
    with torch.inference_mode():
        for identity, case in sorted(queries.items()):
            by = []
            for start in range(0, len(case['bank']), 4):
                if time.monotonic() - started > 1800: raise TimeoutError('Bounded CPU retrieval budget')
                bank = case['bank'][start:start + 4]
                texts = ['Instruct: ' + contract['pair_instruction'] + '\nQuery: ' + case['goal'] + '\nPast record:\n' + m['text'] for m in bank]
                tokens = tokenizer(texts, padding=True, truncation=False, return_tensors='pt')
                if tokens['input_ids'].shape[1] > 2048: raise ValueError('Reader truncation forbidden')
                h = model(**tokens).last_hidden_state[:, -1].float()
                v = F.normalize(h, dim=-1)
                hidden = F.gelu(F.linear(v, weights['net.0.weight'], weights['net.0.bias']))
                p = F.linear(hidden, weights['net.2.weight'], weights['net.2.bias']).softmax(-1)[:, 1].numpy()
                if not np.isfinite(p).all(): raise ValueError('Nonfinite reader scores')
                by.extend({'memory_id': m['memory_id'], 'score': float(x)} for m, x in zip(bank, p)); n += len(bank)
            scores[identity] = sorted(by, key=lambda r: (-r['score'], r['memory_id']))
            print(f'Goal-only reader {len(scores)}/{len(queries)}; {n} record pairs', flush=True)
    jobs, decisions = {}, []
    for c in cases:
        lookup = {m['memory_id']: m for m in c['bank']}
        ranked = scores[c['family_id'], c['mode']]; picked = []
        for r in ranked:
            candidate = picked + [r['memory_id']]
            tokens = len(worker_tokenizer.encode(memory_text([lookup[i] for i in candidate]), add_special_tokens=False).ids)
            if len(candidate) <= c['max_records'] and tokens <= c['memory_token_budget']: picked = candidate
            if len(picked) == c['max_records']: break
        y = labels[c['state_id']]
        policies = {'none': [], 'required_evidence': y['required_memory_ids'],
                    'goal_reader': picked, 'counter_memory': y['counter_memory_ids']}
        for policy, ids in policies.items():
            selected = [lookup[i] for i in ids]
            length = len(worker_tokenizer.encode(memory_text(selected), add_special_tokens=False).ids) if ids else 0
            if len(ids) > c['max_records'] or length > c['memory_token_budget']:
                raise ValueError('Memory budget violation')
            messages = current_messages(c['public_text'], selected); jid = signature(messages)
            jobs[jid] = {'job_id': jid, 'messages': messages}
            decisions.append({'state_id': c['state_id'], 'policy': policy, 'job_id': jid,
                              'selected_memory_ids': ids, 'memory_tokens': length,
                              'all_required_evidence': set(y['required_memory_ids']) <= set(ids)})
    write_rows(output / 'reader-scores.jsonl', [{'family_id': k[0], 'mode': k[1], 'ranking': v} for k, v in sorted(scores.items())])
    write_rows(output / 'decisions.jsonl', decisions)
    write_rows(output / 'inputs.jsonl', [jobs[k] for k in sorted(jobs)])
    write(output / 'reader-provenance.json', {'model_id': MODEL, 'revision': REVISION, 'device': 'cpu', 'dtype': 'float32',
        'goal_only': True, 'candidate_pool': 'whole bank, no dense/BM25 prefilter', 'records_scored': n,
        'queries': len(queries), 'elapsed_seconds': time.monotonic() - started, 'new_training_updates': 0,
        'source_sha256': digest(Path(__file__).read_bytes()), 'data_manifest_sha256': digest((data / 'manifest.json').read_bytes()),
        'checkpoint_sha256': {str(p.relative_to(READER)): digest(p.read_bytes()) for p in READER.rglob('*') if p.is_file()}})
    result = {'decisions': len(decisions), 'distinct_worker_jobs': len(jobs), 'reader_pairs': n, 'new_worker_calls': 0}
    write(output / 'summary.json', result)
    write(output / 'manifest.json', {'sha256': {p.name: digest(p.read_bytes()) for p in output.iterdir() if p.is_file()}})
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for k in ('data', 'output'): p.add_argument('--' + k, type=Path, required=True)
    a = p.parse_args(); print(json.dumps(score(a.data, a.output), indent=2))
