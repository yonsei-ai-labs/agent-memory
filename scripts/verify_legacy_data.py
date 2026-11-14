"""Verify the frozen legacy dataset snapshot without importing legacy code."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path("artifacts/legacy-datasets-manifest.json")


def local_path(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if (
        not name
        or relative.is_absolute()
        or ".." in relative.parts
        or "\\" in name
        or relative.as_posix() != name
        or name == "."
    ):
        raise ValueError(f"Invalid archive path: {name}")
    result = root / name
    if not result.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Path outside snapshot: {name}")
    return result


def file_hash(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Expected regular file: {path}")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def selected_paths(root: Path, selection: dict) -> set[str]:
    """Reconstruct the declared source selection, excluding interpreter caches."""
    paths = set()
    for name in selection["directory_roots"]:
        directory = local_path(root, name)
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError(f"Missing or symlinked source directory: {name}")
        for base, directories, files in os.walk(directory, followlinks=False):
            directories[:] = sorted(
                value for value in directories
                if value not in selection["excluded_directory_names"]
            )
            for value in directories:
                if (Path(base) / value).is_symlink():
                    raise ValueError(f"Symlinked directory in source selection: {value}")
            for value in files:
                path = Path(base) / value
                if path.suffix not in selection["excluded_file_suffixes"]:
                    paths.add(path.relative_to(root).as_posix())
    for pattern in selection["file_globs"]:
        if PurePosixPath(pattern).is_absolute() or ".." in PurePosixPath(pattern).parts:
            raise ValueError(f"Invalid source glob: {pattern}")
        paths.update(
            path.relative_to(root).as_posix() for path in root.glob(pattern) if path.is_file()
        )
    paths.update(selection["individual_files"])
    return paths


def verify(root: Path, source_root: Path | None = None) -> dict:
    root = root.resolve()
    manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    if manifest["format"] != "agent-memory-legacy-data-archive/v1":
        raise ValueError("Unsupported legacy dataset manifest version")
    archive = local_path(root, manifest["archive_root"])
    if archive.is_symlink() or not archive.is_dir():
        raise ValueError("Missing or symlinked archive root")
    expected = set()
    roles = Counter()
    statuses = Counter()
    families = set()
    total_bytes = 0
    for entry in manifest["files"]:
        name = entry["source_path"]
        if name in expected:
            raise ValueError(f"Duplicate archived path: {name}")
        expected.add(name)
        if not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
            raise ValueError(f"Invalid SHA-256: {name}")
        path = local_path(archive, name)
        if file_hash(path) != entry["sha256"] or path.stat().st_size != entry["bytes"]:
            raise ValueError(f"Archive hash or size mismatch: {name}")
        roles[entry["role"]] += 1
        statuses[entry["source_git_status"]] += 1
        total_bytes += entry["bytes"]
        parts = PurePosixPath(name).parts
        if entry["role"] == "dataset" and len(parts) > 2:
            families.add(parts[1])
        if source_root is not None:
            source = local_path(source_root, name)
            if file_hash(source) != entry["sha256"]:
                raise ValueError(f"Current source differs from archived snapshot: {name}")

    actual = set()
    for path in archive.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Symlink in frozen archive: {path}")
        if path.is_file():
            actual.add(path.relative_to(archive).as_posix())
    if actual != expected:
        raise ValueError(
            f"Archive inventory mismatch: missing={sorted(expected - actual)}, "
            f"unlisted={sorted(actual - expected)}"
        )
    if selected_paths(archive, manifest["selection"]) != expected:
        raise ValueError("Archived paths disagree with the declared source selection")
    if source_root is not None:
        if selected_paths(source_root, manifest["selection"]) != expected:
            raise ValueError("Current source inventory differs from archived selection")
    calculated = {
        "files": len(expected),
        "bytes": total_bytes,
        "files_by_role": dict(sorted(roles.items())),
        "files_by_source_git_status": dict(sorted(statuses.items())),
        "dataset_families": len(families),
    }
    if calculated != manifest["summary"]:
        raise ValueError("Archive summary differs from file entries")
    return {
        "status": "passed",
        "scope": "legacy_archive_bytes_and_inventory",
        "snapshot_id": manifest["snapshot_id"],
        **calculated,
        "current_source_files_verified": len(expected) if source_root is not None else 0,
        "semantic_labels_or_historical_results_validated": False,
        "benchmark_or_model_calls": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root", type=Path,
        help="Also compare current source bytes and selection; the archive needs no source checkout.",
    )
    args = parser.parse_args()
    source_root = args.source_root.resolve() if args.source_root is not None else None
    print(json.dumps(verify(ROOT, source_root), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
