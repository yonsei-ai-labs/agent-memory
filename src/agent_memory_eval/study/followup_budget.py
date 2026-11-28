"""Append-only cumulative approvals for the registered retail follow-up."""

from decimal import Decimal, InvalidOperation
import subprocess

from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.contracts.core import ContractError, canonical, digest
from agent_memory_eval.recording.assets import publish_json
from agent_memory_eval.study.records import fixed, read


def approval_amount(value):
    try:
        amount = Decimal(str(value))
    except InvalidOperation as error:
        raise ContractError("Supply a positive finite spending ceiling") from error
    require(amount.is_finite() and amount > 0, "Supply a positive finite spending ceiling")
    return amount


def check_increase(previous, grant):
    mutable = {"max_usd", "user_authorization"}
    require({k: v for k, v in previous.items() if k not in mutable}
            == {k: v for k, v in grant.items() if k not in mutable}, "Budget increase changes the registered study")
    require(isinstance(grant.get("user_authorization"), str) and grant["user_authorization"].strip()
            and approval_amount(grant["max_usd"]) > approval_amount(previous["max_usd"]),
            "A budget increase requires a higher cumulative ceiling and explicit authorization")


def increases(path, first):
    previous = first
    for number, file in enumerate(sorted(path.glob("*.json")), 1):
        record = read(file)
        require(file.name == f"{number:06}.json" and record["previous_sha256"] == digest(canonical(previous)),
                "Incomplete or changed budget approval history")
        check_increase(previous, record["approval"])
        previous = record["approval"]
        yield record


def approval_chain(path):
    require(path.is_file(), "The follow-up needs an explicit spending approval")
    first = read(path)
    return [first, *(entry["approval"] for entry in increases(path.with_suffix(".increases"), first))]


def publish_approval(path, grant):
    """The initial approval and each increase are individually immutable, including racing authorizations."""
    folder = path.with_suffix(".increases")
    if path.exists():
        chain = approval_chain(path)
        if grant == chain[-1]:
            return path if len(chain) == 1 else folder / f"{len(chain) - 1:06}.json"
        check_increase(chain[-1], grant)
        folder.mkdir(exist_ok=True)
        path = folder / f"{len(chain):06}.json"
        grant = dict(previous_sha256=digest(canonical(chain[-1])), approval=grant)
    try:
        publish_json(path, grant)
    except FileExistsError:
        require(read(path) == grant, "Another approval was recorded first")
    return path


def register_grants(dest, grants, max_calls, reserved_calls):
    """Record the reservation boundary at which each new ceiling took effect under the controller lock."""
    fixed(dest / "authorization.json", dict(grants[0], max_requests=max_calls))
    folder = dest / "grant-increases"
    entries = list(increases(folder, grants[0]))
    require([e["approval"] for e in entries] == list(grants[1:len(entries) + 1]),
            "Resume needs the complete original budget approval history")
    for i in range(len(entries) + 1, len(grants)):
        check_increase(grants[i - 1], grants[i])
        folder.mkdir(exist_ok=True)
        publish_json(folder / f"{i:06}.json", dict(previous_sha256=digest(canonical(grants[i - 1])),
                     approval=grants[i], after_reserved_calls=reserved_calls))


def ceiling_schedule(dest, initial):
    first = {k: v for k, v in initial.items() if k != "max_requests"}
    schedule = [(0, approval_amount(first["max_usd"]))]
    for entry in increases(dest / "grant-increases", first):
        boundary = entry["after_reserved_calls"]
        require(type(boundary) is int and boundary >= schedule[-1][0], "Invalid approval reservation boundary")
        schedule.append((boundary, approval_amount(entry["approval"]["max_usd"])))
    return schedule


def registration_freeze(root, plan_id, original, current):
    if original != current:
        old, new = original["implementation_commit"], current["implementation_commit"]
        git_ids = {"implementation_commit", "implementation_tree"}
        require({k: v for k, v in original.items() if k not in git_ids}
                == {k: v for k, v in current.items() if k not in git_ids}, "The registered implementation freeze changed")
        tree = subprocess.check_output(["git", "-C", str(root), "rev-parse", old + "^{tree}"], text=True).strip()
        require(tree == original["implementation_tree"]
                and subprocess.run(["git", "-C", str(root), "diff", "--quiet", old, new, "--", ".",
                                    f":(exclude)artifacts/approvals/{plan_id}.increases/**"]).returncode == 0,
                "Resume needs the registered implementation; only this plan's committed budget increases may differ")
    return original
