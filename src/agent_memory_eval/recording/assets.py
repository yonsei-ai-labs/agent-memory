"""Read pinned repository assets and publish immutable analysis files."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_memory_eval.contracts.core import ContractError, digest
from agent_memory_eval.recording.journal import safe_path


def asset_text(root, ref):
    data = safe_path(root, ref["path"]).read_bytes()
    if digest(data) != ref["sha256"]:
        raise ContractError("Pinned asset changed: " + ref["path"])
    return data.decode("utf-8")


def publish_json(path, result):
    """Publish complete bytes exclusively, including when independent reproductions race."""
    with TemporaryDirectory(dir=path.parent) as directory:
        provisional = Path(directory) / "evidence.json"
        with provisional.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(provisional, path)
