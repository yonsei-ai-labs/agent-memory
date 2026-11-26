"""Apply one proposed change to each registered memory representation."""

import re

from agent_memory_eval.contracts.core import ContractError, digest


def writer_pairs(forms, clause):
    """Return executable conditions and explicit reference/candidate IDs by representation."""
    if not isinstance(forms, dict) or not forms or not isinstance(clause, str) or not clause.strip():
        raise ContractError("A writing comparison needs named representations and a nonempty writer clause")
    conditions, pairs = [], {}
    used = {"no_memory", "empty_preload", "public_record"}
    for form, prompt in forms.items():
        if not isinstance(form, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,52}", form):
            raise ContractError("Use a short, distinct identifier for each memory representation")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ContractError("Each representation needs its original writer instruction")
        candidate = form + "__candidate"
        if {form, candidate} & used:
            raise ContractError("Duplicate or reserved writer condition")
        used.update((form, candidate))
        pairs[form] = {"reference": form, "candidate": candidate}
        for arm, text in ((form, prompt), (candidate, clause + "\n" + prompt)):
            conditions.append(dict(id=arm, representation=form, prompt=text,
                                   prompt_version=digest(text.encode())))
    return conditions, pairs
