"""Blind, citation-checked review of retrospective business outcome claims in stored memory."""

from collections import Counter

from agent_memory_eval.analysis.claim_evidence import _citation
from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.contracts.core import canonical, digest


def outcome_packet(item, receipts, opening, *, claim_outcomes, subject_fields=(), required_subject_fields=()):
    """Deduplicate identical evidence without showing writer policy, form, grade or later behavior."""
    labels = sorted(claim_outcomes)
    require(labels and all(isinstance(label, str) and label for label in labels)
            and len(labels) == len(set(labels)) and not {"unobserved", "unrecognized"}.intersection(labels),
            "Declare the business outcomes that an observed return can establish")
    content = dict(memory={k: item[k] for k in ("title", "body")}, public_opening=opening, receipts=receipts,
                   claim_outcomes=labels)
    require(set(required_subject_fields) <= set(subject_fields), "Unknown required subject field")
    if subject_fields:
        require(len(set(subject_fields)) == len(subject_fields)
                and all(isinstance(k, str) and k for k in subject_fields), "Invalid receipt subject fields")
        content["subject_fields"] = list(subject_fields)
        content["required_subject_fields"] = list(required_subject_fields)
    return digest(canonical(content)), content


def score_outcome_review(packet, rating):
    """Check the quoted claim against the status and subject of each cited public return."""
    require(set(rating) == {"claims", "no_outcome_reason"} and isinstance(rating["claims"], list),
            "Invalid outcome review")
    by_id = {r["call_id"]: r for r in packet["receipts"]}
    claims = rating["claims"]
    if claims:
        require(rating["no_outcome_reason"] is None, "An outcome claim has been rated as absent")
    else:
        require(rating["no_outcome_reason"] in {"explicitly_unknown", "attempt_only", "future_or_general", "no_mention"},
                "Specify why there is no retrospective outcome claim")
    checked, covered = [], set()
    for claim in claims:
        fields = packet.get("subject_fields", [])
        keys = {"field", "quote", "judgment", "outcome", "call_ids"}
        judgments = {"supported", "contradicted", "unsupported"}
        if fields:
            keys.add("subject")
            judgments.add("outcome_only")
        require(set(claim) == keys
                and claim["field"] in {"title", "body"} and claim["judgment"] in judgments
                and claim["outcome"] in packet["claim_outcomes"]
                and isinstance(claim["call_ids"], list), "Invalid retrospective outcome claim")
        citation = _citation(packet, "/memory/" + claim["field"], claim["quote"])
        ids = claim["call_ids"]
        require(len(set(ids)) == len(ids) and all(k in by_id for k in ids), "Invalid receipt citation")
        subject = {}
        if fields:
            require(isinstance(claim["subject"], dict) and set(claim["subject"]) == set(fields),
                    "Record every declared subject field, using null for an unspecified value")
            subject = {k: v for k, v in claim["subject"].items() if v is not None}
            require(all(isinstance(v, str) and v.strip() and v in claim["quote"]
                        for v in subject.values()), "A subject value must appear in the quoted claim")
        specified = bool(subject) and set(packet.get("required_subject_fields", ())) <= subject.keys()
        evidence = []
        for key in ids:
            receipt = by_id[key]
            args = receipt.get("arguments", {})
            if receipt["outcome"] in {"unobserved", "unrecognized"}:
                evidence.append("unknown")
            elif receipt["outcome"] != claim["outcome"] or any(
                isinstance(args.get(k), str) and args[k].casefold() != v.casefold() for k, v in subject.items()
            ):
                evidence.append("contradicted")
            elif all(isinstance(args.get(k), str) and args[k].casefold() == v.casefold() for k, v in subject.items()):
                evidence.append("supported")
            else:
                evidence.append("unknown")
        if claim["judgment"] == "supported":
            require((not fields or specified) and evidence and set(evidence) == {"supported"},
                    "The cited returns must confirm the reported outcome and its stated subject")
            covered.update(ids)
        elif claim["judgment"] == "contradicted":
            require(evidence and set(evidence) == {"contradicted"},
                    "A contradiction needs an observed incompatible outcome or subject")
        elif claim["judgment"] == "outcome_only":
            require(not specified and evidence and set(evidence) == {"supported"},
                    "Outcome-only requires a confirmed status and no confirmed product-level claim")
        checked.append(dict(**claim, citation=citation))
    observable = {k for k, r in by_id.items() if r["outcome"] not in {"unobserved", "unrecognized"}}
    return dict(claims=checked, no_outcome_reason=rating["no_outcome_reason"],
                unsupported_or_contradicted=any(c["judgment"] in {"unsupported", "contradicted"} for c in claims),
                observable_receipts=len(observable), covered_receipts=len(covered),
                omitted_call_ids=sorted(observable - covered))


