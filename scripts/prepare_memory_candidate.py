"""Export public development evidence or compile a cited writer proposal into a new unbudgeted draft."""

import argparse
from copy import deepcopy
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_write import load_form_sources
from agent_memory_eval.experiments.prompt_comparison import FIRST_PASS_FORMAT, PAIRED_FORMS_FORMAT, compile_config, verify_proposal_quotes
from agent_memory_eval.recording.assets import asset_text, publish_json


def development_packet(root, workspace, config_bytes):
    config = read_json(config_bytes)
    if not isinstance(config, dict) or config.get("format") not in (FIRST_PASS_FORMAT, PAIRED_FORMS_FORMAT):
        raise ContractError("Writer proposals require a first-pass comparison")
    design = compile_config(root, config)
    development = design["task_sets"]["development"]
    if not development:
        raise ContractError("Select development tasks to supply evidence for a proposal")
    selected = dict(config, task_sets=dict(development=development, evaluation=[]))
    sources = load_form_sources(compile_config(root, selected), workspace)
    cases = {}
    for task in development:
        source = sources[task]
        cases[task] = dict(public_context_sha256=source.public_context_sha256,
                           first_pass_success=source.official_success,
                           messages=read_json(source.public_context.encode())["messages"])
    multi = config["format"] == PAIRED_FORMS_FORMAT
    reference = ({"incumbents": [c for c in design["conditions"] if c["id"] in config["forms"]],
                  "common_writer_instruction": asset_text(root, config["common_writer_ref"]).rstrip("\r\n")}
                 if multi else {"incumbent": design["conditions"][0]})
    return dict(parent_config_sha256=digest(config_bytes), **reference,
                development=cases,
                request="Propose one reusable change to the writer instruction. Cite public development messages "
                        "and predict an observable business action. Keep individual answers and identifiers out of the policy.",
                proposal_fields=["parent_config_sha256", "hypothesis", "expected_action", "writer_clause" if multi else "writer_prompt", "evidence"],
                citation_fields=["task_id", "pointer", "quote"])


def candidate_draft(root, config_bytes, packet, proposal):
    parent_sha = digest(config_bytes)
    draft = read_json(config_bytes)
    multi = draft["format"] == PAIRED_FORMS_FORMAT
    change = "writer_clause" if multi else "writer_prompt"
    if (not isinstance(proposal, dict) or not isinstance(proposal.get(change), str)
            or proposal.get("parent_config_sha256") != parent_sha or packet["parent_config_sha256"] != parent_sha):
        raise ContractError("A writer proposal must identify the supplied parent configuration")
    suffix = digest(canonical(proposal))[:16]
    if multi:
        draft["writer_clause"] = proposal[change]
    else:
        incumbent = draft["conditions"][0]
        candidate = dict(id="writer_" + suffix, representation=incumbent["representation"],
                         prompt_version="writer-" + suffix, prompt=proposal[change])
        draft["conditions"] = [incumbent, candidate]
    draft.update(name="memory-candidate-" + suffix, status="draft", guards=None,
                 candidate_proposal=deepcopy(proposal),
                 task_sets=dict(development=draft["task_sets"]["development"], evaluation=[]),
                 selection_reason="Use the registered development cases for this proposal. Confirmation cases remain in the parent study.")
    compile_config(root, draft)
    verify_proposal_quotes(proposal, {task: row["messages"] for task, row in packet["development"].items()})
    return draft


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True, help="New output file in an existing directory")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--development-packet", action="store_true")
    mode.add_argument("--proposal", type=Path, help="JSON containing the writer change, hypothesis and development citations")
    args = parser.parse_args()
    config_bytes = args.config.read_bytes()
    packet = development_packet(ROOT, args.workspace, config_bytes)
    value = packet if args.development_packet else candidate_draft(
        ROOT, config_bytes, packet, read_json(args.proposal.read_bytes()))
    publish_json(args.output, value)
    print(args.output)


if __name__ == "__main__":
    main()
