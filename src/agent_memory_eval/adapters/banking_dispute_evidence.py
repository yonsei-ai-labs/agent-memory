"""Check a public opportunity to reuse evidence in a multi-transaction banking task."""

import json
import re

from agent_memory_eval.contracts.core import ContractError, digest

SUBMIT = "submit_cash_back_dispute_0589"
DOCUMENT = re.compile(r"(?m)^\s*ID: ([^\n]+)\n\s*Score: [^\n]+\n\s*Content: ")
TRANSACTION = re.compile(r"\btxn_[A-Za-z0-9_]+\b")
RATE = re.compile(r"\d+(?:\.\d+)?\s*(?:%|percent\b)", re.I)


def dispute_targets(task):
    arguments = [json.loads(a["arguments"]["arguments"])
                 for a in task["evaluation_criteria"]["actions"]
                 if a["name"] == "call_discoverable_user_tool" and a["requestor"] == "user"
                 and a["arguments"].get("discoverable_tool_name") == SUBMIT]
    ids = [arg["transaction_id"] for arg in arguments]
    customers = {arg["user_id"] for arg in arguments}
    if len(customers) != 1 or len(ids) != len(set(ids)) or len(ids) < 2:
        raise ContractError("Expected distinct dispute targets for one native customer")
    return customers.pop(), set(ids)


def public_opportunity(task, source):
    if source is None:
        return dict(eligible=False, reason="first_pass_ungraded")
    _, targets = dispute_targets(task)
    context = json.loads(source.public_context)
    messages = context["messages"]
    # The native greeting and first customer utterance precede the first worker request.
    first_agent = next((i for i, m in enumerate(messages) if m["role"] == "assistant"
                        and i > 0), len(messages))
    initial = json.dumps([context["task_context"], *messages[:first_agent]], ensure_ascii=False)
    initial_ids = set(TRANSACTION.findall(initial))
    tools, observed, rates, procedure = {}, set(), set(), []
    witnesses = []
    cards = {d for d in task["required_documents"] if "(general)" not in d}
    for i, m in enumerate(messages):
        for call in m.get("tool_calls") or []:
            tools[call["id"]] = call["function"]["name"]
        if m["role"] != "tool":
            continue
        name = tools[m["tool_call_id"]]
        content = m["content"]
        ids = sorted(targets.intersection(TRANSACTION.findall(content)).difference(initial_ids))
        docs = []
        has_procedure = False
        if name == "grep":
            matches = list(DOCUMENT.finditer(content))
            for j, match in enumerate(matches):
                doc = match.group(1).strip()
                end = matches[j + 1].start() if j + 1 < len(matches) else len(content)
                if doc in cards and doc not in initial and RATE.search(content[match.end():end]):
                    docs.append(doc)
            has_procedure = SUBMIT in content and SUBMIT not in initial
        if ids or docs or has_procedure:
            witnesses.append(dict(message_index=i, tool=name, content_sha256=digest(content.encode()),
                                  target_ids=ids, rate_doc_ids=docs, submission_tool=has_procedure))
        observed.update(ids)
        rates.update(docs)
        if has_procedure:
            procedure.append(i)
    eligible = bool(observed and (rates or procedure))
    return dict(eligible=eligible, reason="verified_public_opportunity" if eligible else "insufficient_public_clue",
                source_run_id=source.source_run_id, public_context_sha256=source.public_context_sha256,
                target_ids_observed=sorted(observed), applicable_rate_docs=sorted(rates),
                documented_submission_tool=bool(procedure), witnesses=witnesses)


def public_transaction_labels(source):
    """Extract merchant and amount from each distinct public native transaction record."""
    if source is None:
        return {}
    labels = {}
    for msg in json.loads(source.public_context)["messages"]:
        if msg["role"] != "tool":
            continue
        for row in re.split(r"(?m)^\s*\d+\. Record ID: ", msg.get("content") or "")[1:]:
            fields = dict(re.findall(r"(?m)^\s+(transaction_id|merchant_name|transaction_amount): ([^\n]+)", row))
            if len(fields) == 3:
                tid = fields.pop("transaction_id")
                if tid in labels and labels[tid] != fields:
                    raise ContractError("Inconsistent versions of one public transaction")
                labels[tid] = fields
    return labels


def merchant_amount_mentions(text, labels):
    """Count a unique public merchant/amount pair when both literals occur within 64 characters."""
    from decimal import Decimal
    from collections import Counter

    def money(value):
        return Decimal(value.replace(",", "").replace("$", "").strip())

    keys = {tid:(row["merchant_name"].casefold(), money(row["transaction_amount"])) for tid, row in labels.items()}
    frequency = Counter(keys.values())
    amounts = [(m.start(), m.end(), money(m.group(1))) for m in re.finditer(r"\$\s*(\d+(?:,\d{3})*(?:\.\d{1,2})?)(?!\d)", text)]
    result = {}
    for tid, (merchant, value) in keys.items():
        if frequency[merchant, value] != 1:
            continue
        for m in re.finditer(r"(?<!\w)" + re.escape(merchant) + r"(?!\w)", text, flags=re.I):
            window = next(((min(begin, m.start()), max(end, m.end())) for begin, end, v in amounts
                           if v == value and max(begin - m.end(), m.start() - end, 0) <= 64), None)
            if window:
                result[tid] = text[window[0]:window[1]]
                break
    return result
