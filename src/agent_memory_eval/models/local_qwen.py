"""Text-only, greedy Qwen inference over a private pipe to an optional Transformers environment."""

import hashlib
import json
import os
from collections import defaultdict, deque
from pathlib import Path
import re
import subprocess
import sys


def public_reply(text, tools):
    """Decode Qwen's native XML-style calls. Keep undecodable replies available for audit."""
    # This text checkpoint can emit the chat end marker followed by a newline and model EOS.
    # Remove only the terminal chat framing. Preserve the unmodified generation separately.
    text = text.rstrip().removesuffix("<|im_end|>").rstrip()
    if text.lstrip().startswith("<think>") and "</think>" in text:
        text = text.split("</think>", 1)[1].lstrip()
    if not any(tag in text for tag in ("<tool_call", "<function=", "</tool_call>")):
        return {"role": "assistant", "content": text.strip()}, None
    schemas = {t["function"]["name"]: t["function"]["parameters"].get("properties", {}) for t in tools}
    prefix, _, tail = text.partition("<tool_call>")
    chunks = re.findall(r"<tool_call>\s*<function=([^>\s]+)>(.*?)</function>\s*</tool_call>", "<tool_call>" + tail, re.S)
    calls = []
    remaining = "<tool_call>" + tail
    try:
        for name, body in chunks:
            block = re.match(r"<tool_call>\s*<function=" + re.escape(name) + r">(.*?)</function>\s*</tool_call>", remaining, re.S)
            if not block or block[1] != body:
                raise ValueError("incomplete tool call")
            remaining = remaining[block.end():].strip()
            args = {}
            rest = body.strip()
            while rest:
                param = re.match(r"<parameter=([^>\s]+)>(.*?)</parameter>", rest, re.S)
                if not param or param[1] in args:
                    raise ValueError("invalid or duplicate parameter")
                key, value = param[1], param[2].strip()
                kind = schemas.get(name, {}).get(key, {}).get("type")
                if isinstance(kind, list):
                    if value == "null" and "null" in kind:
                        value = None
                    kind = next((k for k in kind if k != "null"), None)
                if value is not None and kind in ("integer", "number", "float", "boolean", "array", "object", "dict"):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        pass  # Preserve the model's value; schema diagnostics can identify the type mismatch.
                args[key] = value
                rest = rest[param.end():].strip()
            calls.append(dict(id=f"call_{len(calls)}", type="function",
                              function=dict(name=name, arguments=json.dumps(args, ensure_ascii=False))))
        if not calls or remaining or "<function=" in prefix:
            raise ValueError("malformed tool-call envelope")
    except ValueError as error:
        return {"role": "assistant", "content": text.strip()}, str(error)
    return {"role": "assistant", "content": prefix.strip() or None, "tool_calls": calls}, None


def required_prefix(request):
    choice = request.get("tool_choice")
    if choice is None or choice in ("auto", "none"):
        return ""
    names = [t["function"]["name"] for t in request["tools"]]
    if choice == "required" and len(names) == 1:
        name = names[0]
    elif (isinstance(choice, dict) and choice.get("type") == "function" and isinstance(choice.get("function"), dict)
          and choice["function"].get("name") in names):
        name = choice["function"]["name"]
    else:
        raise ValueError("The local transport requires one declared function for constrained decoding")
    return "<tool_call>\n<function=" + name + ">\n"


def stop_ids(model_eos, chat_eos, *, stop_at_chat_end):
    ids = list(model_eos) if isinstance(model_eos, (tuple, list)) else [model_eos]
    if stop_at_chat_end:
        if not isinstance(chat_eos, int):
            raise ValueError("Missing tokenizer chat-end token")
        if chat_eos not in ids:
            ids.append(chat_eos)
    return ids