def join_outcome_reviews(packets, assignments, review):
    """Use stored-item and observed-source-return denominators separately; retain missing ratings."""
    require(set(review) <= set(packets) and all(digest(canonical(v)) == k for k, v in packets.items()),
            "Unknown or changed masked packet")
    scored = {k: score_outcome_review(packets[k], rating) for k, rating in review.items()}
    counts = ("assigned_writers", "stored", "rated", "items_with_unsupported_or_contradicted",
              "items_without_outcome_claim", "covered_receipts", "observable_receipts_across_items")
    totals = {}
    rows = []
    for item in assignments:
        group = totals.setdefault(item["policy"], Counter(dict.fromkeys(counts, 0)))
        row = dict(item, rating=scored.get(item["packet"]))
        rows.append(row)
        group["assigned_writers"] += 1
        group["stored"] += item["packet"] is not None
        if row["rating"] is not None:
            group["rated"] += 1
            r = row["rating"]
            group["items_with_unsupported_or_contradicted"] += r["unsupported_or_contradicted"]
            group["items_without_outcome_claim"] += not r["claims"]
            group["covered_receipts"] += r["covered_receipts"]
            group["observable_receipts_across_items"] += r["observable_receipts"]
    return dict(policy_totals=totals, rows=rows)


def join_bank_reviews(packets, assignments, review):
    """Score every stored version and give each registered writing assignment one paired observation."""
    require(set(review) <= set(packets) and all(digest(canonical(v)) == k for k, v in packets.items()),
            "Unknown or changed masked packet")
    scores = {k: score_outcome_review(packets[k], vote) for k, vote in review.items()}
    totals, rows = {}, []
    for bank in assignments:
        items = [dict(item, rating=scores.get(item["packet"])) for item in bank["items"]]
        require(all(i["packet"] in packets for i in items)
                and len({i["id"] for i in items}) == len(items), "Invalid stored version or masked packet")
        rated = [item["rating"] for item in items if item["rating"] is not None]
        complete = bool(items) and len(items) == len(rated)
        bad = sum(r["unsupported_or_contradicted"] for r in rated)
        bank_rating = dict(unsupported_or_contradicted=bool(bad)) if complete else None
        rows.append(dict(bank, items=items, rating=bank_rating))
        totals.setdefault(bank["policy"], Counter()).update(dict(
            assigned_writers=1, stored_banks=bool(items), fully_rated_banks=complete,
            banks_with_unsupported_or_contradicted=complete and bool(bad),
            stored_items=len(items), rated_items=len(rated), items_with_unsupported_or_contradicted=bad,
            items_without_outcome_claim=sum(not r["claims"] for r in rated),
            outcome_only_claims=sum(c["judgment"] == "outcome_only" for r in rated for c in r["claims"]),
            covered_receipts=sum(r["covered_receipts"] for r in rated),
            observable_receipts_across_items=sum(r["observable_receipts"] for r in rated)))
    return dict(unit="writer_bank", policy_totals=totals, rows=rows)
