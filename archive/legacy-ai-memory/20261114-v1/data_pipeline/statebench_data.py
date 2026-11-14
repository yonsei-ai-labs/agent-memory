"""Train-only intake for a STATE-Bench memory-selection feasibility study.

No model calls, task definitions, test environments, or success labels are used.
The snapshot intentionally contains only public train trajectories and runtime
source/docs. A user-turn checkpoint is an observed prefix, not a resumable DB.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import re
import statistics
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "runs/statebench-preparation-20261112-v1"
REVISION = "5644b1838d96bc4483da29642d058ecaa6f80f7f"
REPOSITORY = "https://github.com/microsoft/STATE-Bench.git"
DOMAINS = ("customer_support", "shopping_assistant", "travel")
TRAIN_PREFIX = "datasets/train_task_trajectories/"
SALT = "statebench-development-groups-20261112-v1"
ERROR_STATUSES = {"error", "failed", "rejected", "not_found"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def writel(path, rows):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w") as f:
        for row in rows:
            f.write(canonical(row) + "\n")


def readl(path):
    with Path(path).open() as f:
        return [json.loads(line) for line in f if line.strip()]


def allowed_snapshot_path(path):
    return (
        path in {"README.md", "LICENSE", "pyproject.toml", "uv.lock", ".env.example"}
        or path.startswith(("docs/", TRAIN_PREFIX))
        or (path.startswith("state_bench/") and path.endswith(".py"))
        or path.startswith("state_bench/configs/")
    )


def acquire(work=WORK):
    work = Path(work)
    repo = work / "upstream"
    if not (repo / ".git").exists():
        work.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--filter=blob:none", "--no-checkout", REPOSITORY, str(repo)], check=True)
    paths = subprocess.check_output(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", REVISION], text=True
    ).splitlines()
    selected = [p for p in paths if allowed_snapshot_path(p)]
    raw = subprocess.check_output(["git", "-C", str(repo), "archive", REVISION, *selected])
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            dest = repo / member.name
            if not dest.resolve().is_relative_to(repo.resolve()) or member.name not in selected:
                raise ValueError("Unexpected archive entry")
            content = archive.extractfile(member).read()
            if dest.exists() and dest.read_bytes() != content:
                raise ValueError("Refusing to replace a changed source: " + member.name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
    manifest = {
        "repository": REPOSITORY, "revision": REVISION,
        "files": {p: sha(repo / p) for p in selected},
        "task_or_environment_json_materialized": False,
        "scope": "train trajectories, runtime code, public docs and protocol; no test task content",
    }
    write(work / "source-snapshot.json", manifest)
    return {"revision": REVISION, "files": len(selected)}


def validate_snapshot(work):
    work = Path(work)
    manifest = json.loads((work / "source-snapshot.json").read_text())
    if manifest["revision"] != REVISION:
        raise ValueError("Unexpected upstream revision")
    for path, expected in manifest["files"].items():
        if not allowed_snapshot_path(path) or sha(work / "upstream" / path) != expected:
            raise ValueError("Changed or disallowed source: " + path)
    return manifest


def explicit_error(result):
    """An observed tool signal, never a task-failure or harmful-memory label."""
    return isinstance(result, dict) and (
        bool(result.get("error")) or result.get("status") in ERROR_STATUSES
    )


def terminal_user(message):
    # Upstream domain check_termination uses substring membership, including
    # natural language followed by the completion marker.
    return message.get("role") == "user" and "[TASK_DONE]" in (message.get("content") or "")


def visible_message(message):
    # Copy an allow-list, not environment/runtime objects with grading fields.
    role = message.get("role")
    if role not in {"system", "user", "assistant"}:
        raise ValueError("Unexpected canonical transcript role")
    content = message.get("content") or ""
    if not isinstance(content, str):
        raise ValueError("Expected text content")
    out = {"role": role, "content": content}
    if message.get("tool_calls"):
        if role != "assistant":
            raise ValueError("Tool calls on a non-assistant message")
        out["tool_calls"] = []
        for call in message["tool_calls"]:
            if not isinstance(call.get("name"), str) or not isinstance(call.get("arguments"), dict) or "result" not in call:
                raise ValueError("Incomplete observed tool call")
            out["tool_calls"].append(deepcopy({k: call[k] for k in ("name", "arguments", "result")}))
    return out


def checkpoint(conversation, index):
    """Only complete earlier turns plus the current user message are visible.

    The saved assistant message merges several tool rounds and final prose.
    Splitting that message into earlier decision points would expose future
    conclusions. Consequently no within-message checkpoints are manufactured.
    """
    if not 0 <= index < len(conversation) or conversation[index]["role"] != "user":
        raise ValueError("A checkpoint must end on a user message")
    if any(terminal_user(m) for m in conversation[:index + 1]):
        raise ValueError("Terminal marker is not a decision")
    return [visible_message(m) for m in conversation[:index + 1]]


def initial_request(conversation):
    return next(m["content"] for m in conversation if m["role"] == "user" and not terminal_user(m))


def request_group(domain, conversation):
    # Conservative exact normalization only. This does not certify semantic
    # template independence; IDs, near-duplicates and families need review too.
    text = re.sub(r"\d+", "<N>", initial_request(conversation).casefold())
    text = " ".join(text.split())
    return digest(domain + "\n" + text)


def collect(work=WORK):
    work = Path(work)
    snapshot = validate_snapshot(work)
    sources = []
    for path in sorted(snapshot["files"]):
        if not path.startswith(TRAIN_PREFIX) or not path.endswith(".json"):
            continue
        obj = json.loads((work / "upstream" / path).read_text())
        if set(obj) != {"conversation"} or not obj["conversation"]:
            raise ValueError("Changed train schema: " + path)
        conv = [visible_message(m) for m in obj["conversation"]]
        domain = path.split("/")[2]
        if domain not in DOMAINS:
            raise ValueError("Unexpected domain")
        sources.append({"source_id": digest(path)[:20], "domain": domain, "path": path,
                        "sha256": snapshot["files"][path], "group_id": request_group(domain, conv),
                        "conversation": conv})
    if Counter(s["domain"] for s in sources) != Counter({d: 100 for d in DOMAINS}):
        raise ValueError("Expected 100 public train trajectories per domain")
    return sources


def prepare(work=WORK):
    work = Path(work)
    out = work / "prepared"
    if (out / "manifest.json").exists():
        raise FileExistsError("Frozen preparation exists; use verify")
    sources = collect(work)
    dev_groups = set()
    for domain in DOMAINS:
        groups = {s["group_id"] for s in sources if s["domain"] == domain}
        dev_groups.update(sorted(groups, key=lambda g: digest(SALT + g))[:20])
    inventory, checkpoints, candidates, policy_observations, audit = [], [], [], {}, {}
    for s in sources:
        split = "development" if s["group_id"] in dev_groups else "memory_source"
        conv = s["conversation"]
        role_counts, names, statuses = Counter(), Counter(), Counter()
        seen_calls, errors, repeated, decisions, policy_calls = set(), [], 0, 0, 0
        current_request = initial_request(conv)
        for mi, message in enumerate(conv):
            role_counts[message["role"]] += 1
            if message["role"] == "user" and not terminal_user(message):
                current_request = message["content"]
                prefix = checkpoint(conv, mi)
                checkpoints.append({"checkpoint_id": s["source_id"] + f"-u{mi}",
                    "source_id": s["source_id"], "domain": s["domain"], "group_id": s["group_id"],
                    "split": split, "message_index": mi, "visible_prefix": prefix,
                    "prefix_sha256": digest(canonical(prefix)), "resumable_environment": False,
                    "applicability_labels": None, "utility_labels": None})
                decisions += 1
            prior_calls = []
            for ci, call in enumerate(message.get("tool_calls", [])):
                names[call["name"]] += 1
                signature = canonical({k: call[k] for k in ("name", "arguments")})
                repeated += signature in seen_calls
                seen_calls.add(signature)
                if isinstance(call["result"], dict):
                    statuses[str(call["result"].get("status", "<absent>"))] += 1
                if explicit_error(call["result"]):
                    errors.append({"message_index": mi, "call_index": ci})
                # Text contains observations, not an inferred recommended action.
                text = ("Historical observation from a different task. Identifiers and values refer to that past task; "
                        "this is not a successful-action prescription.\nPast initial request: " + initial_request(conv)
                        + "\nPast current request: " + current_request
                        + "\nEarlier calls in the same saved turn: " + canonical(prior_calls)
                        + "\nObserved call and result: " + canonical(call))
                candidates.append({"memory_id": "m-" + digest(s["source_id"] + f"/{mi}/{ci}")[:20],
                    "source_id": s["source_id"], "source_group": s["group_id"], "domain": s["domain"],
                    "source_split": split, "kind": "observed_tool_transition", "text": text,
                    "provenance": {"path": s["path"], "sha256": s["sha256"], "message_index": mi, "call_index": ci},
                    "review_status": "unreviewed", "task_success": None, "utility_label": None})
                if call["name"] == "get_policies":
                    policy_calls += 1
                    key = digest(canonical([s["domain"], call["arguments"], call["result"]]))
                    p = policy_observations.setdefault(key, {"policy_id": key, "domain": s["domain"],
                        "arguments": call["arguments"], "result": call["result"], "sources": []})
                    p["sources"].append({"source_id": s["source_id"], "split": split, "message_index": mi, "call_index": ci})
                prior_calls.append(call)
        inventory.append({**{k: s[k] for k in ("source_id", "domain", "path", "sha256", "group_id")},
            "split": split, "messages": len(conv), "user_checkpoints": decisions,
            "tool_calls": sum(names.values()), "tools": dict(names), "statuses": dict(statuses),
            "explicit_error_calls": errors, "repeated_identical_calls": repeated,
            "policy_calls": policy_calls, "chars": len(canonical(conv)),
            "has_final_success_label": False, "date_redacted": "<REDACTED>" in canonical(conv)})
    for domain in DOMAINS:
        rows = [r for r in inventory if r["domain"] == domain]
        grouped = defaultdict(list)
        for r in rows:
            grouped[r["group_id"]].append(r["source_id"])
        audit[domain] = {
            "trajectories": len(rows), "development": sum(r["split"] == "development" for r in rows),
            "memory_source": sum(r["split"] == "memory_source" for r in rows),
            "user_checkpoints": sum(r["user_checkpoints"] for r in rows),
            "tool_calls": sum(r["tool_calls"] for r in rows),
            "explicit_error_calls": sum(len(r["explicit_error_calls"]) for r in rows),
            "trajectories_with_explicit_error": sum(bool(r["explicit_error_calls"]) for r in rows),
            "repeated_identical_calls": sum(r["repeated_identical_calls"] for r in rows),
            "policy_calls": sum(r["policy_calls"] for r in rows),
            "median_chars": statistics.median(r["chars"] for r in rows),
            "duplicate_normalized_initial_request_groups": [v for v in grouped.values() if len(v) > 1],
        }
    writel(out / "sources.jsonl", inventory)
    writel(out / "checkpoints.jsonl", checkpoints)
    writel(out / "memory-candidates.jsonl", candidates)
    writel(out / "policy-observations.jsonl", sorted(policy_observations.values(), key=lambda p: p["policy_id"]))
    write(out / "audit.json", {"by_domain": audit, "total_trajectories": len(inventory),
        "total_checkpoints": len(checkpoints), "total_tool_calls": len(candidates),
        "unique_observed_policy_payloads": len(policy_observations),
        "candidate_status": "unreviewed observations, not a deployed memory bank or supervised labels",
        "official_test_tasks_read": 0, "actual_utility_labels": 0, "paid_calls": 0,
        "duplicate_grouping_limit": "normalized initial requests only; no claim of semantic family independence",
        "repeated_call_limit": "repeat counts include legitimate state checks; not wasted-call labels"})
    write(out / "manifest.json", {"revision": REVISION, "split_salt": SALT,
        "snapshot_sha256": sha(work / "source-snapshot.json"),
        "files": {p.name: sha(p) for p in sorted(out.iterdir()) if p.is_file()},
        "script_sha256": sha(__file__), "labels": "none"})
    return verify(work)


def verify(work=WORK):
    work = Path(work)
    validate_snapshot(work)
    out = work / "prepared"
    manifest = json.loads((out / "manifest.json").read_text())
    if manifest["snapshot_sha256"] != sha(work / "source-snapshot.json"):
        raise ValueError("Changed snapshot manifest")
    for path, expected in manifest["files"].items():
        if sha(out / path) != expected:
            raise ValueError("Changed prepared artifact: " + path)
    raw = {s["source_id"]: s for s in collect(work)}
    rows = {r["source_id"]: r for r in readl(out / "sources.jsonl")}
    groups = defaultdict(set)
    for r in rows.values():
        groups[r["group_id"]].add(r["split"])
    if any(len(v) != 1 for v in groups.values()):
        raise ValueError("A source group crossed the development boundary")
    checkpoints = readl(out / "checkpoints.jsonl")
    for c in checkpoints:
        source = raw[c["source_id"]]
        expected = checkpoint(source["conversation"], c["message_index"])
        if c["visible_prefix"] != expected or c["prefix_sha256"] != digest(canonical(expected)):
            raise ValueError("Prefix changed or future information included")
        if c["split"] != rows[c["source_id"]]["split"] or c["utility_labels"] is not None:
            raise ValueError("Invalid split or fabricated utility label")
    candidates = readl(out / "memory-candidates.jsonl")
    if len({m["memory_id"] for m in candidates}) != len(candidates):
        raise ValueError("Duplicate memory ID")
    for m in candidates:
        p = m["provenance"]
        source = raw[m["source_id"]]
        call = source["conversation"][p["message_index"]]["tool_calls"][p["call_index"]]
        if not m["text"].endswith(canonical(call)) or m["utility_label"] is not None:
            raise ValueError("Candidate does not preserve the cited call")
        if m["source_split"] != rows[m["source_id"]]["split"]:
            raise ValueError("Wrong source split")
    result = {"status": "passed", "trajectories": len(rows), "checkpoints": len(checkpoints),
              "tool_observations": len(candidates), "official_test_content_read": False,
              "checks": ["source hashes", "frozen artifacts", "source-group boundary", "exact visible prefixes", "tool provenance", "no invented utility labels"]}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["acquire", "prepare", "verify"])
    parser.add_argument("--work", type=Path, default=WORK)
    args = parser.parse_args()
    print(json.dumps(globals()[args.command](args.work), indent=2))
