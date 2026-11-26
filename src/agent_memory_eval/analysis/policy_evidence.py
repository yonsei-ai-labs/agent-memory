"""Read policy outcomes and public literal evidence for every registered trajectory."""

from collections import Counter, defaultdict
from decimal import Decimal
from functools import lru_cache
import re

from agent_memory_eval.analysis.action_grounding import argument_features
from agent_memory_eval.analysis.clustered_outcomes import clustered_contrast
from agent_memory_eval.analysis.decision_attribution import Action
from agent_memory_eval.analysis.decision_cues import _message_field, writer_material
from agent_memory_eval.analysis.efficiency import paired_effort
from agent_memory_eval.analysis.recorded_decisions import READS, require
from agent_memory_eval.analysis.trajectory import recorded_effort, total_effort
from agent_memory_eval.contracts.core import canonical, digest
from agent_memory_eval.contracts.validation import pointer
from agent_memory_eval.memory.preparation import snapshot_items


# Harvest only public business fields. Call IDs, grading criteria and transport metadata are never candidates.
ENTITY = re.compile(r"(?<![\w-])[A-Za-z][A-Za-z0-9_-]{3,}(?![\w-])")
FIELDS = ("source", "guidance", "stored", "current", "exposed", "action")


def public_fields(messages):
    for msg in messages:
        if isinstance(msg.get("content"), str):
            yield msg["content"]
        for call in msg.get("tool_calls") or []:
            fn = call.get("function", call)
            yield fn["name"]
            action = Action.from_function(fn).record()
            for _, _, value in argument_features(action.get("arguments")):
                yield value


def candidates(messages):
    found = set()
    for msg in messages:
        for call in msg.get("tool_calls") or []:
            fn = call.get("function", call)
            found.add(fn["name"])
            found.update(
                value for _, _, value in argument_features(Action.from_function(fn).record().get("arguments"))
            )
        for token in ENTITY.findall(msg.get("content") or ""):
            if any(c.isdigit() or c == "_" for c in token) or re.search(r"[a-z][A-Z]", token):
                found.add(token)
    return found


def exact_identifiers(messages):
    names = set()
    for msg in messages:
        for call in msg.get("tool_calls") or []:
            fn = call.get("function", call)
            names.add(fn["name"])
            for path, kind, value in argument_features(Action.from_function(fn).record().get("arguments")):
                if kind == "embedded_identifier" or re.search(r"(?:id|number|code)(?:/\d+)?$", path, re.IGNORECASE):
                    names.add(value)
    return names


@lru_cache(maxsize=4096)
def _patterns(term):
    # Identifiers containing digits or punctuation keep their spelling and delimiters.
    def bounded(pattern):
        return r"(?<![\w-])" + pattern + r"(?![\w-])"

    exact = re.compile(bounded(re.escape(term)))
    folded = None
    if not term.isupper() and all(c.isalpha() or c.isspace() for c in term):
        folded = re.compile(bounded(r"\s*".join(re.escape(c) for c in term if not c.isspace())), re.IGNORECASE)
    return exact, folded


class TextFields:
    def __init__(self, values):
        self.text = "\0".join(values)
        self.folded = "".join(self.text.split()).casefold()

    def match(self, term, *, allow_normalized=True):
        exact, folded = _patterns(term)
        if term in self.text and exact.search(self.text):
            return "exact"
        if (
            allow_normalized
            and folded
            and "".join(term.split()).casefold() in self.folded
            and folded.search(self.text)
        ):
            return "normalized_only"
        return "absent"


def current_fields(log, request, wire):
    manifest = log.blobs.get(request["input_manifest_ref"])
    catalog = log.blobs.get(log.state["provenance"][request["request_id"]]["sources_ref"])
    for binding in manifest["bindings"]:
        origin = catalog[binding["source_id"]]["kind"]
        if origin in {"memory_snapshot", "memory_return"}:
            continue
        path = binding["pointer"]
        value = pointer(wire, path)
        if path.startswith("/messages/"):
            subpath = path.split("/")[3:]
            if not subpath:
                yield from public_fields([value])
            elif _message_field(subpath) and isinstance(value, str):
                yield value
        elif path == "/tools" or path.startswith("/tools/"):
            yield canonical(value).decode()


