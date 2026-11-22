"""Propose linked labels from unambiguous public exchanges with one explicit cancellation."""

import argparse
import json
import re
import unicodedata
from collections import Counter
from hashlib import sha256
from pathlib import Path

from agent_memory_eval.contracts.core import canonical, digest
from scripts.analyze_full_retail import require
from scripts.analyze_retail_two_visit import review_ids
from scripts.audit_retail_order_choice import publish
from scripts.run_retail_two_visit import read

ORDER = re.compile(r"(?<![a-z0-9])#?w\d{7}(?!\d)", re.IGNORECASE)
REASON = re.compile(
    r"\bno longer (?:need(?:ed)?|requir(?:e|ed))\b|\b(?:don't|do not|doesn't) need.{0,50}?any(?:more| longer)\b",
    re.IGNORECASE,
)
ASK = re.compile(
    r"\b(?:confirm|authori[sz]e|permission|should i|may i|would you like me to|shall i|do you (?:want|approve)|is (?:that|this) (?:right|correct))\b|\byes\s*(?:/|or)\s*no\b",
    re.IGNORECASE,
)
IDENTIFY = re.compile(
    r"\bwhich\b.{0,85}?\b(?:order|one)\b|\b(?:provide|share|give|tell|know|supply|specify|confirm|send|what(?:'s| is))\b.{0,65}?\border (?:number|id)\b",
    re.IGNORECASE,
)
AFFIRM = re.compile(
    r"^(?:(?:okay|ok|sure)[, .-]+)?(?:yes\b|yeah\b|yep\b|correct\b|(?:that's|that is) (?:right|correct|the (?:right |correct )?one)\b|(?:everything|those details|the details|that) (?:all )?(?:matches|match|looks (?:right|correct))\b|(?:please )?(?:go ahead|proceed|cancel)\b|i (?:confirm|authori[sz]e|approve)\b)",
    re.IGNORECASE,
)
CONFLICT = re.compile(
    r"\b(?:not|don't|do not|hold|actually|wait|except|wrong|different|instead|however|but|only if|unless|stop|never|before)\b|\bno\b(?! (?:longer|problem))|\?",
    re.IGNORECASE,
)


def clean(text):
    # Some public answers use a JSON object as the visible text.
    if text.startswith("{"):
        try:
            wrapper = json.loads(text)
            if set(wrapper) == {"message"} and isinstance(wrapper["message"], str):
                text = wrapper["message"]
        except (ValueError, TypeError):
            pass
    text = unicodedata.normalize("NFKC", text).translate(
        str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})
    )
    return " ".join(text.replace("*", "").replace("`", "").split())


def ids(text):
    return {"#" + m.upper().lstrip("#") for m in ORDER.findall(text)}


def propose(packet):
    turns = packet["turns"]
    labels = {
        "status": "rule_coded",
        "method": "explicit_public_turns_v1",
        "turns_sha256": packet["turns_sha256"],
        "clarifications": [],
        "proposals": [],
        "responses": [],
    }
    cancellations = [
        i for i, t in enumerate(turns) if t.get("role") == "action" and t.get("name") == "cancel_pending_order"
    ]
    if len(cancellations) != 1:
        return None, "cancellation_attempts"
    operation = cancellations[0]
    dialogue = [
        (i, t["role"], clean(t["text"]))
        for i, t in enumerate(turns)
        if 7 <= i < operation and t["role"] in {"assistant", "user"}
    ]
    if (
        len(dialogue) < 2
        or len(dialogue) % 2
        or any(r != ("assistant", "user")[n % 2] for n, (_, r, _) in enumerate(dialogue))
    ):
        return None, "turn_order"
    proposal, _, text = dialogue[-2]
    consent, _, reply = dialogue[-1]
    targets = ids(text)
    if len(targets) != 1 or "cancel" not in text.lower() or not ASK.search(text) or not REASON.search(text):
        return None, "proposal_or_reason"
    order = next(iter(targets))
    if not AFFIRM.search(reply) or CONFLICT.search(reply) or (ids(reply) - {order}):
        return None, "affirmation"
    if any(t["role"] == "action" for t in turns[proposal + 1 : operation]):
        return None, "additional_action"
    for question, answer in zip(dialogue[:-2:2], dialogue[1:-2:2], strict=True):
        i, _, q = question
        alternatives = ids(q)
        explicit_permission = re.search(
            r"\b(?:should i|may i|shall i|do you (?:confirm|authori[sz]e)|would you like me to|yes or no)\b",
            q,
            re.IGNORECASE,
        )
        if (
            IDENTIFY.search(q)
            and len(ids(answer[2])) == 1
            and len(alternatives) != 1
            and not explicit_permission
        ):
            labels["clarifications"].append(i)
        elif (
            len(alternatives) == 1
            and explicit_permission
            and "cancel" in q.lower()
            and REASON.search(q)
            and re.search(
                r"\b(?:wrong order|not the (?:right |correct )?(?:order|one)|no[,!])", answer[2], re.IGNORECASE
            )
        ):
            labels["proposals"].append(
                {"turn": i, "order": next(iter(alternatives)), "reason": "no longer needed"}
            )
            labels["responses"].append({"turn": answer[0], "proposal": i, "disposition": "refused"})
        else:
            return None, "earlier_exchange"
    trailing = [clean(t["text"]).lower() for t in turns[operation + 1 :] if t["role"] == "assistant"]
    if any(
        ("?" in t and not re.search("anything else|any more|else i can", t)) or ("please confirm" in t)
        for t in trailing
    ):
        return None, "later_question"
    # A distinct question asking which order is meant can occur in the same turn as a proposal.
    for i, _, q in dialogue[::2]:
        if (
            re.search(
                r"\b(?:is (?:this|that) the (?:order|one) you (?:mean|meant|were referring to)|is (?:this|that) the order we discussed)\b",
                q,
                re.IGNORECASE,
            )
            and i not in labels["clarifications"]
        ):
            labels["clarifications"].append(i)
    labels["clarifications"].sort()
    labels["proposals"].append({"turn": proposal, "order": order, "reason": "no longer needed"})
    labels["responses"].append({"turn": consent, "proposal": proposal, "disposition": "confirmed"})
    return labels, "strict"


