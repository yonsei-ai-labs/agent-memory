"""Load a pinned repository asset without allowing path or symlink escapes."""

from agent_memory_eval.contracts.core import ContractError, digest
from agent_memory_eval.recording.journal import safe_path


def asset_text(root, ref):
    data = safe_path(root, ref["path"]).read_bytes()
    if digest(data) != ref["sha256"]:
        raise ContractError("Pinned asset changed: " + ref["path"])
    return data.decode("utf-8")