def complete_tokens(effort):
    if effort and effort["dispatched"] == effort["tokens_reported_on"]:
        return effort["reported_tokens"]
    return None


def matches(term, fields, identifiers):
    return {
        k: f.match(term, allow_normalized=term not in identifiers) if f is not None else None
        for k, f in fields.items()
    }


def counted(value):
    return value in {"exact", "normalized_only"}


class PolicyEvidence:
    def __init__(self):
        self.sources = {}
        self.writers = {}
        self.readers = {}
        self.cost = None
        self.cluster_unit = "registered_cluster"

    def source(self, task, messages):
        self.sources[task] = (
            candidates(messages),
            TextFields(public_fields(messages)),
            exact_identifiers(messages),
        )

    def writer(self, task, form, condition, status, log=None, *, checksum=None, reused=0, draw=1, reason=None):
        record = dict(
            task=task,
            form=form,
            condition=condition,
            status=status,
            run=None,
            effort=None,
            request_sha256=None,
            journal_sha256=checksum,
            public_contexts=None,
            instruction_sha256=None,
            feedback_level=None,
            writer_draw=draw,
            reason=reason,
        )
        guidance = None
        if log:
            record.update(run=log.run, effort=recorded_effort(log, reused_local_generations=reused))
            if log.requests and log.finishes.get(log.requests[0]["request_id"], {}).get("status") == "response":
                request = log.requests[0]
                event = log.starts[request["request_id"]][0]
                instruction, origins, feedback = writer_material(log.folder, log.blobs, log.scan, log.state, event)
                require(
                    log.start["source_run_ids"] == [v["run"] for v in origins],
                    "Writer has different public sources",
                )
                record.update(
                    request_sha256=request["request_ref"]["sha256"],
                    instruction_sha256=digest(instruction.encode()),
                    feedback_level=feedback["level"],
                    public_contexts=[{k: v for k, v in item.items() if k != "context"} for item in origins],
                )
                guidance = TextFields([instruction, canonical(feedback).decode()])
        source = self.sources.get(task)
        ends = log.of_type("run.ended") if log else []
        record["source_cues"] = None
        if ends:
            snapshot = ends[0]["payload"]["end_snapshot_id"]
            items = snapshot_items(log.blobs, log.state, snapshot)
            record.update(snapshot=snapshot, versions=[dict(item=i["id"], version=i["version"]) for i in items])
        if ends and source:
            stored = TextFields(i[k] for i in items for k in ("title", "body"))
            record["source_cues"] = [
                dict(term=t, **matches(t, dict(stored=stored, guidance=guidance), source[2]))
                for t in sorted(source[0])
            ]
        key = log.run if log else ("unobserved", len(self.writers))
        require(key not in self.writers, "Repeated writer in policy evidence")
        self.writers[key] = record, guidance

    def decision(self, row, log):
        business_calls = 0
        for request in log.requests:
            if request["actor_role"] != "worker":
                continue
            finish = log.finishes.get(request["request_id"])
            if finish and finish["status"] == "response":
                choice = log.reply(request)
                if choice["finish_reason"] != "length":
                    business_calls += sum(
                        c["function"]["name"] not in READS for c in choice["message"].get("tool_calls") or []
                    )
        evidence = dict(business_calls=business_calls, cues=None)
        self.readers[log.run] = evidence
        decision = row["decision"]
        if decision is None:
            return
        request = log.starts[decision["request_id"]][0]["payload"]
        wire = log.blobs.get(request["request_ref"])
        message = log.reply(request)["message"]
        source = self.sources.get(row["task"])
        writer = self.writers.get(row["writer_run"])
        terms = candidates([message]) | (source[0] if source else set())
        identifiers = exact_identifiers([message]) | (source[2] if source else set())
        visible = [log.versions[v["item"], v["version"]][v["field"]] for v in decision["memory"]]
        fields = dict(
            source=source[1] if source else None,
            guidance=writer[1] if writer else None,
            stored=TextFields(i[k] for i in log.items for k in ("title", "body")),
            current=TextFields(current_fields(log, request, wire)),
            exposed=TextFields(visible),
            action=TextFields(public_fields([message])),
        )
        evidence["cues"] = [
            dict(
                term=term,
                source_candidate=term in source[0] if source else None,
                **matches(term, fields, identifiers),
            )
            for term in sorted(terms)
        ]

    def verify_policy(self, expected, mode, rows):
        """Check the actual submitted writer prompt and the bank assigned to every observed reader."""
        for writer, _ in self.writers.values():
            if writer["instruction_sha256"] is not None:
                key = writer["form"], writer["condition"]
                require(
                    writer["instruction_sha256"] == expected[key] and writer["feedback_level"] == "result_only",
                    "Submitted writer instruction or feedback differs from the common policy",
                )
        for row in rows:
            if row["phase"] != "solve":
                continue
            require(row["delivery"] == mode, "Different registered delivery")
            if row["decision"] is None:
                continue
            writer, _ = self.writers[row["writer_run"]]
            require(
                writer["instruction_sha256"] is not None
                and [p["run"] for p in writer["public_contexts"]] == [row["source_run"]]
                and len(writer["versions"]) == 1,
                "Reader did not receive its required single-bank policy",
            )
            if mode == "preload":
                (item,) = writer["versions"]
                require(
                    row["decision"]["memory"] == [dict(item, field=f) for f in ("body", "title")],
                    "Required preload did not reach the first public decision",
                )
            elif row["decision"]["memory"]:
                returns = row["read_returns"] or {}
                fields = {v["field"] for v in row["decision"]["memory"]}
                require(
                    (returns.get("memory_list") or returns.get("memory_get"))
                    and ("body" not in fields or returns.get("memory_get")),
                    "On-demand memory exposure has no corresponding private return",
                )

    def report(self, trajectory, *, summary=False):
        groups = defaultdict(list)
        details = []
        for row in trajectory["observations"]:
            obs = self.readers.get(row["run"], {})
            detail = dict(row, cues=obs.get("cues"), business_calls=obs.get("business_calls"))
            details.append(detail)
            groups[row["phase"], row["condition"]].append(detail)
        sources = {r["task"]: r for r in details if r["phase"] == "source"}
        tables = [
            condition_summary(phase, condition, group, sources, self.sources)
            for (phase, condition), group in sorted(groups.items())
        ]
        comparisons = policy_comparisons(groups, self.cluster_unit)
        writers = [v[0] for v in self.writers.values()]
        writer_paths = [c for w in writers for c in w["source_cues"] or []]
        result = dict(
            format="agent-memory-policy-evidence/v1",
            benchmark=trajectory["benchmark"],
            scope=trajectory["scope"],
            observation_count=len(details),
            bindings=trajectory["bindings"],
            trajectory_sha256=trajectory["observations_sha256"],
            observations_sha256=digest(canonical(details)),
            writers_sha256=digest(canonical(writers)),
            by_condition=tables,
            policy_comparisons=comparisons,
            writers=dict(
                assigned=len(writers),
                observed=sum(w["effort"] is not None for w in writers),
                sealed_nonempty=sum(bool(w.get("versions")) for w in writers),
                source_candidate_occurrences=len(writer_paths),
                stored_matches=sum(counted(c["stored"]) for c in writer_paths),
                effort=total_effort(writers),
            ),
            study_cost=self.cost,
            same_input_repetitions=trajectory["same_input_repetitions"],
        )
        if not summary:
            result.update(observations=details, writer_allocations=writers)
        return result


