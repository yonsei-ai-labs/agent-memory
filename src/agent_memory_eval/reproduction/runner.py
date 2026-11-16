"""Run fixed historical reviews in disposable worktrees, without runtime imports.

The registry describes analysis commits separately from execution commits.
Only standard-library bootstrap code runs from the evolving main checkout.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time


REGISTRY_FORMAT = "agent-memory-historical-reproduction-registry/v1"
DEFAULT_REGISTRY = "protocols/reproductions/historical-v1.json"


class ReproductionError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _pairs(items):
    value = {}
    for key, child in items:
        if key in value:
            raise ReproductionError("Duplicate JSON key: " + key)
        value[key] = child
    return value


def read_json(data):
    return json.loads(data, object_pairs_hook=_pairs)


def write_json(path, value):
    data = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    temporary = path.with_suffix(path.suffix + ".pending")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def relative(name):
    if not isinstance(name, str) or not name:
        raise ReproductionError("An explicit relative artifact path is required")
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != name or name == ".":
        raise ReproductionError("Artifact path must be normalized and remain inside its root")
    return path


def checked_path(root, name):
    path = root / relative(name)
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ReproductionError("Symlink inputs are not supported: " + name)
        if part == root:
            break
    return path


def clean_environment():
    allowed = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME", "UV_CACHE_DIR")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update(UV_OFFLINE="1", UV_NO_PROGRESS="1", PYTHONDONTWRITEBYTECODE="1")
    return env


def git(repo, *args):
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
                            capture_output=True, env=clean_environment())
    if result.returncode:
        raise ReproductionError("Git command failed: " + result.stderr.decode(errors="replace").strip())
    return result.stdout


def commit(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        raise ReproductionError("A full immutable Git commit ID is required")
    return value


def reference(ref):
    relative(ref["path"])
    if not re.fullmatch(r"[0-9a-f]{64}", ref["sha256"]):
        raise ReproductionError("An artifact SHA-256 is required")
    return ref


def git_ref(repo, revision, ref):
    reference(ref)
    data = git(repo, "cat-file", "blob", revision + ":" + ref["path"])
    if sha(data) != ref["sha256"]:
        raise ReproductionError("Frozen Git blob mismatch: " + ref["path"])
    return data


def load_entry(repo, registry_path, experiment):
    repo = Path(repo).resolve()
    if Path(git(repo, "rev-parse", "--show-toplevel").decode().strip()).resolve() != repo:
        raise ReproductionError("The repository argument must be its Git root")
    registry_path = Path(registry_path).absolute()
    registry_data = registry_path.read_bytes()
    registry = read_json(registry_data)
    if registry["format"] != REGISTRY_FORMAT or registry["intent"] != "read_only_historical_review":
        raise ReproductionError("Unsupported reproduction registry")
    if experiment not in registry["experiments"]:
        raise ReproductionError("Unknown registered experiment: " + experiment)
    entry = registry["experiments"][experiment]
    revision = commit(entry["reproduction_commit"])
    implementation = commit(entry["implementation_commit"])
    for rev in (revision, implementation):
        if git(repo, "rev-parse", rev + "^{commit}").decode().strip() != rev:
            raise ReproductionError("Historical revision is not the requested commit")
    lock = read_json(git_ref(repo, revision, entry["protocol_lock_ref"]))
    refs = lock["implementation_files"] + [v for v in lock.values()
        if isinstance(v, dict) and {"path", "sha256"} <= v.keys()]
    for ref in lock["implementation_files"]:
        git_ref(repo, implementation, ref)
    for ref in refs:
        git_ref(repo, revision, ref)
    required = refs + [entry["protocol_lock_ref"], entry["review_script_ref"],
                      entry["environment"]["lock_ref"], entry["environment"]["project_ref"], *entry["expected_outputs"]]
    for ref in required:
        git_ref(repo, revision, ref)
    if not entry["review_script_ref"]["path"].startswith("scripts/") or not entry["review_script_ref"]["path"].endswith(".py"):
        raise ReproductionError("Historical entry must be a registered review script")
    environment = entry["environment"]
    if not re.fullmatch(r"3\.\d+", environment["python_version"]) or not isinstance(environment["extras"], list):
        raise ReproductionError("Explicit Python minor version and dependency extras are required")
    if any(not re.fullmatch(r"[a-zA-Z0-9_-]+", extra) for extra in environment["extras"]):
        raise ReproductionError("Invalid dependency extra")
    inputs = entry["inputs"]
    roots = [relative(row["path"]) for row in inputs]
    if not roots or any(a == b or a in b.parents or b in a.parents
                        for i, a in enumerate(roots) for b in roots[i + 1:]):
        raise ReproductionError("Inputs must be explicit and nonoverlapping")
    if not any(row["kind"] == "run" and row["path"] == lock["planned_output"] for row in inputs):
        raise ReproductionError("The historical experiment's raw root must be included")
    for row, root in zip(inputs, roots):
        if row["kind"] == "run":
            if root.parts[0] != "runs":
                raise ReproductionError("Historical runs must stay under runs/")
            reference(dict(path=row["path"] + "/log.jsonl", sha256=row["log_sha256"]))
        elif row["kind"] == "file":
            reference(row)
        else:
            raise ReproductionError("Unsupported input kind")
    return dict(repo=repo, entry=entry, experiment=experiment, required_refs=required,
                frozen_ref_count=len(refs), registry_ref=dict(path=str(registry_path), sha256=sha(registry_data)))


@contextmanager
def input_leases(base, entry):
    streams = []
    try:
        for row in entry["inputs"]:
            if row["kind"] != "run":
                continue
            path = checked_path(base, row["path"] + "/writer.lock")
            stream = path.open("rb")
            streams.append(stream)
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ReproductionError("A source run still has an active writer") from error
        yield
    finally:
        for stream in streams:
            stream.close()


def inventory_inputs(base, entry):
    rows = []
    for item in entry["inputs"]:
        path = checked_path(base, item["path"])
        if item["kind"] == "run":
            if not path.is_dir():
                raise ReproductionError("Missing raw run: " + item["path"])
            paths = sorted(path.rglob("*"))
        else:
            paths = [path]
        for child in paths:
            name = child.relative_to(base).as_posix()
            checked_path(base, name)
            mode = child.stat().st_mode
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise ReproductionError("Input is not a regular file: " + name)
            rows.append(dict(path=name, bytes=child.stat().st_size, sha256=file_hash(child)))
        expected_name = item["path"] + "/log.jsonl" if item["kind"] == "run" else item["path"]
        actual = next((row for row in rows if row["path"] == expected_name), None)
        expected = item.get("log_sha256", item.get("sha256"))
        if actual is None or actual["sha256"] != expected:
            raise ReproductionError("Historical input checksum mismatch: " + expected_name)
    return sorted(rows, key=lambda row: row["path"])


def preflight(config, inputs_root):
    base = Path(inputs_root).resolve()
    with input_leases(base, config["entry"]):
        rows = inventory_inputs(base, config["entry"])
    return dict(status="ready", experiment=config["experiment"],
                reproduction_commit=config["entry"]["reproduction_commit"],
                implementation_commit=config["entry"]["implementation_commit"],
                frozen_refs_verified=config["frozen_ref_count"], input_files=len(rows),
                input_bytes=sum(row["bytes"] for row in rows), benchmark_or_model_calls=0)


def stage_inputs(base, worktree, inventory):
    for row in inventory:
        original = checked_path(base, row["path"])
        target = checked_path(worktree, row["path"])
        if target.exists():
            raise ReproductionError("Staged input would overwrite a historical tracked file")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
        target.chmod(0o444)
        if target.samefile(original) or file_hash(target) != row["sha256"]:
            raise ReproductionError("Input copy is not independent or differs from the source")


def prepare_environment(worktree, output, environment):
    uv = shutil.which("uv")
    if uv is None:
        raise ReproductionError("uv is required to prepare the locked offline environment")
    command = [uv, "sync", "--frozen", "--offline", "--no-python-downloads", "--no-progress",
               "--python", environment["python_version"]]
    for extra in environment["extras"]:
        command.extend(["--extra", extra])
    with (output / "environment.log").open("wb") as log:
        result = subprocess.run(command, cwd=worktree, env=clean_environment(), stdout=log,
                                stderr=subprocess.STDOUT, timeout=180)
    if result.returncode:
        raise ReproductionError("Locked offline environment preparation failed; see environment.log")
    return dict(command=command, offline=True, extras=environment["extras"],
                uv_version=subprocess.check_output([uv, "--version"], env=clean_environment(), text=True).strip())


def verify_worktree(config, worktree):
    if git(worktree, "rev-parse", "HEAD").decode().strip() != config["entry"]["reproduction_commit"]:
        raise ReproductionError("Historical worktree HEAD changed")
    for ref in config["required_refs"]:
        path = checked_path(worktree, ref["path"])
        if not path.is_file() or file_hash(path) != ref["sha256"]:
            raise ReproductionError("Historical worktree file changed: " + ref["path"])
    changed = git(worktree, "diff", "--name-only", "--no-ext-diff", "HEAD").decode().splitlines()
    if changed:
        raise ReproductionError("Historical tracked files changed: " + ", ".join(changed))


def reproduce(config, inputs_root, output, *, keep_worktree=False, timeout_seconds=1800,
              environment_factory=None, progress=None):
    if timeout_seconds <= 0:
        raise ReproductionError("A positive review timeout is required")
    base = Path(inputs_root).resolve()
    output = Path(output).absolute()
    if output.exists() or output.resolve() != output:
        raise ReproductionError("Reproduction output must be a new directory without symlink traversal")
    if base == output or output in base.parents:
        raise ReproductionError("Reproduction output cannot contain its input root")
    for item in config["entry"]["inputs"]:
        root = base / item["path"]
        if output == root or root in output.parents or output in root.parents:
            raise ReproductionError("Reproduction output must not overlap source inputs")
    entry, worktree = config["entry"], output / "checkout"
    started = time.monotonic()
    with input_leases(base, entry):
        inventory = inventory_inputs(base, entry)
        output.mkdir(parents=True, exist_ok=False)
        receipt = dict(format="agent-memory-historical-reproduction-receipt/v1", status="running",
            experiment=config["experiment"], started_at_utc=now(), registry_ref=config["registry_ref"],
            implementation_commit=entry["implementation_commit"], reproduction_commit=entry["reproduction_commit"],
            protocol_lock_ref=entry["protocol_lock_ref"], review_script_ref=entry["review_script_ref"],
            expected_outputs=entry["expected_outputs"], environment_spec=entry["environment"],
            inputs_root=str(base), worktree=str(worktree), source_inputs_preserved=False,
            staged_inputs_preserved=False, historical_code_preserved=False, steps=[],
            requested_timeout_seconds=timeout_seconds, cleanup=dict(status="not_created"))
        manifest = output / "inputs-manifest.json"
        write_json(manifest, dict(format="agent-memory-reproduction-inputs/v1", files=inventory))
        receipt["input_manifest_ref"] = dict(path=manifest.name, sha256=file_hash(manifest))
        bootstrap = Path(__file__).with_name("_child.py")
        receipt["launcher_sources"] = [dict(path=p.name, sha256=file_hash(p)) for p in (Path(__file__), bootstrap)]
        receipt["launcher_head"] = git(config["repo"], "rev-parse", "HEAD").decode().strip()
        created, staged = False, False

        def step(name):
            receipt["steps"].append(dict(phase=name, timestamp_utc=now()))
            write_json(output / "receipt.json", receipt)
            if progress:
                progress(name)

        try:
            step("create_worktree")
            git(config["repo"], "worktree", "add", "--detach", str(worktree), entry["reproduction_commit"])
            created = True
            verify_worktree(config, worktree)
            step("copy_inputs")
            stage_inputs(base, worktree, inventory)
            staged = True
            step("prepare_environment")
            factory = environment_factory or prepare_environment
            receipt["environment"] = factory(worktree, output, entry["environment"])
            verify_worktree(config, worktree)
            python = worktree / ".venv/bin/python"
            command = [str(python), "-I", "-B", str(bootstrap), str(worktree),
                       str(worktree / entry["review_script_ref"]["path"]), str(output / "child-report.json")]
            receipt["command"] = command
            step("review")
            with (output / "review.stdout.log").open("wb") as stdout, (output / "review.stderr.log").open("wb") as stderr:
                result = subprocess.run(command, cwd=worktree, env=clean_environment(),
                                        stdout=stdout, stderr=stderr, timeout=timeout_seconds)
            receipt["returncode"] = result.returncode
            child = read_json((output / "child-report.json").read_bytes())
            receipt["child"] = child
            if result.returncode or child["status"] != "passed":
                raise ReproductionError("Historical review failed; see child-report.json and review.stderr.log")
            receipt["status"] = "passed"
        except BaseException as error:
            receipt.update(status="failed", error_type=type(error).__name__, error=str(error))
        finally:
            for key, action in (
                ("source_inputs_preserved", lambda: inventory_inputs(base, entry) == inventory),
                ("staged_inputs_preserved", lambda: staged and inventory_inputs(worktree, entry) == inventory),
                ("historical_code_preserved", lambda: created and (verify_worktree(config, worktree) is None)),
            ):
                try:
                    receipt[key] = bool(action())
                except Exception as error:
                    receipt.setdefault("verification_errors", []).append(dict(check=key, error=str(error)))
                if not receipt[key]:
                    receipt["status"] = "failed"
            if (output / "child-report.json").exists() and "child" not in receipt:
                receipt["child"] = read_json((output / "child-report.json").read_bytes())
            if created and not keep_worktree:
                try:
                    git(config["repo"], "worktree", "remove", "--force", str(worktree))
                    receipt["cleanup"] = dict(status="removed", input_copies_retained=False)
                except Exception as error:
                    receipt.update(status="failed", cleanup=dict(status="failed", error=str(error)))
            elif created:
                receipt["cleanup"] = dict(status="retained", input_copies_retained=True)
            receipt.update(finished_at_utc=now(), elapsed_seconds=time.monotonic() - started)
            receipt["evidence_files"] = [dict(path=p.name, sha256=file_hash(p), bytes=p.stat().st_size)
                for p in (manifest, output / "environment.log", output / "review.stdout.log",
                          output / "review.stderr.log", output / "child-report.json") if p.exists()]
            write_json(output / "receipt.json", receipt)
    return receipt
