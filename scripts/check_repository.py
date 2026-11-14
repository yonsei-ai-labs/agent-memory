"""Read-only checks for local documentation, migration provenance, and scaffolding."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tomllib
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def local_path(name: str) -> Path:
    if Path(name).is_absolute():
        raise ValueError(f"Expected relative path: {name}")
    path = (ROOT / name).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError(f"Path outside repository: {name}")
    return path


def markdown_files() -> list[Path]:
    roots = [ROOT, *(ROOT / name for name in ("docs", "schemas", "protocols", "tests", "src", "artifacts"))]
    files = set(ROOT.glob("*.md"))
    for folder in roots[1:]:
        files.update(folder.rglob("*.md"))
    # Validate new archive guidance, while leaving historical Markdown unchanged.
    for name in ("archive/AGENTS.md", "archive/legacy-ai-memory/README.md"):
        path = ROOT / name
        if path.is_file():
            files.add(path)
    return sorted(files)


def check_markdown(path: Path) -> int:
    content = path.read_text(encoding="utf-8")
    if not content.endswith("\n"):
        raise ValueError(f"Missing final newline: {path.relative_to(ROOT)}")
    in_code = False
    columns = None
    links = 0
    for number, line in enumerate(content.splitlines(), 1):
        if line.rstrip() != line:
            raise ValueError(f"Trailing whitespace: {path.relative_to(ROOT)}:{number}")
        if line.startswith("```"):
            in_code = not in_code
            columns = None
            continue
        if in_code:
            continue
        if line.startswith("|"):
            width = len(re.split(r"(?<!\\)\|", line)) - 2
            if columns is not None and width != columns:
                raise ValueError(f"Table width mismatch: {path.relative_to(ROOT)}:{number}")
            columns = width
        else:
            columns = None
        for target in re.findall(r"\]\(([^)]+)\)", line):
            parsed = urlsplit(target.strip().strip("<>"))
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            destination = (path.parent / unquote(parsed.path)).resolve()
            if not destination.is_relative_to(ROOT) or not destination.exists():
                raise ValueError(f"Broken local link: {path.relative_to(ROOT)}:{number}: {target}")
            if parsed.fragment and destination.suffix == ".md":
                headings = set()
                for heading in destination.read_text(encoding="utf-8").splitlines():
                    if heading.startswith("#"):
                        text = heading.lstrip("#").strip().lower().replace("`", "")
                        headings.add(re.sub(r"[^\w\- ]", "", text).replace(" ", "-"))
                if unquote(parsed.fragment) not in headings:
                    raise ValueError(f"Unknown heading anchor: {destination.name}#{parsed.fragment}")
            links += 1
    if in_code:
        raise ValueError(f"Unclosed code fence: {path.relative_to(ROOT)}")
    return links


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-source-snapshot", type=Path)
    parser.add_argument("--check-import-hashes", action="store_true")
    args = parser.parse_args()
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    if config["project"]["name"] != "agent-memory-eval":
        raise ValueError("Unexpected project metadata")
    files = markdown_files()
    links = sum(check_markdown(path) for path in files)

    manifest = json.loads((ROOT / "artifacts" / "legacy-source-manifest.json").read_text())
    commit = manifest["source"]["commit"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Source must be an exact Git commit")
    source_paths = set()
    targets = set()
    verified_copies = 0
    source_checks = 0
    drift = set()
    for source in manifest["sources"]:
        name = source["path"]
        if name in source_paths or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"]):
            raise ValueError(f"Invalid source entry: {name}")
        source_paths.add(name)
        if args.verify_source_snapshot:
            data = subprocess.check_output(
                ["git", "show", f"{commit}:{name}"], cwd=args.verify_source_snapshot
            )
            if digest(data) != source["sha256"]:
                raise ValueError(f"Legacy source hash mismatch: {name}")
            source_checks += 1
        for target in source["targets"]:
            destination = local_path(target["path"])
            if not destination.is_file():
                raise ValueError(f"Missing migration destination: {target['path']}")
            current = digest(destination.read_bytes())
            targets.add(target["path"])
            if target["transformation"] == "verbatim":
                if source["sha256"] != target["sha256_at_migration"]:
                    raise ValueError(f"Invalid original copy hash: {target['path']}")
                if current == source["sha256"]:
                    verified_copies += 1
            if current != target["sha256_at_migration"]:
                if args.check_import_hashes:
                    raise ValueError(f"Changed since import: {target['path']}")
                drift.add(target["path"])

    literature = ROOT / "docs" / "references" / "literature"
    review = json.loads((literature / "review.json").read_text())
    with (literature / "papers.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != review["scope"]["papers"] or {row["id"] for row in rows} != set(review["paper_ids"]):
        raise ValueError("Literature CSV and original review scope disagree")

    feedback = json.loads((ROOT / "artifacts" / "experiment-handoff-import.json").read_text())
    if feedback["format"] != "agent-memory-experiment-feedback-import/v1":
        raise ValueError("Unsupported experiment feedback import format")
    imported = feedback["imported_document"]
    imported_bytes = local_path(imported["path"]).read_bytes()
    if digest(imported_bytes) != imported["sha256"] or len(imported_bytes) != imported["bytes"]:
        raise ValueError("Experiment feedback original copy mismatch")
    if (imported["sha256"], imported["bytes"]) != (feedback["source"]["sha256"], feedback["source"]["bytes"]):
        raise ValueError("Experiment feedback import differs from recorded source")
    for name in feedback["derived_documents"]:
        if not local_path(name).is_file():
            raise ValueError(f"Missing feedback review destination: {name}")

    print(json.dumps({
        "status": "passed",
        "scope": "repository_links_migration_and_scaffold",
        "markdown_files_checked": len(files),
        "local_links_checked": links,
        "source_entries": len(source_paths),
        "migrated_targets": len(targets),
        "verbatim_files_verified": verified_copies,
        "legacy_source_hashes_verified": source_checks,
        "original_literature_papers": len(rows),
        "experiment_feedback_original_copies_verified": 1,
        "files_changed_since_migration": sorted(drift),
        "benchmark_or_model_calls": 0,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