def effort_pair(candidate, reference):
    def measures(row):
        return dict(tokens=complete_tokens(row["effort"]), business_calls=row["business_calls"])

    return candidate["official_success"], reference["official_success"], measures(candidate), measures(reference)


def literal_counts(rows):
    stages = {key: Counter() for key in FIELDS}
    routes = dict.fromkeys(
        (
            "source_candidates",
            "also_stored",
            "also_exposed",
            "in_action_and_current",
            "in_action_without_current_match",
            "exposed_without_action_match",
        ),
        0,
    )
    for row in rows:
        for cue in row["cues"] or []:
            for key in FIELDS:
                stages[key][cue[key] or "unobserved"] += 1
            if not cue["source_candidate"]:
                continue
            routes["source_candidates"] += 1
            if not counted(cue["stored"]):
                continue
            routes["also_stored"] += 1
            if not counted(cue["exposed"]):
                continue
            routes["also_exposed"] += 1
            if not counted(cue["action"]):
                routes["exposed_without_action_match"] += 1
            elif counted(cue["current"]):
                routes["in_action_and_current"] += 1
            else:
                routes["in_action_without_current_match"] += 1
    return (
        {
            key: {mode: counts[mode] for mode in ("exact", "normalized_only", "absent", "unobserved")}
            for key, counts in stages.items()
        },
        routes,
    )