def public_packets(path):
    if path.is_file():
        yield from read(path).items()
    else:
        for entry in sorted(path.glob("*.json")):
            yield entry.stem, read(entry)


def seal(packets, direct, registration, secret, sample):
    index = review_ids(registration["jobs"]["decision"], secret, None)
    expected = sorted(
        index.values(),
        key=lambda v: sha256(("retail-two-visit-public-coding-audit-v1/" + v).encode()).hexdigest(),
    )[:100]
    require(set(sample["ids"]) == set(expected), "Blinded quality-control sample changed")
    require(direct.keys() <= set(index.values()), "Review outside the registered cohort")
    checks = Counter()
    disagreements = []
    methods = Counter()
    seen = set()
    for blind, packet in public_packets(packets):
        require(blind in direct and blind not in seen, "Unreviewed or duplicate completed transcript")
        seen.add(blind)
        labels = direct[blind]
        require(
            labels["turns_sha256"] == packet["turns_sha256"] == digest(canonical(packet["turns"])),
            "Changed review transcript",
        )
        require(labels["status"] in {"reviewed", "unresolved"}, "Missing direct coding")
        methods[labels["method"]] += 1
        rule, _ = propose(packet)
        if blind not in expected:
            continue
        require(
            labels["method"] == "blinded_direct_agent_read",
            "Audit sample requires a full blinded dialogue read",
        )
        checks["available"] += 1
        if rule is not None:
            checks["strict_candidates"] += 1
            different = [k for k in ("clarifications", "proposals", "responses") if rule[k] != labels[k]]
            if different:
                disagreements.append({"id": blind, "fields": different})
    require(seen == set(direct), "Final review coverage differs")
    return {
        "study": registration["design"]["name"],
        "schedule_sha256": registration["preflight"]["schedule_sha256"],
        "sample": {"registered": len(expected), **dict(checks), "disagreements": disagreements},
        "methods": dict(methods),
        "labels": direct,
        "index": index,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packets", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--direct", type=Path, help="Seal all adjudicated labels and release the run/ID index")
    parser.add_argument("--registration", type=Path)
    parser.add_argument("--blind-key", type=Path)
    parser.add_argument("--sample", type=Path)
    args = parser.parse_args()
    if args.direct:
        require(
            args.registration and args.blind_key and args.sample,
            "Sealing needs the registration, key and fixed sample",
        )
        publish(
            args.output,
            seal(
                args.packets,
                read(args.direct),
                read(args.registration),
                args.blind_key.read_bytes(),
                read(args.sample),
            ),
        )
        raise SystemExit(0)
    accepted, other = {}, {}
    for blind, packet in public_packets(args.packets):
        value, reason = propose(packet)
        if value:
            accepted[blind] = value
        else:
            other[blind] = reason
    pending = args.output.with_suffix(".pending")
    pending.write_text(json.dumps({"candidates": accepted, "manual": other}, indent=2) + "\n")
    pending.replace(args.output)
    print("strict candidates", len(accepted), "manual", dict(Counter(other.values())))
