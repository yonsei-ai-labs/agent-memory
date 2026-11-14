# Historical value masking: design-only intervention

Retains all 47 reviewed v2 design cases, their questions, labels and current evidence. Two new arms mark or mask manually reviewed historical spans. These arms have equal worker-tokenizer lengths. Original and no-memory outputs are reused only with identical messages and fixed worker provenance.

- `span-reviews.jsonl`: 61 source-anchored edits; 45 quantity-only cases and 2 broader enumeration cases.
- `transformations.jsonl`: exact before/marked/masked text, edits and token counts for 51 records.
- `decisions.jsonl`, `jobs.jsonl`: 188 policy inputs; 94 new worker calls.
- `cases.jsonl`, `annotations.jsonl`: unchanged subset of v2 originals.
- `manifest.json`: frozen data hashes. This README is descriptive, outside the manifest.

See [design plan](../../../docs/ai-memory-external-update-masking-2026-11-11.md). This is a source-aware diagnostic intervention, not an automated masking model or an official benchmark score. BEAM confirmation remains unexecuted.
