"""Reuse verified goal-reader scores after an outcome-free display correction."""
import argparse
import json
from pathlib import Path
import shutil
import zipfile

from tokenizers import Tokenizer

from data_pipeline.memory_decision_dataset import digest, readl, write, write_rows
from data_pipeline.prepare_memory_state_screen import SOURCE, memory_text
from training.memory_selector.travel_state_protocol import current_messages
from training.memory_selector.worker_contract_protocol import signature


def rebuild(old_data, data, cached, output):
    if output.exists(): raise FileExistsError(output)
    for folder in (old_data, data, cached):
        for n, expected in json.loads((folder / 'manifest.json').read_text())['sha256'].items():
            if digest((folder / n).read_bytes()) != expected: raise ValueError('Changed input/cache')
    old = {c['state_id']: c for c in readl(old_data / 'cases.jsonl')}
    cases = readl(data / 'cases.jsonl')
    if set(old) != {c['state_id'] for c in cases}:
        raise ValueError('State inventory changed')
    for c in cases:
        if {k: v for k, v in c.items() if k != 'public_text'} != {k: v for k, v in old[c['state_id']].items() if k != 'public_text'}:
            raise ValueError('More than current display changed; cannot reuse scores')
    if (data / 'annotations.jsonl').read_bytes() != (old_data / 'annotations.jsonl').read_bytes():
        raise ValueError('Relation labels changed')
    with zipfile.ZipFile(SOURCE) as z:
        tokenizer = Tokenizer.from_str(z.read('artifacts/worker-tokenizer/tokenizer.json').decode())
    by_state = {c['state_id']: c for c in cases}
    decisions = readl(cached / 'decisions.jsonl'); jobs = {}
    for d in decisions:
        c = by_state[d['state_id']]; bank = {m['memory_id']: m for m in c['bank']}
        selected = [bank[i] for i in d['selected_memory_ids']]
        actual_tokens = len(tokenizer.encode(memory_text(selected), add_special_tokens=False).ids) if selected else 0
        if actual_tokens != d['memory_tokens']: raise ValueError('Delivered memory changed')
        messages = current_messages(c['public_text'], selected); jid = signature(messages)
        d['job_id'] = jid; jobs[jid] = {'job_id': jid, 'messages': messages}
    output.mkdir(parents=True)
    shutil.copyfile(cached / 'reader-scores.jsonl', output / 'reader-scores.jsonl')
    shutil.copyfile(cached / 'reader-provenance.json', output / 'cached-reader-provenance.json')
    provenance = json.loads((cached / 'reader-provenance.json').read_text())
    provenance['display_correction_reuse'] = {
        'states_verified': len(cases), 'all_selector_inputs_identical': True,
        'annotation_bytes_identical': True, 'new_encoder_calls': 0,
        'old_data_manifest_sha256': digest((old_data / 'manifest.json').read_bytes()),
        'corrected_data_manifest_sha256': digest((data / 'manifest.json').read_bytes()),
        'cached_preparation_manifest_sha256': digest((cached / 'manifest.json').read_bytes()),
        'rebuild_source_sha256': digest(Path(__file__).read_bytes())}
    write(output / 'reader-provenance.json', provenance)
    write_rows(output / 'decisions.jsonl', decisions)
    write_rows(output / 'inputs.jsonl', [jobs[k] for k in sorted(jobs)])
    result = {'decisions': len(decisions), 'distinct_worker_jobs': len(jobs),
              'reader_pairs': provenance['records_scored'], 'new_worker_calls': 0, 'new_encoder_calls': 0}
    write(output / 'summary.json', result)
    write(output / 'manifest.json', {'sha256': {p.name: digest(p.read_bytes()) for p in output.iterdir() if p.is_file()}})
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for k in ('old-data', 'data', 'cached', 'output'): p.add_argument('--' + k, type=Path, required=True)
    a = p.parse_args(); print(json.dumps(rebuild(a.old_data, a.data, a.cached, a.output), indent=2))
