# pandas version contrast, 2026-11-08

Evaluation only, frozen before any model prediction. This is one actual return-contract transition with two stable controls, **not** a new family holdout or broad version benchmark.

The successful historical procedure ran under pandas 3.1.0. The current environment may be 3.1.0 or 2.3.3, so this is cross-environment applicability, not necessarily a chronologically older memory. `fillna(inplace=True)` returns the DataFrame in the new runtime and None in the older runtime. The two independently varied fixture inputs agree. `fillna(inplace=False)` and `sort_values(inplace=True)` are stable controls. Both requirements (preserve/mutate original) and four procedures are tested. A version-unobserved condition enumerates the two possible environments.

72 rows represent 18 current states and four source episodes. Only **two known-version candidate pairs change label**; there are only two mixed/insufficient rows. All current pandas 3.1.0 states overlap the development API family. The same-implementation history in the unchanged-version condition is an intentional positive control with a different input fixture. Do not treat these rows as independent experiences or use them for checkpoint/threshold selection.

Source and target executions, exact package versions, row-to-evidence keys and hashes are included. The model sees `input` only. Future target outcomes and auxiliary concept targets are audit/supervision fields.

Primary contract references: [pandas 2.3.3 fillna](https://pandas.pydata.org/pandas-docs/version/2.3.3/reference/api/pandas.DataFrame.fillna.html), [current fillna](https://pandas.pydata.org/docs/dev/reference/api/pandas.DataFrame.fillna.html). The recorded executions, not a floating documentation URL, define this frozen test.
