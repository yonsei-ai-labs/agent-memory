"""Copy an observed frozen snapshot without rewriting memories or replaying runs."""

from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, digest, new_id
from agent_memory_eval.contracts.validation import ROOT, inspect_journal, read_public_prefix
from agent_memory_eval.recording.journal import BlobStore, read_experiment


def validate_source(root, descriptor, scan, state, log_bytes, *, allow_empty=False):
    if (digest(log_bytes) != descriptor["log_sha256"] or scan["tail_bytes"] or scan["incomplete_runs"] or scan["pending_requests"]
            or any(row["experiment_id"] != descriptor["experiment_id"] for row in scan["records"])
            or any(event["record_kind"] != "observed" for event in scan["events"].values())):
        raise ContractError("Frozen source is changed, unfinished or not observed")
    snapshot = state["snapshots"][descriptor["snapshot_id"]]
    bank = state["banks"][snapshot["bank_id"]]
    if ((not snapshot["heads"] and not allow_empty) or not bank["sealed"]
            or bank["owner_run"] != descriptor["source_run_id"] or bank["heads"] != snapshot["heads"]):
        raise ContractError("Frozen source is empty, unsealed or belongs to another run")
    blobs = BlobStore(root)
    produced = {event["payload"]["version_id"]:event for event in scan["events"].values()
                if event["type"] == "memory.stored" and event["run_id"] == descriptor["source_run_id"]}
    for item,version in snapshot["heads"].items():
        event = produced[version]
        value = blobs.get(state["versions"][version])
        if value["id"] != item or value["version"] != version or digest(value["body"].encode()) != event["payload"]["body_sha256"]:
            raise ContractError("Frozen memory differs from its original produced version")
        read_public_prefix(Path(root),value["public_context_prefix"],scan["events"],event["event_seq"])
    return snapshot


def load_source(descriptor):
    root = (ROOT/descriptor["path"]).resolve()
    if not root.is_relative_to(ROOT):
        raise ContractError("Frozen source must be inside the repository workspace")
    scan,state,_ = read_experiment(root)
    data = (root/"log.jsonl").read_bytes()
    snapshot = validate_source(root,descriptor,scan,state,data)
    return dict(root=root,scan=scan,state=state,log_bytes=data,snapshot=snapshot)


def copy_source_blobs(root, journal):
    """Copy a content-addressed source once, retaining each original blob name."""
    count = 0
    for path in sorted((root/"blobs/sha256").iterdir()):
        if path.is_symlink() or not path.is_file():
            raise ContractError("Frozen source contains an unsafe blob path")
        ref = journal.blobs.put_bytes(path.read_bytes())
        if ref["sha256"] != path.name:
            raise ContractError("Frozen source blob name differs from its content")
        count += 1
    return count


def install_snapshot(memory, policy, heads, order, revision, versions, *, owner_run=None):
    """Give imported versions a sealed local bank without minting new item identities."""
    journal = memory.journal
    if len(order) != len(set(order)) or set(order) != set(heads):
        raise ContractError("Imported item order differs from the snapshot heads")
    for item in order:
        version = heads[item]
        ref = versions[version]
        value = journal.blobs.get(ref)
        if (value["id"], value["version"]) != (item, version):
            raise ContractError("Imported item differs from its immutable version")
        if version in journal.state["versions"] and journal.state["versions"][version] != ref:
            raise ContractError("Imported version ID collides with another immutable version")
    bank_id = memory.create_bank(owner_run or new_id("import"), policy)
    state = deepcopy(journal.state)
    state["versions"].update({v: versions[v] for v in heads.values()})
    bank = state["banks"][bank_id]
    bank.update(heads=dict(heads), start_heads=dict(heads), order=list(order), start_order=list(order), revision=revision)
    journal.append([],state=state)
    local_snapshot = memory.freeze(bank_id)
    memory.seal(bank_id)
    return local_snapshot


def import_source(memory, descriptor, policy):
    source = load_source(descriptor)
    journal = memory.journal
    count = copy_source_blobs(source["root"], journal)
    snapshot = source["snapshot"]
    local_snapshot = install_snapshot(memory, policy, snapshot["heads"], snapshot["order"],
                                      snapshot["head_revision"], source["state"]["versions"])
    finishes = [event["payload"] for event in source["scan"]["events"].values()
                if event["type"] == "model.request_finished" and event["run_id"] == descriptor["source_run_id"]]
    cost = sum((Decimal(str(event["cost_usd"])) for event in finishes if event["cost_status"] == "observed"),Decimal(0))
    return dict(requested=descriptor,source_log_ref=journal.blobs.put(source["log_bytes"].decode()),
        source_final_state_ref=source["scan"]["records"][-1]["state_ref"],
        local_snapshot_id=local_snapshot,copied_blob_count=count,
        source_generation_reported_cost_usd_decimal=str(cost),
        source_generation_unknown_cost=any(event["cost_status"] == "unknown" for event in finishes))


def audit_import(root, descriptor, archive):
    blobs = BlobStore(root)
    data = blobs.get(archive["source_log_ref"]).encode()
    scan = inspect_journal(data)
    for record in scan["records"]:
        blobs.verify(record)
    state = blobs.get(scan["records"][-1]["state_ref"])
    if archive["requested"] != descriptor or archive["source_final_state_ref"] != scan["records"][-1]["state_ref"]:
        raise ContractError("Imported source archive changed")
    return validate_source(root,descriptor,scan,state,data)
