"""Capture an immutable implementation identity only from a clean checkout."""

from pathlib import Path
import subprocess

from agent_memory_eval.contracts.core import ContractError, digest


def implementation_identity(root):
    root = Path(root).resolve()
    def git(*args):
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)
        return result.stdout
    try:
        if Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve() != root:
            raise ContractError("Implementation root must be the repository root")
        head = git("rev-parse", "HEAD").decode().strip()
        if git("status", "--porcelain=v1", "--untracked-files=all"):
            raise ContractError("Commit all implementation, test, protocol and documentation changes before freezing")
        tree = git("rev-parse", "HEAD^{tree}").decode().strip()
        lock = root / "uv.lock"
        lock_ref = dict(path="uv.lock", sha256=digest(lock.read_bytes())) if lock.exists() else None
        if git("rev-parse", "HEAD").decode().strip() != head or git("status", "--porcelain=v1", "--untracked-files=all"):
            raise ContractError("Implementation changed while recording the freeze identity")
    except subprocess.CalledProcessError as error:
        raise ContractError("Cannot identify a committed Git implementation") from error
    return dict(format="agent-memory-implementation-freeze/v1", implementation_commit=head,
                implementation_tree=tree, worktree_has_uncommitted_implementation=False,
                lock_ref=lock_ref, reproduction_commit=None,
                scope="implementation_identity_only_not_a_paid_protocol_or_run_authorization")