def condition_summary(phase, condition, rows, sources, public_sources):
    decisions = Counter()
    fields = Counter()
    with_source_evidence = 0
    for row in rows:
        choice = row["decision"]
        action = "unobserved"
        if choice is not None:
            action = "call" if choice["calls"] else "no_call"
            fields.update({s["field"] for s in choice.get("memory", [])})
            with_source_evidence += row["task"] in public_sources
        grade = {True: "success", False: "failure", None: "ungraded"}[row["official_success"]]
        decisions[action, grade] += 1
    counts, routes = literal_counts(rows)
    versus_first = None
    if phase == "solve":
        versus_first = paired_effort(
            [effort_pair(row, sources[row["task"]]) for row in rows], metrics=("tokens", "business_calls")
        )
    return dict(
        phase=phase,
        condition=condition,
        assigned=len(rows),
        started=sum(r["status"] != "not_started" for r in rows),
        graded=sum(r["status"] == "graded" for r in rows),
        clusters=len({r["cluster"] for r in rows}),
        tasks=len({r["task"] for r in rows}),
        first_decision={
            a: {g: decisions[a, g] for g in ("success", "failure", "ungraded")}
            for a in ("call", "no_call", "unobserved")
        },
        decisions_with_body=fields["body"],
        decisions_with_title=fields["title"],
        read_calls=dict(sum((Counter(r["reads_before_decision"] or {}) for r in rows), Counter())),
        decisions_with_source_evidence=with_source_evidence,
        literal_matches=counts,
        source_cue_occurrences=routes,
        effort=total_effort(rows),
        versus_first_pass=versus_first,
    )


def policy_comparisons(groups, cluster_unit):
    arms = {condition for phase, condition in groups if phase == "solve"}
    if arms == {"existing", "candidate"}:
        ref, new, by_delivery = "existing", "candidate", False
    elif arms == {"on_demand", "preload"}:
        ref, new, by_delivery = "on_demand", "preload", True
    else:
        return []

    def slots(condition):
        rows = groups["solve", condition]
        indexed = {
            (r["task"], r["form"], r["writer_draw"], r["reader_repeat"], None if by_delivery else r["delivery"]): r
            for r in rows
        }
        require(len(indexed) == len(rows), "Ambiguous policy pairing")
        return indexed

    left, right = slots(ref), slots(new)
    require(left.keys() == right.keys(), "Incomplete policy pairing")
    for key, a in left.items():
        b = right[key]
        require(a["source_run"] == b["source_run"] and a["cluster"] == b["cluster"], "Different pairing source")
        if by_delivery and a["writer_run"] and b["writer_run"]:
            require(
                a["writer_run"] == b["writer_run"] and a["snapshot"] == b["snapshot"], "Different delivery banks"
            )
    comparisons = []
    for form in [None, *sorted({key[1] for key in left if key[1] is not None})]:
        keys = [k for k in left if form is None or k[1] == form]
        pairs = [effort_pair(right[k], left[k]) for k in keys]
        contrast = clustered_contrast(
            [
                (left[k]["cluster"], k[0], k[1], k[2:], left[k]["official_success"], right[k]["official_success"])
                for k in keys
            ],
            cluster_unit=cluster_unit,
        )
        comparisons.append(
            dict(
                reference=ref,
                candidate=new,
                form=form,
                clusters=contrast["clusters"],
                cluster_contrast=contrast,
                **paired_effort(pairs, metrics=("tokens", "business_calls")),
            )
        )
    return comparisons


def banking_cost(*closures):
    stages = [s for c in closures for s in c["stages"].values()]
    confirmed = sum((Decimal(s["reported"]) for s in stages), Decimal(0))
    held = sum((Decimal(s["held"]) for s in stages), Decimal(0))
    covered = Decimal(closures[-1]["total_covered_usd"])
    require(confirmed + held == covered, "Study bills and final covered amount differ")
    return dict(confirmed_usd=str(confirmed), held_usd=str(held), covered_usd=str(covered))
