# API procedure transfer diagnostic, 2026-11-08 v1

Authored, executed API fixtures for deciding whether a successful past code procedure can be reused under the current return/mutation requirements. This is **not** a collection of naturally occurring SWE trajectories or evidence that showing memories helps an LLM worker.

- 34 concrete implementations in 7 library families; 2 confirmation inputs each; 544 fresh candidate executions.
- 96 current states with 4 candidates each: 200 train, 72 dev, 112 test rows.
- A candidate may be supported, conflicting, or unresolved across two explicitly possible current implementations. Multiple candidates can be valid; some states have none.
- Train families: NumPy, SciPy sparse, Pillow, Python containers. Dev: pandas. Test: NetworkX, scikit-learn. All source histories stay with their family.
- 62 successful source episodes are actually used; the two inputs agreed for every implementation/goal/procedure. Derived rows and fixtures are not independent experiences.

The model sees only `input`. `evidence`, `label`, source identifiers and outcome metadata are supervision/audit material. `input.world_texts` contains visible possible implementations, not execution results. Target evidence uses fixture 1, history uses fixture 0, and an exact target implementation cannot also be its candidate history.

Recheck without GPU or third-party packages:

```sh
python -m data_pipeline.audit_api_transfer datasets/api_transfer/20261108-v1
```

For a fresh execution in a separate environment, install `requirements-runtime.txt` and run `python -m data_pipeline.api_transfer_runtime --output /new/empty/path`. The exact producer files are snapshotted in `reproduction/`. Runtime dependencies are for the fixture audit only; the neural trainer does not execute library fixtures.

Values/types and receiver effects are checked on two inputs. This is not a universal program verification result. The four generic code patterns and shared package conventions make this a deliberately narrow diagnostic. Both source and current episodes are constructed, not historical events from a real agent timeline. The artifact index was frozen before any trained-model prediction on this panel.
