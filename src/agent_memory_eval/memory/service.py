"""Versioned file memory with owned banks and fixed or live read views."""

from copy import deepcopy
from dataclasses import asdict, dataclass

from agent_memory_eval.contracts.core import (ContractError, MemoryFault, MemoryPolicy,
                                              RunContext, canonical, digest, new_id, read_json)
from agent_memory_eval.contracts.validation import read_public_prefix, validate_public_return
from agent_memory_eval.memory.context import snapshot_message


@dataclass(frozen=True)
class OperationResult:
    value: dict
    wire: dict
    result_ref: dict
    returned_event_id: str
    replayed: bool = False


class MemoryService:
    def __init__(self, journal, *, record_kind="illustrative"):
        self.journal = journal
        if record_kind not in {"illustrative", "observed"}:
            raise ContractError("Unknown memory record kind")
        self.record_kind = record_kind

    def _controller(self):
        return RunContext(self.journal.experiment_id, phase="seal", record_kind=self.record_kind)

    def _bank(self, state, bank_id):
        try:
            return state["banks"][bank_id]
        except (KeyError, TypeError):
            raise MemoryFault("ACCESS_DENIED") from None

    def create_bank(self, owner_run, policy, parent_snapshot=None):
        if not owner_run or not policy.enabled:
            raise ContractError("A memory bank needs an owner and enabled policy")
        with self.journal.lock:
            state = deepcopy(self.journal.state)
            parent = {"heads": {}, "order": []} if parent_snapshot is None else self._snapshot(state, parent_snapshot)
            bank_id = new_id("b")
            state["banks"][bank_id] = dict(owner_run=owner_run, policy=asdict(policy), sealed=False,
                parent_snapshot=parent_snapshot, revision=0, heads=deepcopy(parent["heads"]),
                order=list(parent["order"]), start_heads=deepcopy(parent["heads"]), start_order=list(parent["order"]))
            self.journal.append([], state=state)
            return bank_id

    def fork(self, snapshot, owner_run, policy):
        return self.create_bank(owner_run, policy, snapshot)

    def seal(self, bank_id):
        with self.journal.lock:
            state = deepcopy(self.journal.state)
            self._bank(state, bank_id)["sealed"] = True
            self.journal.append([], state=state)

    @staticmethod
    def _snapshot(state, snapshot_id):
        try:
            return state["snapshots"][snapshot_id]
        except KeyError:
            raise MemoryFault("NOT_FOUND") from None

    def _freeze(self, state, bank_id, heads, order, revision, parents):
        snapshot_id = new_id("snap")
        manifest = dict(snapshot_id=snapshot_id, heads=deepcopy(heads), order=list(order),
                        bank_id=bank_id, head_revision=revision, parent_snapshot_ids=list(parents))
        ref = self.journal.blobs.put(manifest)
        state["snapshots"][snapshot_id] = manifest
        event = self._controller().event("bank.snapshot_created", dict(bank_id=bank_id,
            snapshot_id=snapshot_id, head_revision=revision, parent_snapshot_ids=list(parents), manifest_ref=ref))
        self.journal.append([event], state=state)
        return snapshot_id

    def freeze(self, bank_id):
        with self.journal.lock:
            state = deepcopy(self.journal.state)
            bank = self._bank(state, bank_id)
            parents = [bank["parent_snapshot"]] if bank["parent_snapshot"] else []
            return self._freeze(state, bank_id, bank["heads"], bank["order"], bank["revision"], parents)

    def compose(self, snapshots):
        if not snapshots or len(set(snapshots)) != len(snapshots):
            raise ContractError("Compose needs an ordered, nonempty list of distinct snapshots")
        with self.journal.lock:
            state, heads, order = deepcopy(self.journal.state), {}, []
            for snapshot_id in snapshots:
                snapshot = self._snapshot(state, snapshot_id)
                for item in snapshot["order"]:
                    version = snapshot["heads"][item]
                    if item in heads and heads[item] != version:
                        raise MemoryFault("VERSION_CONFLICT")
                    if item not in heads:
                        order.append(item)
                        heads[item] = version
            return self._freeze(state, new_id("pool"), heads, order, 0, snapshots)

    def _version(self, state, version):
        return self.journal.blobs.get(state["versions"][version])

    def preload(self, ctx, bank_id):
        """Prepare every frozen item verbatim, without inventing a model tool call."""
        if self.journal.contract_version != "1.2" or ctx.phase != "solve":
            raise ContractError("Preload requires a v1.2 journal and the solve phase")
        with self.journal.lock:
            state = deepcopy(self.journal.state)
            bank = self._bank(state, bank_id)
            start = self.journal.scan["starts"].get(ctx.run_id)
            if (ctx.experiment_id != self.journal.experiment_id or bank["owner_run"] != ctx.run_id
                    or bank["sealed"] or start is None or start["payload"]["bank_id"] != bank_id):
                raise MemoryFault("ACCESS_DENIED")
            if not MemoryPolicy(**bank["policy"]).preloads:
                raise MemoryFault("POLICY_DENIED")
            prior = [e for e in self.journal.events.values() if e["run_id"] == ctx.run_id and e["type"] == "memory.preloaded"]
            if prior:
                if prior[0]["payload"]["bank_id"] != bank_id:
                    raise ContractError("Preload retry changed banks")
                return deepcopy(prior[0])
            if any(e["run_id"] == ctx.run_id and e["type"] == "model.request_started" for e in self.journal.events.values()):
                raise ContractError("Preload must precede every model request")
            snapshot_id = bank["parent_snapshot"]
            snapshot = self._snapshot(state, snapshot_id)
            if start["payload"]["start_snapshot_id"] != snapshot_id or bank["start_heads"] != snapshot["heads"]:
                raise ContractError("Preload bank does not match the starting snapshot")
            producer = next(e for e in self.journal.events.values() if e["type"] == "bank.snapshot_created"
                            and e["payload"]["snapshot_id"] == snapshot_id)
            items, versions = [], []
            for item in snapshot["order"]:
                version = snapshot["heads"][item]
                value = self._version(state, version)
                items.append({k: value[k] for k in ("id", "version", "title", "body")})
                versions.append(dict(item_id=item, version_id=version, artifact_ref=state["versions"][version]))
            content = canonical(dict(items=items)).decode()
            used = state["return_bytes"].get(ctx.run_id, 0)
            cap = bank["policy"]["max_return_bytes"]
            if cap is not None and used + len(content.encode()) > cap:
                raise MemoryFault("QUOTA_EXCEEDED")
            state["return_bytes"][ctx.run_id] = used + len(content.encode())
            event = ctx.at("prepare").event("memory.preloaded", dict(bank_id=bank_id, snapshot_id=snapshot_id,
                selector="all_snapshot_verbatim", snapshot_manifest_ref=producer["payload"]["manifest_ref"],
                public_message_ref=self.journal.blobs.put(snapshot_message(items)), versions=versions,
                items=[dict(item_id=m["id"], version_id=m["version"], fields=["title", "body"]) for m in items]),
                causes=[start["event_id"], producer["event_id"]])
            event["schema_version"] = "1.2"
            return self.journal.append([event], state=state)["events"][0]

    @staticmethod
    def _args(arguments, allowed, required):
        if not isinstance(arguments, dict) or set(arguments) - set(allowed) or not set(required) <= arguments.keys():
            raise MemoryFault("INVALID_ARGUMENT")

    def _store(self, state, bank, arguments, prefix, *, native_body=False):
        self._args(arguments, ("title", "body", "id", "expected_version"), ("title", "body"))
        title, body = arguments["title"], arguments["body"]
        if (not isinstance(title, str) or not title.strip() or not isinstance(body, str)
                or not (body if native_body else body.strip())):
            raise MemoryFault("INVALID_ARGUMENT")
        item, expected = arguments.get("id"), arguments.get("expected_version")
        if item is None:
            if expected is not None:
                raise MemoryFault("INVALID_ARGUMENT")
            item = new_id("m")
        elif not isinstance(item, str) or not isinstance(expected, str):
            raise MemoryFault("INVALID_ARGUMENT")
        elif item not in bank["heads"]:
            raise MemoryFault("NOT_FOUND")
        elif bank["heads"][item] != expected:
            raise MemoryFault("VERSION_CONFLICT")
        version = new_id("v")
        memory = dict(id=item, version=version, title=title, body=body,
                      parent_version_id=expected, public_context_prefix=prefix)
        heads = dict(bank["heads"], **{item: version})
        policy = bank["policy"]
        if policy["max_items"] is not None and len(heads) > policy["max_items"]:
            raise MemoryFault("QUOTA_EXCEEDED")
        size = len(canonical({"title": title, "body": body}))
        size += sum(len(canonical({k: self._version(state, v)[k] for k in ("title", "body")}))
                    for key, v in heads.items() if key != item)
        if policy["max_payload_bytes"] is not None and size > policy["max_payload_bytes"]:
            raise MemoryFault("QUOTA_EXCEEDED")
        ref = self.journal.blobs.put(memory)
        state["versions"][version] = ref
        bank["heads"], bank["revision"] = heads, bank["revision"] + 1
        if expected is None:
            bank["order"].append(item)
        return dict(status="ok", id=item, version=version), memory, ref

    def _read(self, state, bank_id, bank, name, arguments):
        live = bank["policy"]["same_episode_read"]
        heads, order = (bank["heads"], bank["order"]) if live else (bank["start_heads"], bank["start_order"])
        if name == "memory_get":
            self._args(arguments, ("id",), ("id",))
            item = arguments["id"]
            if not isinstance(item, str):
                raise MemoryFault("INVALID_ARGUMENT")
            if item not in heads:
                raise MemoryFault("NOT_FOUND")
            value = self._version(state, heads[item])
            return dict(status="ok", item={k: value[k] for k in ("id", "version", "title", "body")})
        self._args(arguments, ("cursor", "limit"), ())
        limit = arguments.get("limit")
        limit = bank["policy"]["page_size"] if limit is None else limit
        if type(limit) is not int or not 1 <= limit <= bank["policy"]["page_size"]:
            raise MemoryFault("INVALID_ARGUMENT")
        fingerprint = digest(canonical(dict(heads=heads, order=order)))
        cursor = arguments.get("cursor")
        offset = 0
        if cursor is not None:
            if not isinstance(cursor, str) or cursor not in state["cursors"]:
                raise MemoryFault("INVALID_ARGUMENT")
            prior = state["cursors"][cursor]
            if prior["bank_id"] != bank_id:
                raise MemoryFault("ACCESS_DENIED")
            if prior["view"] != fingerprint or prior["limit"] != limit:
                raise MemoryFault("STALE_CURSOR")
            offset = prior["offset"]
        items = [self._version(state, heads[item]) for item in order[offset:offset + limit]]
        next_cursor = None
        if offset + limit < len(order):
            next_cursor = new_id("cursor")
            state["cursors"][next_cursor] = dict(bank_id=bank_id, view=fingerprint,
                                                limit=limit, offset=offset + limit)
        return dict(status="ok", items=[{k: item[k] for k in ("id", "version", "title")} for item in items],
                    next_cursor=next_cursor)

    def _native_head(self, state, heads, label):
        if not heads:
            return None
        if len(heads) != 1:
            raise ContractError("A single-document native backend cannot bind a multi-item bank")
        version = self._version(state, next(iter(heads.values())))
        if version["title"] != label:
            raise ContractError("Native bank contains a different memory representation")
        return version

    def execute(self, ctx, bank_id, operation_id, name, arguments, *, call_id, prefix=None, denial=None, native=None):
        """Only infrastructure retries reuse operation_id; fresh model calls remain distinct."""
        if ctx.run_id is None or ctx.experiment_id != self.journal.experiment_id:
            raise ContractError("Memory operation needs its owning run")
        fingerprint = dict(run_id=ctx.run_id, session_id=ctx.session_id, phase=ctx.phase,
                           bank_id=bank_id, name=name, arguments=arguments, call_id=call_id, denial=denial)
        regular_tool = name
        if native is not None:
            if self.journal.contract_version != "1.3" or name not in native.tools:
                raise ContractError("Unsupported native memory contract or tool")
            regular_tool = native.tools[name]
            fingerprint["native_format"] = native.format
        request_hash = digest(canonical(fingerprint))
        with self.journal.lock:
            previous = self.journal.operations.get(operation_id)
            if previous is not None:
                op = previous["operation"]
                if op["request_sha256"] != request_hash:
                    raise ContractError("Operation ID reused with a different request")
                wire = self.journal.blobs.get(op["result_ref"])
                event = next(e for e in previous["events"] if e["type"] == "memory.returned")
                if native is not None:
                    for stored in (e["payload"] for e in previous["events"] if e["type"] == "memory.stored"):
                        current = self._bank(self.journal.state, bank_id)["heads"]
                        if current.get(stored["item_id"]) == stored["version_id"]:
                            prior = stored["parent_version_id"]
                            prior_body = self._version(self.journal.state, prior)["body"] if prior else ""
                            native.recover_committed(prior_body, self._version(self.journal.state, stored["version_id"])["body"])
                return OperationResult(read_json(wire["content"].encode()), wire, op["result_ref"], event["event_id"], True)
            state = deepcopy(self.journal.state)
            memory, body_ref, error = None, None, None
            annotations, new_body = [], None
            operation = "update" if name == "memory_store" and isinstance(arguments, dict) and arguments.get("id") else {
                "memory_store": "create", "memory_list": "list", "memory_get": "get"}.get(regular_tool)
            if operation is None:
                raise ContractError("Unsupported memory tool")
            try:
                bank = self._bank(state, bank_id)
                start = self.journal.scan["starts"].get(ctx.run_id)
                if (bank["owner_run"] != ctx.run_id or bank["sealed"] or start is None
                        or start["payload"]["bank_id"] != bank_id):
                    raise MemoryFault("ACCESS_DENIED")
                if denial or not MemoryPolicy(**bank["policy"]).permits(regular_tool, ctx.phase):
                    raise MemoryFault(denial or "POLICY_DENIED")
                if regular_tool == "memory_store":
                    prefix = prefix or self.journal.public_prefix(ctx)
                    if prefix["run_id"] != ctx.run_id:
                        raise MemoryFault("ACCESS_DENIED")
                    read_public_prefix(self.journal.root, prefix, self.journal.events, len(self.journal.events) + 1)
                if native is not None:
                    live = self._native_head(state, bank["heads"], native.format)
                    visible = live if bank["policy"]["same_episode_read"] else self._native_head(state, bank["start_heads"], native.format)
                    value, new_body = native.evaluate(name, arguments, live["body"] if live else "",
                                                      visible["body"] if visible else "")
                    if regular_tool == "memory_store" and live:
                        operation = "update"
                    if "error" not in value and new_body is not None and new_body != (live["body"] if live else ""):
                        change = dict(title=native.format, body=new_body)
                        if live:
                            change.update(id=live["id"], expected_version=live["version"])
                        _, memory, body_ref = self._store(state, bank, change, prefix, native_body=True)
                    if regular_tool == "memory_get" and "error" not in value:
                        if not visible or value.get("memory_content") != visible["body"]:
                            raise ContractError("Native read does not match its visible version")
                        annotations = [dict(item_id=visible["id"], version_id=visible["version"], fields=["body"])]
                elif name == "memory_store":
                    value, memory, body_ref = self._store(state, bank, arguments, prefix)
                else:
                    value = self._read(state, bank_id, bank, name, arguments)
                # All successful memory return content, including store ACK metadata, consumes this budget.
                size = len(canonical(value))
                used = state["return_bytes"].get(ctx.run_id, 0)
                cap = bank["policy"]["max_return_bytes"]
                if cap is not None and used + size > cap:
                    raise MemoryFault("QUOTA_EXCEEDED")
                state["return_bytes"][ctx.run_id] = used + size
            except MemoryFault as fault:
                state, memory, body_ref = deepcopy(self.journal.state), None, None
                annotations, new_body = [], None
                error = fault.code
                value = dict(status="error", error=dict(code=error))
            wire = dict(role="tool", tool_call_id=call_id, content=canonical(value).decode())
            ref = self.journal.blobs.put(wire)
            events = []
            if error:
                events.append(ctx.event("policy.denied", dict(code=error, tool=name)))
            if memory is not None:
                events.append(ctx.event("memory.stored", dict(bank_id=bank_id,
                    head_revision=state["banks"][bank_id]["revision"], operation_id=operation_id,
                    item_id=memory["id"], version_id=memory["version"], parent_version_id=memory["parent_version_id"],
                    title=memory["title"], body_ref=body_ref, body_sha256=digest(memory["body"].encode()),
                    public_context_prefix=prefix)))
            items = value.get("items", [value["item"]] if "item" in value else [])
            returned = ctx.event("memory.returned", dict(bank_id=bank_id, operation_id=operation_id,
                operation=operation, status=value["status"] if native is None else ("error" if "error" in value else "ok"), result_ref=ref,
                items=[dict(item_id=item["id"], version_id=item["version"],
                            fields=[field for field in ("title", "body") if field in item]) for item in items]),
                causes=[events[-1]["event_id"]] if events else [])
            if native is not None:
                returned["schema_version"] = "1.3"
                returned["payload"].update(native_format=native.format, native_tool=name, items=annotations)
                validate_public_return(wire, returned["payload"])
            row = self.journal.append(events + [returned], state=state,
                operation=dict(operation_id=operation_id, request_sha256=request_hash, result_ref=ref))
            if native is not None and new_body is not None and "error" not in value:
                native.commit(new_body)
            return OperationResult(value, wire, ref, row["events"][-1]["event_id"])
