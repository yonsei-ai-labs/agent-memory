"""Reviewed train experience bank and outcome-blind development pilot intake."""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

from data_pipeline.statebench_data import WORK as PREP, REVISION, canonical, digest, readl, sha, verify, write, writel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "datasets/statebench_pilot/20261112-v1"
WORK = ROOT / "runs/statebench-pilot-20261112-v1"
SALT = "statebench-live-development-20261112-v1"

# Manually reviewed source passages. Takeaways are conditional hypotheses, not
# labels inferred from filenames, subsequent actions, or successful tool status.
# domain, source number, evidence message/call pointers, observation, condition,
# candidate use, and boundary where applying the old procedure is inappropriate.
SPECS = [
 ("customer_support", 1, [(2,3),(3,None),(5,None),(8,1)],
  "A defect return was finalized before symptoms were clarified. The customer later described heavy battery use. A changed-mind preview then returned 'Item already returned'.",
  "A return classification is uncertain, or an earlier return may already be finalized.",
  "Clarify the symptom and inspect the current item status before committing; distinguish an uncommitted preview from a completed return.",
  "Do not reissue a return or reuse the old refund amount when the current item is already returned."),
 ("customer_support", 18, [(1,None),(2,0),(2,1)],
  "An order requested for cancellation was already cancelled in get_order. The trajectory contained read calls and no new cancellation call.",
  "The user asks for cancellation and completion status is not yet known.",
  "Check whether cancellation is already complete before attempting the write.",
  "Once current status is confirmed cancelled, an old cancellation procedure is redundant."),
 ("customer_support", 38, [(2,4),(3,None)],
  "An exchange preview was rejected as outside the exchange window. The customer's next request was an explanation of the window, not a retry.",
  "An eligibility rejection is observed and the user now asks why it was rejected.",
  "Use the observed reason and current delivery/policy facts to explain eligibility; distinguish explanation from another exchange attempt.",
  "The historical date and window length are not the current customer's deadline; the source's current date was redacted."),
 ("customer_support", 48, [(1,None),(3,None),(4,0),(4,1)],
  "For 'the little add-on', the assistant suggested a phone case; the user corrected the target to a USB hub and said to keep the case. Only the hub was submitted for return.",
  "The requested item is ambiguous in a multi-item order.",
  "Resolve the target and preserve the items the user wants to keep before preparing a return.",
  "Do not substitute the past product identifier or ask again after the current user has already identified the target."),
 ("customer_support", 76, [(2,3),(3,None),(4,0)],
  "A return preview had zero restocking fee but a nonzero shipping clawback. After the customer confirmed that separate adjustment, the return used the preview amount.",
  "A membership benefit waives one fee and a refund still has other components.",
  "Keep fee components separate and verify the current preview before confirming the refund.",
  "A restocking waiver does not by itself establish that every shipping adjustment is waived; historical amounts are not current amounts."),
 ("customer_support", 46, [(1,None),(2,0),(2,1),(5,None)],
  "The requested order belonged to another account. The current customer confirmed they were not the verified owner and asked that no order changes be made. The source contains no write call.",
  "A return request may concern another account's order.",
  "Resolve ownership using the current account and allowed process before an order mutation.",
  "Do not treat a claimed partner relationship as verified authority, or copy another customer's identifiers."),
 ("customer_support", 103, [(2,0),(2,2),(2,4),(2,5)],
  "A price-match request involved two similarly named headphones with different product identifiers. The agent inspected details for both before explaining the distinction.",
  "A quoted cheaper listing may differ from the purchased product.",
  "Match the order's exact product and current listing before computing any price adjustment.",
  "A related product name or a cheaper different variant is insufficient evidence for the requested refund."),
 ("customer_support", 114, [(1,None),(2,4),(2,5)],
  "The customer requested a larger shirt and explicitly kept the book. Exchange preview and confirmation targeted the shirt item only.",
  "One line of a mixed order should change while another stays unchanged.",
  "Carry the user's scope through the preview and confirmed action.",
  "Do not expand a one-item exchange into a whole-order change or reuse the old item ID."),
 ("customer_support", 121, [(1,None),(2,5),(3,None),(5,None),(6,0)],
  "The customer used the word damaged but described receiving the wrong kitchen item. The observed preview and confirmed return used reason wrong_item after clarification and consent.",
  "The user's category word and concrete description point to different issue types.",
  "Use the described event and current evidence to choose a return reason, then verify the preview and requested action.",
  "Do not classify only from the word damaged, or copy the old refund figures."),
 ("customer_support", 149, [(1,None),(2,3),(3,None),(4,0)],
  "An unspecified accessory was cancelled before its identity was confirmed. The customer objected, and a later order lookup showed a partially cancelled order.",
  "A requested cancellation target is ambiguous, especially after a previous write.",
  "Before a write, clarify the target. After a write, inspect the actual state and explain what changed before proposing more action.",
  "A cancellation success response is not proof that the intended item was chosen; do not assume the original state is intact."),
 ("shopping_assistant", 25, [(2,0),(2,1),(3,None),(4,0)],
  "Adding three more units failed because two were already present and the per-cart cap was three. After the user agreed to one more, adding one produced quantity three.",
  "An additive request interacts with existing quantity and a quantity cap.",
  "Compare existing quantity, requested increment and current cap; obtain agreement before reducing the requested increment.",
  "Do not confuse add quantity with target quantity or silently truncate the request; recompute current quantities."),
 ("shopping_assistant", 140, [(2,0),(2,3),(2,4),(2,5)],
  "A new points redemption was rejected because a redemption already existed. Cancelling that redemption and applying a newly computed amount then returned redeemed.",
  "The user wants to change an existing loyalty redemption.",
  "Inspect existing redemption and current balance/cap before replacing the amount.",
  "This is not a general instruction to cancel points use: preserve a satisfactory current redemption and respect the user's latest preference."),
 ("shopping_assistant", 84, [(1,None),(2,0),(2,1),(3,None),(4,0)],
  "The customer wanted the webcam removed entirely. The remove call deleted that product line, and a later cart lookup retained the laptop.",
  "The user requests all of a product removed while other products should remain.",
  "Distinguish removing a line from reducing its quantity, and verify unaffected items remain.",
  "Do not remove the whole line for a request to remove only one unit."),
 ("shopping_assistant", 15, [(3,None),(4,1),(4,2),(5,None),(6,0)],
  "Searching with out-of-stock products included found the customer's cheaper previous-generation model. The user declined backorder and the more expensive substitute; the later cart was empty.",
  "The desired product is absent from an in-stock-only search and the user has a budget constraint.",
  "Distinguish no in-stock match from no catalog entry, and ask before substituting or backordering.",
  "Do not treat a prior refusal or historical availability as the new customer's choice or current stock."),
 ("shopping_assistant", 79, [(2,6),(3,None),(4,0),(4,1),(4,2)],
  "After quantities increased, cart subtotal changed while the stored standard-shipping charge remained the same. The source alone does not certify that charge as policy-correct.",
  "A cart mutation occurs after a shipping option has been set.",
  "Inspect current cart and shipping policy before treating the old shipping charge as a newly verified amount.",
  "Do not claim the old fee is correct or that every quantity change changes shipping; check the present conditions."),
 ("shopping_assistant", 93, [(2,0),(2,1),(3,None),(4,0),(5,None)],
  "Reducing a two-laptop cart changed its discounted total. Restoring the second laptop produced a different total from the original two-laptop cart; the user then declined further points use.",
  "Quantity changes have affected a cart with an existing loyalty redemption.",
  "Inspect current redemption and total after a reversal; restoring quantity does not establish that the old discount was restored.",
  "Do not automatically spend more points when the current user declines them; do not infer an unseen balance from the agent's prose."),
 ("shopping_assistant", 107, [(1,None),(2,3),(3,None)],
  "The customer required two units of a particular size/color and rejected partial quantity. get_variants showed only one of that variant; no add call followed.",
  "Variant-specific stock may be less than a requested all-or-nothing quantity.",
  "Check the exact variant's stock and honor the requested quantity boundary.",
  "Product-level in_stock is insufficient for two units of a particular variant; do not silently add the available subset."),
 ("shopping_assistant", 81, [(1,None),(2,1),(2,2),(3,None)],
  "The customer requested a laptop comparison and explicitly said they were not ready to buy. The observed calls inspected product information; no cart write occurred.",
  "A comparison or recommendation request does not yet authorize purchase preparation.",
  "Supply the requested information without turning consideration into an add-to-cart action.",
  "When the current user later explicitly requests a purchase action, the earlier no-write intent must not be treated as permanent."),
 ("shopping_assistant", 52, [(4,0),(4,1),(7,None),(8,0)],
  "The laptop was added, but promo validation returned code not found. The user later rejected alternative promos and points; the final cart lookup showed no applied promo.",
  "A product addition succeeded while a requested promotion failed.",
  "Report the separate outcomes and base further action on the latest user choice.",
  "A not-found result does not prove expiry; do not repeat the product addition or silently apply a replacement promo."),
 ("shopping_assistant", 69, [(4,0),(5,None),(6,1),(6,2),(6,3)],
  "After a laptop was added, the user asked to switch products. Adding the replacement temporarily left both in the cart; removing the original and reading the cart showed only the replacement.",
  "The current request replaces an item already in the cart.",
  "Treat a replacement as a state transition involving the old line as well as the new one, and verify the resulting cart.",
  "Do not remove the old product when the request is to add another, or repeat removal after the switch is already complete."),
]


