"""Blind, citation-checked review of retrospective business outcome claims in stored memory."""

from collections import Counter

from agent_memory_eval.analysis.claim_evidence import _citation
from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.contracts.core import canonical, digest


def outcome_packet(item, receipts, opening):
    """Deduplicate identical evidence without showing writer policy, form, grade or later behavior."""
    content = dict(memory={k: item[k] for k in ("title", "body")}, public_opening=opening, receipts=receipts)
    return digest(canonical(content)), content


def score_outcome_review(packet, rating):
    """Check quotes and receipt links. Semantic product, owner and scope judgments remain the reviewer's."""
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
        require(set(claim) == {"field", "quote", "judgment", "outcome", "call_ids"}
                and claim["field"] in {"title", "body"} and claim["judgment"] in {"supported", "contradicted", "unsupported"}
                and claim["outcome"] in {"opened", "eligibility_rejected", "request_rejected", "opening_failed"}
                and isinstance(claim["call_ids"], list), "Invalid retrospective opening claim")
        citation = _citation(packet, "/memory/" + claim["field"], claim["quote"])
        ids = claim["call_ids"]
        require(len(set(ids)) == len(ids) and all(k in by_id for k in ids), "Invalid receipt citation")
        actual = {by_id[k]["outcome"] for k in ids}
        if claim["judgment"] == "supported":
            require(ids and actual == {claim["outcome"]}, "Reported outcome differs from its cited return")
            covered.update(ids)
        elif claim["judgment"] == "contradicted":
            require(ids and actual.isdisjoint({claim["outcome"], "unobserved", "unrecognized"}),
                    "A contradiction needs an observed incompatible outcome")
        checked.append(dict(**claim, citation=citation))
    observable = {k for k, r in by_id.items() if r["outcome"] not in {"unobserved", "unrecognized"}}
    return dict(claims=checked, no_outcome_reason=rating["no_outcome_reason"],
                unsupported_or_contradicted=any(c["judgment"] != "supported" for c in claims),
                observable_receipts=len(observable), covered_receipts=len(covered),
                omitted_call_ids=sorted(observable - covered))


def join_outcome_reviews(packets, assignments, review):
    """Use stored-item and observed-source-return denominators separately; retain missing ratings."""
    require(set(review) <= set(packets) and all(digest(canonical(v)) == k for k, v in packets.items()),
            "Unknown or changed masked packet")
    scored = {k: score_outcome_review(packets[k], rating) for k, rating in review.items()}
    totals = {}
    rows = []
    for item in assignments:
        group = totals.setdefault(item["policy"], Counter())
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