class LocalQwen:
    """An observed local transport. Each generation starts from the complete recorded request."""

    def __init__(self, python, checkpoint, model, seed, *, stop_at_chat_end=False):
        self.model = dict(id=model, selected_endpoint={"provider_name": "local-transformers"})
        self.saved = defaultdict(deque)
        self.worker = subprocess.Popen(
            [str(python), "-u", __file__, str(checkpoint), str(seed)] + (["--chat-end"] if stop_at_chat_end else []),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, encoding="utf-8", env={**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                                              "TOKENIZERS_PARALLELISM": "false"})
        try:
            self.runtime = self.read()
            if self.runtime.get("checkpoint") != Path(checkpoint).name:
                raise ValueError("Local model did not load the requested checkpoint")
        except BaseException:
            self.close()
            raise

    def read(self):
        line = self.worker.stdout.readline()
        if not line:
            raise RuntimeError("Local inference process exited without a response")
        result = json.loads(line)
        if "error" in result:
            raise RuntimeError(result["error"])
        return result

    def respond(self, request):
        if request["model"] != self.model["id"]:
            raise ValueError("Different local model requested")
        key = self.wire_key(request)
        reused = None
        if self.saved.get(key):
            output, reused = self.saved[key].popleft()
            output = {k: v for k, v in output.items() if k != "decode_error"}
        else:
            self.worker.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            self.worker.stdin.flush()
            output = self.read()
        message, error = public_reply(output["text"], request.get("tools", []))
        tag = output["input_sha256"][:12]
        for c in message.get("tool_calls", []):
            c["id"] = tag + "_" + c["id"]
        finish = "length" if not output["ended"] else ("tool_calls" if message.get("tool_calls") else "stop")
        if error:
            output["decode_error"] = error
        result = dict(model=self.model["id"], provider="local-transformers", local_generation=output,
                    usage=dict(prompt_tokens=output["prompt_tokens"], completion_tokens=output["completion_tokens"],
                               total_tokens=output["prompt_tokens"] + output["completion_tokens"], cost=0),
                    choices=[dict(message=message, finish_reason=finish)])
        if reused is not None:
            result["reused_local_generation"] = reused
        return result

    @staticmethod
    def wire_key(request):
        return json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    def close(self):
        self.worker.stdin.close()
        try:
            self.worker.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.worker.kill()
            self.worker.wait()
        self.worker.stdout.close()


def serve(checkpoint, seed, *, stop_at_chat_end=False):
    import torch
    import transformers
    from transformers import AutoTokenizer, Qwen3_5ForCausalLM

    tokenizer = AutoTokenizer.from_pretrained(checkpoint, local_files_only=True)
    model, loading = Qwen3_5ForCausalLM.from_pretrained(
        checkpoint, local_files_only=True, dtype=torch.bfloat16, attn_implementation="sdpa",
        device_map={"": "cuda"}, output_loading_info=True)
    if loading.get("missing_keys") or loading.get("mismatched_keys"):
        raise ValueError("The local text model has uninitialized parameters")
    model.eval()
    eos = stop_ids(model.generation_config.eos_token_id, tokenizer.eos_token_id, stop_at_chat_end=stop_at_chat_end)
    generation_options = {"eos_token_id": eos} if stop_at_chat_end else {}
    def send(obj):
        print(json.dumps(obj, ensure_ascii=False, allow_nan=False), flush=True)
    runtime = dict(checkpoint=Path(checkpoint).name, torch=torch.__version__, transformers=transformers.__version__,
                   device=torch.cuda.get_device_name(), dtype="bfloat16", attention="sdpa", seed=seed)
    if stop_at_chat_end:
        runtime["stop_token_ids"] = eos
    send(runtime)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            messages = request["messages"]
            for message in messages:
                for call in message.get("tool_calls") or []:
                    args = call["function"]["arguments"]
                    if isinstance(args, str):
                        call["function"]["arguments"] = json.loads(args)
            prompt = tokenizer.apply_chat_template(messages, tools=request.get("tools") or None, tokenize=False,
                                                   add_generation_prompt=True, enable_thinking=False)
            prefix = required_prefix(request)
            rendered = prompt + prefix
            inputs = tokenizer(rendered, add_special_tokens=False, return_tensors="pt").to(model.device)
            torch.manual_seed(seed)
            with torch.inference_mode():
                out = model.generate(**inputs, max_new_tokens=request["max_completion_tokens"], do_sample=False,
                                     pad_token_id=tokenizer.pad_token_id, return_dict_in_generate=False, **generation_options)
            ids = out[0, inputs.input_ids.shape[1]:].tolist()
            ended = bool(ids) and ids[-1] in eos
            decoded = tokenizer.decode(ids[:-1] if ended else ids, skip_special_tokens=False)
            send(dict(text=prefix + decoded, emitted_text=decoded, forced_prefix=prefix, ended=ended,
                      rendered_input=rendered, input_sha256=hashlib.sha256(rendered.encode()).hexdigest(),
                      prompt_tokens=inputs.input_ids.shape[1], completion_tokens=len(ids), output_token_ids=ids))
        except Exception as error:
            send(dict(error=f"{type(error).__name__}: {error}"))


if __name__ == "__main__":
    serve(sys.argv[1], int(sys.argv[2]), stop_at_chat_end="--chat-end" in sys.argv[3:])