def build():
    verify(PREP)
    if (DATA / "bank-manifest.json").exists():
        raise FileExistsError("The reviewed bank is frozen")
    sources = readl(PREP / "prepared/sources.jsonl")
    bank, evidence = [], []
    for domain, number, refs, observation, condition, use, boundary in SPECS:
        s = next(r for r in sources if r["domain"] == domain and Path(r["path"]).name.startswith(str(number) + "-"))
        if s["split"] != "memory_source":
            raise ValueError("Review tried to use development material")
        conversation = json.loads((PREP / "upstream" / s["path"]).read_text())["conversation"]
        mid = "experience-" + digest(s["source_id"] + "-review-v1")[:16]
        for mi, ci in refs:
            m = conversation[mi]
            value = m if ci is None else m["tool_calls"][ci]
            evidence.append({"memory_id": mid, "source_id": s["source_id"], "source_path": s["path"],
                "source_sha256": s["sha256"], "message_index": mi, "call_index": ci,
                "observed_value": value, "observed_sha256": digest(canonical(value))})
        text = ("Past observation: " + observation + "\nPotentially useful when: " + condition
                + "\nConditional takeaway to check now: " + use + "\nBoundary: " + boundary)
        bank.append({"memory_id": mid, "source_id": s["source_id"], "source_group": s["group_id"],
            "domain": domain, "source_split": "memory_source", "text": text,
            "review_status": "approved_for_pilot", "reviewer": "single assistant, specified evidence passages",
            "source_success": None, "utility_label": None, "interpretation": "conditional hypothesis, not certified successful procedure"})
    writel(DATA / "bank.jsonl", bank)
    writel(DATA / "evidence.jsonl", evidence)
    selected = []
    for domain in ("customer_support", "shopping_assistant"):
        eligible = [s for s in sources if s["domain"] == domain and s["split"] == "development"]
        selected += sorted(eligible, key=lambda s: digest(SALT + s["source_id"]))[:4]
    write(DATA / "pilot-selection.json", {"salt": SALT, "sources": selected,
        "selection": "first four hash-ordered development sources per domain, before task/env contents or new outcomes",
        "repeats": 1, "policies": ["none", "harrier_goal", "harrier_state"], "official_test_tasks": 0})
    write(DATA / "review-exclusions.json", [
        {"domain": "shopping_assistant", "source_number": 70, "reason": "Expiry appears in filename but is not established by the observed get_cart result; do not invent a stale-promo lesson."},
        {"domain": "shopping_assistant", "source_number": 143, "reason": "Assistant claims invalid identifier; no observed failed lookup establishes that error."},
        {"domain": "shopping_assistant", "source_number": 145, "reason": "Policy-only exchange; retain as potential policy-cache control, not an experience transition in this pilot."}])
    write(DATA / "bank-manifest.json", {"revision": REVISION, "files": {p.name: sha(p) for p in DATA.iterdir() if p.is_file()},
        "memories": len(bank), "independent_sources": len({m["source_id"] for m in bank}),
        "evidence_passages": len(evidence), "actual_utility_labels": 0,
        "design_limit": "manual training-source coverage sample; no exhaustive or independent semantic review", "code_sha256": sha(__file__)})
    return verify_bank()


