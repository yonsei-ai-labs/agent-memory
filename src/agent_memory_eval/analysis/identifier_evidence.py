"""Verified identifier occurrences in public experience, stored fields and decision-time evidence."""

from functools import lru_cache

from agent_memory_eval.analysis.claim_evidence import _storage_call
from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require
from agent_memory_eval.contracts.core import read_json
from agent_memory_eval.experiments.forms_write import _project_context


def message_citations(messages, pattern):
    found = []
    for i, msg in enumerate(messages):
        if msg["role"] == "system":
            continue
        locations = [(f"/messages/{i}/content", msg.get("content"))]
        for j, call in enumerate(msg.get("tool_calls") or []):
            locations.append((f"/messages/{i}/tool_calls/{j}/function/arguments", call["function"]["arguments"]))
        for path, text in locations:
            if isinstance(text, str):
                found.extend(dict(role=msg["role"], pointer=path, begin=m.start(), end=m.end(), quote=m.group())
                             for m in pattern.finditer(text))
    return found


def writer_lineage(roots):
    """Join a frozen worker bank to completed writers and to the exact public source their requests contained."""
    def journal(run, stage):
        candidates = []
        for root in roots:
            folder = root / run
            path = folder / (stage + ".json")
            if path.exists():
                receipt = read_json(path.read_bytes())
                if receipt["status"] in {"completed", "empty"}:
                    require(receipt["run"] == run, "Mismatched source/writer receipt")
                    candidates.append((folder, receipt))
        require(len(candidates) == 1, f"Ambiguous or missing sealed {stage} for {run}")
        folder, receipt = candidates[0]
        return RecordedDecisions(folder, run, receipt["journal_sha256"], receipt["journal_bytes"])

    @lru_cache(maxsize=160)
    def source(run):
        log = journal(run, "source")
        req = [r for r in log.requests if r["actor_role"] == "worker"][-1]
        return _project_context(log.blobs.get(req["request_ref"]), dict(choices=[log.reply(req)]))

    @lru_cache(maxsize=624)
    def writer(run):
        log = journal(run, "writer")
        parent, = log.start["source_run_ids"]
        experience = source(parent)
        requests = {r["request_id"]: r for r in log.requests if r["actor_role"] == "writer"}
        # Include voluntary-empty writers in the source coverage denominator.
        first = next(iter(requests.values()))
        supplied = read_json(log.blobs.get(first["request_ref"])["messages"][1]["content"].encode())
        require(supplied == experience, "Writer input differs from the original public experience")
        stored = {}
        for e in log.of_type("memory.stored"):
            p = e["payload"]
            version = log.blobs.get(p["body_ref"])
            finish, call = _storage_call(log.blobs, log.events.values(), e)
            req = requests[finish["request_id"]]
            args = read_json(call["function"]["arguments"].encode())
            require(call["function"]["name"] == "memory_store"
                    and (version["id"], version["version"]) == (p["item_id"], p["version_id"])
                    and all(args[k] == version[k] for k in ("title", "body"))
                    and read_json(log.blobs.get(req["request_ref"])["messages"][1]["content"].encode()) == experience,
                    "Stored field lacks its source-bound writer action")
            stored[version["id"], version["version"]] = (version, e["event_id"])
        end, = log.of_type("run.ended")
        frozen = log.state["snapshots"][end["payload"]["end_snapshot_id"]]
        heads = {(item, frozen["heads"][item]) for item in frozen["order"]}
        require(heads <= stored.keys(), "Frozen writer version has no observed storage action")
        return parent, experience["messages"], {key: stored[key] for key in heads}

    return writer


def trace_identifier(job, choice, writer, pattern):
    """Count only literal, version-specific evidence. Current list membership is not a target label."""
    versions, sources = {}, {}
    for run in job["writer_runs"]:
        parent, messages, stored = writer(run)
        if parent not in sources:
            sources[parent] = message_citations(messages, pattern)
        for key, (version, event) in stored.items():
            require(key not in versions, "Duplicate item/version across source writers")
            versions[key] = (version, run, event, parent)
    stored_hits, shown = [], []
    bank_keys = {(i["id"], i["version"]) for i in choice["items"]}
    for item in choice["items"]:
        key = (item["id"], item["version"])
        version, run, event, parent = versions[key]
        for field in ("title", "body"):
            require(item[field] == version[field], "Frozen field differs from writer storage")
            for m in pattern.finditer(item[field]):
                stored_hits.append(dict(item=key[0], version=key[1], writer=run, store_event=event, source_run=parent,
                                        field=field, begin=m.start(), end=m.end(), quote=m.group()))
    require(bank_keys == versions.keys(), "Decision bank differs from source writers' stored versions")
    for span in choice["visible"]:
        if any((hit["item"], hit["version"], hit["field"]) == (span["item_id"], span["version_id"], span["field"])
               for hit in stored_hits):
            evidence = {k: span[k] for k in ("request_id", "item_id", "version_id", "field", "pointer",
                                              "char_start", "char_end", "source")}
            parent = versions[span["item_id"], span["version_id"]][3]
            shown.append(dict(evidence, source_run=parent, identifier_in_own_source=bool(sources[parent])))
    # There is no decision-time current evidence for a capped or missing business response.
    current = message_citations(choice["public"], pattern) if choice["step"] else None
    return dict(sources=sources, stored=stored_hits, exposed=shown, current=current,
                focused=choice["focused"], request=choice["step"].request if choice["step"] else None)
