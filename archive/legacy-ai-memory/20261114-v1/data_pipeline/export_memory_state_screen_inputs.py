"""Export messages only, preserving local relation annotations separately."""
import argparse
import json
from pathlib import Path
import shutil

from data_pipeline.memory_decision_dataset import digest, write
from training.memory_selector.memory_state_screen_protocol import BASE_CONFIG, validate_inputs


def export(data, prepared, output):
    if output.exists(): raise FileExistsError(output)
    for folder in (data, prepared):
        manifest = json.loads((folder / 'manifest.json').read_text())
        for name, expected in manifest['sha256'].items():
            if digest((folder / name).read_bytes()) != expected: raise ValueError('Changed source input')
    provenance = json.loads((data / 'source-provenance.json').read_text())
    count = json.loads((prepared / 'summary.json').read_text())['distinct_worker_jobs']
    output.mkdir(parents=True)
    shutil.copyfile(prepared / 'inputs.jsonl', output / 'inputs.jsonl')
    write(output / 'config.json', {**BASE_CONFIG, 'main_jobs': count})
    write(output / 'worker-provenance.json', provenance['worker'])
    write(output / 'input-manifest.json', {'sha256': {n: digest((output / n).read_bytes()) for n in ('inputs.jsonl', 'config.json', 'worker-provenance.json')}})
    return {'worker_jobs': len(validate_inputs(output)), 'repeat_jobs': 2, 'gold_uploaded': False}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for k in ('data', 'prepared', 'output'): p.add_argument('--' + k, type=Path, required=True)
    a = p.parse_args(); print(json.dumps(export(a.data, a.prepared, a.output), indent=2))
