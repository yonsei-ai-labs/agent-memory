"""Reconstruct the reviewed dataset from public source and checked metadata."""
import argparse
import json
from pathlib import Path
import shutil
from urllib.request import urlopen

from data_pipeline.memoryarena_travel import build, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, default=Path("datasets/memoryarena_travel/20261109-reference-v1"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads((args.metadata / "source-manifest.json").read_text())
    if digest(manifest["generator"]) != manifest["generator_sha256"]:
        raise ValueError("Generator changed; use the recorded version")
    if args.output.exists():
        raise FileExistsError(args.output)
    source = args.source
    if source is None:
        source = args.output.parent / "downloads" / "memoryarena-travel-da1a37c.jsonl"
        if not source.exists():
            with urlopen(manifest["source_url"], timeout=60) as response:
                payload = response.read(7_000_001)
            if len(payload) > 7_000_000:
                raise ValueError("Unexpected source size")
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(payload)
    if digest(source) != manifest["source_sha256"]:
        raise ValueError("Wrong public source")
    build(source, args.output)
    expected = json.loads((args.metadata / "artifact-index.json").read_text())
    reviewed_metadata = {"summary.json", "annotation-review.json"}
    for name, checksum in expected["sha256"].items():
        path = args.metadata / name if name in reviewed_metadata else args.output / name
        if digest(path) != checksum:
            raise ValueError("Reconstructed data differs: " + name)
    for name in reviewed_metadata | {"artifact-index.json"}:
        shutil.copyfile(args.metadata / name, args.output / name)
    print(json.dumps({"status": "reproduced", "output": str(args.output),
                      "source_index_sha256": digest(args.output / "artifact-index.json")}, indent=2))


if __name__ == "__main__":
    main()
