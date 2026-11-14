"""Compatibility entry point for the read-only v1.1 contract checks."""

import json

from agent_memory_eval.contracts.validation import (
    FIXTURE, ROOT, SCHEMAS, artifact, canonical, check_source_memory, checked_path,
    digest, inspect_journal, leaves, pointer, read_json, read_public_prefix,
    record_hash, resolve_source, validate_batch, validate_fixture,
    validate_manifest, validate_public_return, validators, verify_references,
)


if __name__ == "__main__":
    print(json.dumps(validate_fixture(), ensure_ascii=False, indent=2))
