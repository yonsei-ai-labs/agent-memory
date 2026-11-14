"""Rebuild the frozen full-request dataset without a GPU or model calls."""
import argparse
import json
from pathlib import Path
import shutil
from urllib.request import urlopen

from data_pipeline.travel_retrieval import build
from evaluation.travel_retrieval_data import audit
from training.memory_selector.prepare_travel_retrieval import sha, write


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--metadata', type=Path, default=Path('datasets/travel_retrieval/20261109-full-request-v1'))
    p.add_argument('--source', type=Path)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    manifest = json.loads((a.metadata / 'source-manifest.json').read_text())
    for name, digest in manifest['generator_sha256'].items():
        if sha(name) != digest:
            raise ValueError('Changed generator dependency ' + name)
    if a.output.exists():
        raise FileExistsError(a.output)
    source = a.source or a.output.parent / 'downloads/memoryarena-travel-da1a37c.jsonl'
    if not source.exists() and a.source is None:
        with urlopen(manifest['source_url'], timeout=60) as response:
            payload = response.read(7_000_001)
        if len(payload) > 7_000_000:
            raise ValueError('Unexpected source size')
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(payload)
    if sha(source) != manifest['source_sha256']:
        raise ValueError('Source revision mismatch')
    build(source, a.output)
    write(a.output / 'data-audit.json', audit(a.output, source))
    shutil.copyfile(a.metadata / 'annotation-review.json', a.output / 'annotation-review.json')
    index = json.loads((a.metadata / 'artifact-index.json').read_text())
    for name, digest in index['sha256'].items():
        if sha(a.output / name) != digest:
            raise ValueError('Reconstructed artifact differs ' + name)
    shutil.copyfile(a.metadata / 'artifact-index.json', a.output / 'artifact-index.json')
    print(json.dumps({'status': 'reproduced', 'source_index_sha256': sha(a.output / 'artifact-index.json')}, indent=2))


if __name__ == '__main__':
    main()