def verify_bank():
    manifest = json.loads((DATA / "bank-manifest.json").read_text())
    for name, expected in manifest["files"].items():
        if sha(DATA / name) != expected:
            raise ValueError("Changed bank artifact: " + name)
    sources = {s["source_id"]: s for s in readl(PREP / "prepared/sources.jsonl")}
    bank = readl(DATA / "bank.jsonl")
    evidence = readl(DATA / "evidence.jsonl")
    for e in evidence:
        s = sources[e["source_id"]]
        if s["split"] != "memory_source" or sha(PREP / "upstream" / s["path"]) != e["source_sha256"]:
            raise ValueError("Changed or non-training source")
        m = json.loads((PREP / "upstream" / s["path"]).read_text())["conversation"][e["message_index"]]
        expected = m if e["call_index"] is None else m["tool_calls"][e["call_index"]]
        if expected != e["observed_value"] or digest(canonical(expected)) != e["observed_sha256"]:
            raise ValueError("Evidence differs from source")
    dev = json.loads((DATA / "pilot-selection.json").read_text())["sources"]
    if {s["group_id"] for s in dev} & {m["source_group"] for m in bank}:
        raise ValueError("Source overlap")
    return {"status": "passed", "memories": len(bank), "evidence_passages": len(evidence), "pilot_sources": len(dev),
            "utility_labels": 0, "official_test_content_read": False}


