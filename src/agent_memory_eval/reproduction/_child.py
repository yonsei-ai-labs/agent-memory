"""Isolated, read-only entry point for a registered historical review.

Run as a file with the historical environment's Python -I -B. This bootstrap
uses only the standard library and must not import the current runtime package.
Python audit hooks prevent accidental I/O in trusted registered review scripts;
they are not an operating-system sandbox for hostile code.
"""

import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import runpy
import sys
import traceback


def main():
    worktree, script, report_path = (Path(p).absolute() for p in sys.argv[1:4])
    report = dict(status="failed", network_attempts=0, process_attempts=0,
                  protected_write_attempts=0, read_only_git_calls=[], imported_modules={})
    policy = json.loads(Path(sys.argv[4]).read_bytes()) if len(sys.argv) > 4 else {}
    git_checkouts = [Path(path).resolve() for path in policy.get("read_only_git_checkouts", [])]
    if any(not path.is_relative_to(worktree) for path in git_checkouts):
        raise RuntimeError("Registered Git input must be inside the historical worktree")

    def inside(path):
        if not isinstance(path, (str, bytes, os.PathLike)):
            return False
        return Path(os.fsdecode(path)).absolute().resolve().is_relative_to(worktree)

    def block(event, args):
        if (event.startswith("socket.") and event != "socket.__new__") or event == "urllib.Request":
            report["network_attempts"] += 1
            raise RuntimeError("Network access is disabled during historical review")
        if event in {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.fork"}:
            if event == "subprocess.Popen":
                command = args[1]
                if (isinstance(command, (list, tuple)) and len(command) == 5
                        and command[:2] == ["git", "-C"] and Path(command[2]).resolve() in git_checkouts
                        and command[3:] in (["rev-parse", "HEAD"], ["status", "--porcelain"])):
                    report["read_only_git_calls"].append(list(command))
                    return
            report["process_attempts"] += 1
            raise RuntimeError("Child processes are disabled during historical review")
        targets = ()
        if event == "open":
            if args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                targets = args[:1]
        elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.truncate", "os.utime"}:
            targets = args[:1]
        elif event in {"os.rename", "os.link", "os.symlink"}:
            targets = args[:2]
        if any(inside(path) for path in targets):
            report["protected_write_attempts"] += 1
            raise RuntimeError("Historical worktree inputs are read only")

    def check_imports():
        imports = {}
        for name, module in list(sys.modules.items()):
            if name == "agent_memory_eval" or name.startswith("agent_memory_eval."):
                path = Path(module.__file__).resolve()
                if not path.is_relative_to(worktree / "src/agent_memory_eval"):
                    raise RuntimeError("Historical imports escaped the fixed worktree: " + name)
                imports[name] = str(path.relative_to(worktree))
        report["imported_modules"] = imports

    try:
        if not sys.flags.isolated or not sys.dont_write_bytecode or sys.flags.optimize:
            raise RuntimeError("Historical review requires isolated Python with assertions enabled")
        if sys.prefix == sys.base_prefix or not Path(sys.prefix).absolute().is_relative_to(worktree / ".venv"):
            raise RuntimeError("Historical review requires its own worktree virtual environment")
        if any(os.environ.get(name) for name in ("OPENAI_API_KEY", "OPENROUTER_API_KEY")):
            raise RuntimeError("API role credentials must not reach a historical review")
        report.update(python=sys.version, executable=sys.executable, prefix=sys.prefix,
                      isolated=True, assertions_enabled=True, api_role_keys_present=False,
                      packages=sorted([dict(name=d.metadata["Name"], version=d.version)
                                       for d in importlib.metadata.distributions()], key=lambda d: d["name"].lower()),
                      bootstrap_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        # Imported before installing the hook so failure reports can still be written.
        sys.addaudithook(block)
        # Historical diagnostic scripts import neighboring review scripts.
        # Only the fixed worktree's script directory is made importable.
        sys.path.insert(0, str(script.parent))
        importlib.import_module("agent_memory_eval")
        check_imports()
        sys.argv = [str(script)]
        try:
            runpy.run_path(str(script), run_name="__main__")
        except SystemExit as error:
            if error.code not in (None, 0):
                raise
        check_imports()
        if any(report[key] for key in ("network_attempts", "process_attempts", "protected_write_attempts")):
            raise RuntimeError("A forbidden operation was attempted even if its exception was caught")
        report["status"] = "passed"
    except BaseException as error:
        report.update(error_type=type(error).__name__, error=str(error))
        traceback.print_exc()
    finally:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
