"""Map native result archives to the same source and solve observation fields as live trajectory adapters."""

from agent_memory_eval.analysis.efficiency import first_decision_effort
from agent_memory_eval.analysis.recorded_decisions import require, sealed_bytes
from agent_memory_eval.analysis.trajectory import allocation
from agent_memory_eval.contracts.core import read_json
from agent_memory_eval.recording.batch import sealed_rows
from agent_memory_eval.recording.journal import safe_path


def _outcome(row, native):
    grade = native["success"]
    status = "not_started" if native["status"] == "not_started" else "ungraded" if grade is None else "graded"
    row.update(run=native["run"], official_success=grade, status=status,
               reason=native.get("reason") or (native["status"] if grade is None else None))
    return row


def _bank_readers(release, recovery):
    original = release["readers"]
    if recovery is None:
        return [(r, {}) for r in original]
    require(recovery["format"] == "banking-memory-cohort-recovery-sensitivity/v1"
            and recovery["original_binding"] == release["binding"], "Recovery belongs to another release")
    old = {r["run"]: r for r in original if r["run"] is not None}
    replacements = {}
    for obs in recovery["observations"]:
        run, new = obs["original_run"], obs["reader"]
        prior = old.get(run)
        require(prior and run not in replacements and new["run"] not in old and
                obs["original_grade"] is prior["success"] is None and obs["original_journal"] == prior["journal"],
                "Recovery must replace an ungraded original exactly once")
        keys = ("task", "draw", "repeat", "form", "delivery", "customer", "cohort", "source", "expected_writer")
        require(all(new[k] == prior[k] for k in keys), "Recovery changed a registered allocation")
        if new["dispatched_worker"]:
            require(all(new[k] == prior[k] for k in ("start", "start_source", "writer_snapshot", "writer", "first_worker")),
                    "Recovery changed the source, memory or first request")
        replacements[run] = new, dict(original_run=run, original_journal=prior["journal"])
    return [replacements.get(r["run"], (r, {})) for r in original]


def _bank_source_effort(workspace, binding, sources):
    root = safe_path(workspace / "runs", binding["recording"])
    registered = read_json(sealed_bytes(root / "registration.json", binding["registration_sha256"]))
    cases = {(c["task_id"], c["source_draw"]): c for c in registered["preflight"]["cases"]}
    efforts = {}
    for s in sources:
        if s["success"] is None:
            continue
        case = cases[s["task"], s["draw"]]
        ref = case.get("imported_first_pass")
        folder = safe_path(workspace / "runs", ref["directory"]) if ref else safe_path(root, f"task-{case['task']}/source")
        _, _, (row,) = sealed_rows(folder)
        require(row["run_id"] == s["run"] and row["result"]["log_sha256"] == s["journal"]
                and row["result"]["official_success"] is s["success"], "Source journal differs from its archive")
        journal = sealed_bytes(safe_path(folder, row["directory"]) / "log.jsonl", s["journal"])
        events = [e for line in journal.splitlines() for e in read_json(line)["events"] if e["run_id"] == s["run"]]
        efforts[s["run"]] = first_decision_effort(events)
    return efforts


def banking_rows(archive, *, release=None, recovery=None, workspace=None):
    releases = archive["releases"]
    require(release in releases or release is None and len(releases) == 1, "Select one registered release")
    native = releases[release or next(iter(releases))]
    readers = _bank_readers(native, recovery)
    customers = {}
    for r, _ in readers:
        require(customers.setdefault(r["task"], r["customer"]) == r["customer"], "Different customers for one task")
    sources = {(s["task"], s["run"]): s for s in native["sources"]}
    writers = {w["run"]: w for w in native["writers"] if w["run"]}
    efforts = _bank_source_effort(workspace, native["binding"], native["sources"]) if workspace is not None else {}
    rows = []
    for s in native["sources"]:
        row = _outcome(allocation(s["task"], customers[s["task"]], "source", "no_memory"), s)
        row.update(evidence=dict(journal=s.get("journal"), grade=s.get("report"),
                                 public_context=s.get("public_context_sha256")), decision=s.get("first_business"),
                   decision_effort=efforts.get(s["run"]))
        rows.append(row)
    for r, history in readers:
        task, src = r["task"], r["source"]
        source = sources.get((task, src["run"]))
        require(source is not None and source["success"] is src["success"], "Different archived source grade")
        writer = writers.get(r["expected_writer"])
        if r["dispatched_worker"]:
            require(writer and writer["source"] == src["run"] == r["start_source"] and source["start"] == r["start"]
                    and writer["task"] == task and writer["form"] == r["form"] and writer["run"] == r["writer"]
                    and writer["snapshot"] == r["writer_snapshot"], "Reader received a different source or memory")
        row = _outcome(allocation(task, r["customer"], "solve", "recorded_writer", form=r["form"],
                                  delivery=r["delivery"], draw=r["draw"], repeat=r["repeat"]), r)
        row.update(source_run=src["run"], writer_run=r.get("writer"), snapshot=r.get("writer_snapshot"),
                   decision=r.get("first_business"), evidence=dict(journal=r.get("journal"), grade=r.get("report"),
                                                                  writer_journal=writer.get("journal") if writer else None, **history))
        if r.get("first_worker") and r.get("start_source"):
            effort = r["effort"]
            row["decision_effort"] = dict(tokens=effort["tokens"], requests=effort["http_calls"], usd=effort["usd"])
        rows.append(row)
    return rows, "registered_customer_or_unresolved_task"


def bfcl_rows(archive):
    require(set(archive["registration"]["tasks"]) == set(archive["tasks"]), "Missing registered BFCL question")
    rows = []
    for task, data in archive["tasks"].items():
        trials = data["allocations"]
        source, = [r for r in trials if r["phase"] == "source"]
        writers = {r["form"]: r for r in trials if r["phase"] == "write"}
        for trial in trials:
            phase = trial["phase"]
            require(trial["task"] == task and phase in {"write", "solve", "source"}, "Invalid native allocation")
            if phase == "write":
                continue
            form, delivery = trial["form"], trial["mode"]
            row = _outcome(allocation(task, task, phase, "no_memory" if phase == "source" else "recorded_writer", form=form, delivery=delivery,
                                      draw=1 if form else None, repeat=1 if form else None), trial)
            row["evidence"] = dict(journal=data["journal_sha256"], decision=trial.get("decision"))
            row["snapshot"] = trial.get("snapshot")
            if phase == "solve":
                row["source_run"] = source["run"]
                if trial["status"] != "not_started":
                    writer = writers[form]
                    require(writer.get("snapshot") and writer["snapshot"] == row["snapshot"], "Different BFCL writer snapshot")
                    row["writer_run"] = writer["run"]
                    row["evidence"].update(public_context=writer["experience_sha256"], writer_request=writer["writer_request_sha256"])
            if trial["status"] == "business_decision":
                row["decision"] = dict(**(trial.get("decision") or {}), memory=[dict(field=f) for f in trial["fields"]])
            if "prompt_tokens" in trial:
                row["decision_effort"] = dict(tokens=trial["prompt_tokens"] + trial["completion_tokens"],
                                              requests=trial["requests"], usd="0")
            rows.append(row)
    return rows, "public_question"