def acquire_runtime():
    verify_bank()
    if (WORK / "runtime-manifest.json").exists():
        raise FileExistsError("Frozen runtime exists")
    snapshot = json.loads((PREP / "source-snapshot.json").read_text())
    root = WORK / "upstream"
    for name in snapshot["files"]:
        dest = root / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PREP / "upstream" / name, dest)
    dev = json.loads((DATA / "pilot-selection.json").read_text())["sources"]
    paths = subprocess.check_output(["git", "-C", str(PREP / "upstream"), "ls-tree", "-r", "--name-only", REVISION], text=True).splitlines()
    extra = []
    for s in dev:
        for folder in ("tasks", "task_envs"):
            path = f"state_bench/domains/{s['domain']}/{folder}/{Path(s['path']).name}"
            if path not in paths:
                raise ValueError("Missing initial task environment")
            extra.append(path)
    extra += [p for p in paths if any(p.startswith(f"state_bench/domains/{d}/prompts/") for d in ("customer_support", "shopping_assistant")) and p.endswith(".md")]
    raw = subprocess.check_output(["git", "-C", str(PREP / "upstream"), "archive", REVISION, *extra])
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for entry in archive.getmembers():
            if entry.isfile():
                if entry.name not in extra:
                    raise ValueError("Unexpected runtime content")
                dest = root / entry.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(archive.extractfile(entry).read())
    write(WORK / "runtime-manifest.json", {"revision": REVISION,
        "files": {p: sha(root / p) for p in sorted(set(snapshot["files"]) | set(extra))},
        "bank_manifest_sha256": sha(DATA / "bank-manifest.json"),
        "scope": "selected development task/env content only; harness and judge access, never selector input",
        "official_test_tasks_read": 0})
    return {"runtime_files": len(snapshot["files"]) + len(extra), "development_tasks": len(dev)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["build", "verify_bank", "acquire_runtime"])
    args = parser.parse_args()
    print(json.dumps(globals()[args.command](), indent=2))
