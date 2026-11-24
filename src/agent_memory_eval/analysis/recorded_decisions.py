"""Read a sealed recording through its cutoff and project choices onto public business inputs."""

from collections import defaultdict

from agent_memory_eval.analysis.decision_attribution import Action, Step, fingerprint, public_history
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import pointer, validate_manifest
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.recording.journal import BlobStore
from agent_memory_eval.recording.provenance import exposure_payload

READS = {"memory_list", "memory_get"}


def require(condition, message):
    if not condition:
        raise ContractError(message)


def sealed_bytes(path, expected, size=None):
    with path.open("rb") as stream:
        data = stream.read() if size is None else stream.read(size)
    require((size is None or len(data) == size) and digest(data) == expected, f"Seal mismatch at {path}")
    return data


class RecordedDecisions:
    def __init__(self, folder, run, checksum, size=None):
        self.run = run
        self.folder = folder
        self.blobs = BlobStore(folder)
        data = sealed_bytes(folder / "log.jsonl", checksum, size)
        records = [read_json(line) for line in data.splitlines()]
        self.all_events = {e["event_id"]: e for record in records for e in record["events"]}
        self.events = {key: e for key, e in self.all_events.items() if e["run_id"] == run}
        starts = self.of_type("run.started")
        require(len(starts) == 1, f"Expected one start for {run}")
        self.start = starts[0]["payload"]
        self.state = self.blobs.get(records[-1]["state_ref"])
        snap = self.start["start_snapshot_id"]
        self.items = snapshot_items(self.blobs, self.state, snap) if snap else []
        self.versions = {(i["id"], i["version"]): i for i in self.items}
        self.starts = {e["payload"]["request_id"]: (e, record["state_ref"]) for record in records
                       for e in record["events"] if e["run_id"] == run and e["type"] == "model.request_started"}
        self.requests = [e["payload"] for e, _ in self.starts.values()]
        self.finishes = {e["payload"]["request_id"]: e["payload"] for e in self.of_type("model.request_finished")}
        self.spans = defaultdict(list)
        for e in self.of_type("memory.input_included"):
            self.spans[e["payload"]["request_id"]].append(e["payload"])
        self.returns = {}
        for e in self.of_type("memory.returned"):
            p = e["payload"]
            message = self.blobs.get(p["result_ref"])
            self.returns[message["tool_call_id"]] = (p["operation"], message)

    def of_type(self, kind):
        return [e for e in self.events.values() if e["type"] == kind]

    def reply(self, req):
        finish = self.finishes[req["request_id"]]
        require(finish["status"] == "response", "Missing response at an observed choice")
        return self.blobs.get(finish["response_ref"])["choices"][0]

    def visible(self, req, wire):
        rid = req["request_id"]
        start, state_ref = self.starts[rid]
        manifest = self.blobs.get(req["input_manifest_ref"])
        origin = self.blobs.get(state_ref)["provenance"][rid]
        sources = self.blobs.get(origin["sources_ref"])
        spans = validate_manifest(self.folder, manifest, start, sources, self.all_events, origin["approved_templates"])
        logged = self.spans[rid]
        require([exposure_payload(manifest, s, sources[s["source_id"]]) for s in spans] == logged,
                "Exposure journal differs from the submitted manifest")
        for span in spans:
            raw = pointer(wire, span["pointer"])[span["char_start"]:span["char_end"]]
            item = self.versions[span["item_id"], span["version_id"]]
            require(span["field"] in {"title", "body"} and raw == canonical(item[span["field"]]).decode()[1:-1]
                    and digest(raw.encode()) == span["rendered_sha256"], "Unverified field in decision input")
        return logged

    def business_history(self, messages):
        """Remove only issued private reads and their matching recorded returns. Keep mixed public output."""
        public, private = [], {}
        for msg in messages:
            if msg["role"] == "tool" and msg.get("tool_call_id") in private:
                cid = msg["tool_call_id"]
                require(self.returns.get(cid) == (private.pop(cid), msg), "Private tool result differs from journal")
                continue
            calls = msg.get("tool_calls") or []
            kept = []
            for call in calls:
                name = call["function"]["name"]
                if name in READS and call["id"] in self.returns:
                    private[call["id"]] = name.removeprefix("memory_")
                else:
                    kept.append(call)
            if len(kept) != len(calls):
                if not kept and not msg.get("content"):
                    continue
                msg = {**msg, "tool_calls": kept}
            public.append(msg)
        require(not private, "Missing private result in submitted history")
        return public


def observed_step(actor, req, wire, choice, controls, history, spans, *, private_names=()):
    msg = choice["message"]
    calls = tuple(Action.from_function(c["function"]) for c in msg.get("tool_calls") or []
                  if c["function"]["name"] not in private_names)
    # Include all wire settings and the complete system message, after exact treatment projection.
    settings = {k: v for k, v in wire.items() if k != "messages"}
    system = history[0] if history and history[0]["role"] == "system" else None
    return Step(actor, req["request_id"], fingerprint([controls, settings, system]), public_history(history),
                msg.get("content"), calls, frozenset(s["field"] for s in spans))
