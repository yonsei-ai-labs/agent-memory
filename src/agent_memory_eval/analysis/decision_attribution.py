"""Compare observed business choices at matched public inputs, independently of the benchmark."""

from collections import Counter
from dataclasses import dataclass

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json


def fingerprint(value):
    return digest(canonical(value))


@dataclass(frozen=True)
class Action:
    name: str
    arguments: bytes

    @classmethod
    def from_function(cls, function):
        args = function["arguments"]
        if isinstance(args, str):
            try:
                args = read_json(args.encode())
            except ValueError:
                # An invalid call is observable. It must not equal a valid JSON string argument.
                return cls(function["name"], b"invalid:" + args.encode())
        return cls(function["name"], canonical(args))

    def record(self):
        if self.arguments.startswith(b"invalid:"):
            return dict(name=self.name, invalid_arguments=self.arguments[8:].decode())
        return dict(name=self.name, arguments=read_json(self.arguments))


@dataclass(frozen=True)
class Step:
    actor: str
    request: str
    controls: str | None
    history: str | None
    text: str | None
    actions: tuple[Action, ...]
    fields: frozenset[str] | None = None

    @property
    def response(self):
        return self.actor, self.text, self.actions

    def selects_single_call(self, name, arguments):
        """Match one business action with the specified argument values after projecting private reads."""
        if len(self.actions) != 1 or self.actions[0].name != name:
            return False
        observed = self.actions[0].record().get("arguments")
        return isinstance(observed, dict) and all(k in observed and canonical(observed[k]) == canonical(value)
                                                  for k, value in arguments.items())


def compare_steps(left, right):
    """Compare reference and candidate. A matched input identifies a contrast, not an individual cause."""
    if left is None or right is None:
        return dict(status="unobserved", change=None)
    if not all((left.controls, right.controls, left.history, right.history)):
        status = "unverified_input"
    elif left.actor != right.actor or left.controls != right.controls:
        status = "unmatched_controls"
    elif left.history != right.history:
        status = "downstream"
    elif left.fields is None or right.fields is None:
        status = "unverified_exposure"
    elif left.fields or right.fields:
        status = "matched_visible_memory"
    else:
        status = "matched_no_visible_memory"

    if left.actions == right.actions:
        change = "wording" if left.text != right.text else "same"
    elif not left.actions or not right.actions:
        change = "call_vs_no_call"
    elif [a.name for a in left.actions] == [a.name for a in right.actions]:
        change = "arguments"
    else:
        change = "function_or_batch"
    comparison = dict(status=status, change=change, reference_request=left.request, candidate_request=right.request,
                      reference_fields=sorted(left.fields) if left.fields is not None else None,
                      candidate_fields=sorted(right.fields) if right.fields is not None else None)
    if left.actions != right.actions:
        comparison.update(reference_actions=[a.record() for a in left.actions],
                          candidate_actions=[a.record() for a in right.actions])
    return comparison


def _first_difference(left, right):
    for i, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return i
    return None if len(left) == len(right) else min(len(left), len(right))


def _at(seq, i):
    return seq[i] if i < len(seq) else None


