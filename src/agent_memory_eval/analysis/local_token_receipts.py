"""Independently reconstruct a recorded local Qwen input and decode its saved output IDs, without loading model weights."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def blob(folder, ref):
    raw = (folder / ref["path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        raise ValueError("Changed recorded content")
    return json.loads(raw)


def verify(tokenizer, checkpoint, root, suffix):
    runtime = json.loads((root / "runtime.json").read_bytes())
    if checkpoint.name != runtime["checkpoint"]:
        raise ValueError("Tokenizer revision does not match the recording")
    counts = Counter()
    for log in sorted(root.glob("*/log.jsonl")):
        events = [e for line in log.read_bytes().splitlines() for e in json.loads(line)["events"]
                  if (e["run_id"] or "").endswith(suffix)]
        finishes = {e["payload"]["request_id"]: e["payload"] for e in events if e["type"] == "model.request_finished"}
        for event in events:
            if event["type"] != "model.request_started":
                continue
            req = event["payload"]
            wire = blob(log.parent, req["request_ref"])
            gen = blob(log.parent, finishes[req["request_id"]]["response_ref"])["local_generation"]
            for message in wire["messages"]:
                for call in message.get("tool_calls") or []:
                    args = call["function"]["arguments"]
                    if isinstance(args, str):
                        call["function"]["arguments"] = json.loads(args)
            rendered = tokenizer.apply_chat_template(wire["messages"], tools=wire["tools"], tokenize=False,
                                                      add_generation_prompt=True, enable_thinking=False) + gen["forced_prefix"]
            ids = gen["output_token_ids"]
            emitted = tokenizer.decode(ids[:-1] if gen["ended"] else ids, skip_special_tokens=False)
            if (rendered != gen["rendered_input"] or hashlib.sha256(rendered.encode()).hexdigest() != gen["input_sha256"]
                    or len(tokenizer.encode(rendered, add_special_tokens=False)) != gen["prompt_tokens"]
                    or emitted != gen["emitted_text"] or gen["forced_prefix"] + emitted != gen["text"]):
                raise ValueError(f"Tokenizer or output mismatch at {req['request_id']}")
            counts.update(verified_requests=1, prompt_tokens=gen["prompt_tokens"], generated_tokens=len(ids))
    if not counts:
        raise ValueError("No matching observed requests to check")
    return dict(recording=root.name, run_suffix=suffix, **counts)


if __name__ == "__main__":
    from transformers import AutoTokenizer

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--readers", nargs=2, action="append", required=True, metavar=("RECORDING", "RUN_SUFFIX"))
    args = parser.parse_args()
    tok = AutoTokenizer.from_pretrained(args.checkpoint, local_files_only=True)
    print(json.dumps([verify(tok, args.checkpoint, Path(root), suffix) for root, suffix in args.readers], sort_keys=True))