def compare_episodes(left, right, *, include_choices=False):
    """Separate synchronous choice, wording and the ordered calls actually requested over the episode."""
    index = _first_difference([s.response for s in left], [s.response for s in right])
    result = dict(first_public=None, first_worker_wording=None, first_business_response=None,
                  first_ordered_call=None, next_customer_changed=None, adjacent_customer_changed=None)
    if index is not None:
        a, b = _at(left, index), _at(right, index)
        actor = a.actor if a and b and a.actor == b.actor else "alignment_end"
        result["first_public"] = dict(index=index, actor=actor, **compare_steps(a, b))
        if actor == "worker":
            customers = [next((s for s in seq[index + 1:] if s.actor == "customer"), None) for seq in (left, right)]
            if all(customers):
                result["next_customer_changed"] = customers[0].response != customers[1].response
            adjacent = [_at(seq, index + 1) for seq in (left, right)]
            if all(s and s.actor == "customer" for s in adjacent):
                result["adjacent_customer_changed"] = adjacent[0].response != adjacent[1].response
    workers = [[(i, s) for i, s in enumerate(seq) if s.actor == "worker"] for seq in (left, right)]

    def location(a, b, *, offset):
        ai, sa = a if a else (None, None)
        bi, sb = b if b else (None, None)
        pair = compare_steps(sa, sb)
        # Different positions after a public branch do not re-establish a common decision point.
        if sa is None or sb is None:
            pair = dict(status="unpaired_tail", change=None,
                        reference_request=sa.request if sa else None, candidate_request=sb.request if sb else None)
        elif index is not None and max(ai, bi) > index and pair["status"].startswith("matched_"):
            pair["status"] = "downstream"
        return dict(offset=offset, reference_index=ai, candidate_index=bi, **pair)

    counts = Counter()
    if include_choices:
        result["worker_choices"] = []
    for i in range(max(len(w) for w in workers)):
        choice = location(_at(workers[0], i), _at(workers[1], i), offset=i)
        counts[choice["status"]] += 1
        if include_choices:
            result["worker_choices"].append(choice)
    result["worker_inputs"] = dict(counts)

    for field, key in (("text", "first_worker_wording"), ("actions", "first_business_response")):
        l, r = workers
        offset = _first_difference([getattr(s, field) for _, s in l], [getattr(s, field) for _, s in r])
        if offset is not None:
            a, b = _at(l, offset), _at(r, offset)
            # An extra speech-only turn is not an extra business call. For wording, a
            # missing turn and a tool-only turn likewise have no differing text.
            if a is None or b is None:
                extra = l if b is None else r
                offset = next((k for k in range(offset, len(extra)) if getattr(extra[k][1], field)), None)
            if offset is not None:
                result[key] = location(_at(l, offset), _at(r, offset), offset=offset)

    sequences = [[(i, s, act) for i, s in turns for act in s.actions] for turns in workers]
    l, r = sequences
    offset = _first_difference([a for _, _, a in l], [a for _, _, a in r])
    if offset is not None:
        a, b = _at(l, offset), _at(r, offset)
        result["first_ordered_call"] = location(a[:2] if a else None, b[:2] if b else None, offset=offset)
        result["first_ordered_call"].update(reference_call=a[2].record() if a else None,
                                           candidate_call=b[2].record() if b else None)
    return result


def public_history(messages):
    """Normalize transport call IDs by occurrence; preserve business arguments, order and result linkage."""
    ids = {}
    history = []
    for message in messages:
        record = dict(message)
        if record.get("tool_calls"):
            calls = []
            for call in record["tool_calls"]:
                call_id = call["id"]
                if call_id in ids:
                    raise ContractError("Duplicate tool call ID in public history")
                ids[call_id] = len(ids)
                action = Action.from_function(call["function"])
                calls.append({**call, "id": ids[call_id], "function": {**call["function"],
                              "arguments": action.arguments.decode()}})
            record["tool_calls"] = calls
        if record.get("role") == "tool":
            if record.get("tool_call_id") not in ids:
                raise ContractError("Public tool result without its issuing call")
            record["tool_call_id"] = ids[record["tool_call_id"]]
        history.append(record)
    return fingerprint(history)


def blinded_comparison(history, reference, candidate):
    """Give a reviewer the common decision context and two public choices, with a separate unblinding key."""
    if (reference.actor != "worker" or candidate.actor != "worker" or reference.request == candidate.request
            or not compare_steps(reference, candidate)["status"].startswith("matched_")
            or public_history(history) != reference.history):
        raise ContractError("Blind review requires a verified pair at one worker input")
    pair_id = fingerprint(sorted((reference.request, candidate.request)))
    order = sorted((reference, candidate), key=lambda s: fingerprint([pair_id, s.request]))
    choices = [dict(text=s.text, actions=[a.record() for a in s.actions]) for s in order]
    return (dict(pair_id=pair_id, public_history=history, choices=choices),
            dict(pair_id=pair_id, request_order=[s.request for s in order]))
