from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_memory_eval.analysis.clustered_outcomes import clustered_contrast
from agent_memory_eval.contracts.core import ContractError, canonical, digest
from agent_memory_eval.experiments.paired_repetitions import PairedRepetitions
from scripts import analyze_banking_policy_cycle as analysis
from scripts import run_banking_policy_cycle as cycle
from scripts.run_full_retail import release_groups

PROFILE = "protocols/pilots/tau-banking-opening-writer-pairs-v2.json"


class RepeatedWriterCohortTests(unittest.TestCase):
    def test_nested_weights_missing_bounds_and_sign_unit(self):
        rows = [("a", f"a{i}", "fact", 1, False, True) for i in range(5)]
        rows += [("b", "b", "fact", 1, True, False)]
        score = clustered_contrast(rows, cluster_unit="customer")
        self.assertEqual((score["assigned_pairs"], score["clusters"], score["difference"]), (6, 2, 0))
        self.assertEqual((score["missing_bounds"], score["two_sided_cluster_sign_p"]), ([0, 0], 1))
        rows += [("b", "b", "episode", 1, False, None)]
        score = clustered_contrast(rows, cluster_unit="customer")
        self.assertEqual((score["difference"], score["observed_clusters"], score["missing_bounds"]), (1, 1, [0.25, 0.5]))
        with self.assertRaises(ContractError):
            clustered_contrast(rows + rows[:1], cluster_unit="customer")

    def test_historical_names_and_invalid_repeat_count(self):
        case = {"task_id": "t", "task": "t"}
        cells = list(PairedRepetitions().writer_cells(case, ["existing", "candidate"]))
        self.assertEqual([v["task"] for _, v in cells], ["t__existing", "t__candidate"])
        self.assertNotIn("writer_draw", cells[0][1])
        for value in (0, True, -2, 1.5):
            with self.assertRaises(ContractError):
                PairedRepetitions(2, value)

    def _simulate(self, workspace, *, with_sources):
        # Only the model-executing dispatcher is replaced. Both registration phases and all slot IDs are real.
        spec = cycle.read(cycle.ROOT / PROFILE)
        check = cycle.read(cycle.ROOT / spec["preflight_file"])
        cases = deepcopy(check["cases"])
        native = {case["task"]: {} for case in cases}
        phases = []

        def dispatch(workspace, cfg, _, conn, cells, tasks, record, **kwargs):
            root = workspace / "runs" / cfg["name"]
            root.mkdir(parents=True)
            cycle.save(root / "registration.json", dict(preflight=record))
            release = release_groups(cfg, cells)
            phases.append((deepcopy(cfg), deepcopy(cells), release))
            seals = {}
            for _, stage, members in release:
                for cell in members:
                    path = Path("task-" + cell["task"]) / stage
                    if stage == "source" and with_sources:
                        shard = root / path
                        shard.mkdir(parents=True)
                        cycle.save(shard / "completion.json", {"offline_test_fixture": True})
                        seals[str(path)] = dict(seal=digest((shard / "completion.json").read_bytes()))
                    else:
                        seals[str(path)] = dict(status="not_started", reported="0", held="0", http=0)
            cycle.save(root / "completion.json", dict(stages=seals, total_covered_usd="0",
                       registration_sha256=digest((root / "registration.json").read_bytes())))

        common = dict(protocol_sha256=digest((cycle.ROOT / PROFILE).read_bytes()))
        source = SimpleNamespace(source_run_id="fixture", source_log_sha256="x", public_context_sha256="y")
        with patch.object(cycle.bank.matched, "run_study", side_effect=dispatch), \
             patch.object(cycle.bank.matched, "source_from", return_value=source), \
             patch.object(cycle, "writer_hashes", return_value={}):
            for mode in ("sources", "validation"):
                cycle.run_paired_validation(workspace, mode, spec, spec, Decimal("0"), {}, {}, cases, native, common)
        return spec, common, phases

    def test_dispatches_all_readers_to_their_registered_bank(self):
        with TemporaryDirectory() as tmp:
            spec, _, phases = self._simulate(Path(tmp), with_sources=True)
            _, _, source_release = phases[0]
            self.assertEqual(len(source_release[0][2]), 19)
            cfg, cells, release = phases[1]
            self.assertEqual(len(cells), 76)
            self.assertEqual([s for _, s, _ in release], ["write", "solve", "solve-repeat-2", "solve-repeat-3"])
            jobs, counts, writers = set(), {"write": 0, "solve": 0}, set()
            for _, name, admitted in release:
                self.assertEqual(len(admitted), 76)
                kind = cfg["stage_kinds"][name]
                for cell in admitted:
                    self.assertEqual(cell["scheduled_stages"][:2], ["source", "write"])
                    for j in cycle.bank.matched.jobs_for(cell, kind):
                        form = j["arm"].split("__")[0]
                        bank = cell["task_id"], cell["writer_policy"], cell["writer_draw"], form
                        if kind == "write":
                            writers.add(bank)
                        else:
                            self.assertIn(bank, writers)
                        self.assertNotIn((bank, name), jobs)
                        jobs.add((bank, name))
                        counts[kind] += 1
            self.assertEqual((counts["write"], counts["solve"], len(writers)), (380, 1140, 380))
            self.assertEqual(len({cell["customer"] for cell in cells}), spec["expected_customers"])

    def test_all_missing_assignments_reach_the_real_paired_report(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec, common, phases = self._simulate(root, with_sources=False)
            self.assertTrue(all(not members for _, _, members in phases[-1][2]))
            directory = root / "data/tau2-bench/data/tau2/domains/banking_knowledge/tasks"
            directory.mkdir(parents=True)
            for task in spec["tasks"]:
                (directory / (task + ".json")).write_text(json.dumps({"required_documents": []}))
            result = analysis.paired_validation_results(root, spec, spec, start=Decimal(0), task_ids=set(spec["tasks"]),
                         read_sha="", ceiling="0", protocol_sha=common["protocol_sha256"], binding=common)
        samples = result["trials"]["existing"] + result["trials"]["candidate"]
        self.assertEqual((len(result["writers"]), len(samples)), (380, 1140))
        self.assertTrue(all(t["success"] is None for t in samples))
        self.assertEqual(result["overall"]["pairs"]["missing"], 570)
        self.assertEqual(len({(t["task"], t["arm"], t["writer_draw"], t["reader_repeat"])
                              for t in result["trials"]["candidate"]}), 570)
        result["writing_outcomes"] = dict(rows=[dict(w, rating=None) for w in result["writers"]])
        customers = {row["task_id"]: row["customer"] for row in phases[0][1]}
        ratings = analysis.clustered_results(result, customers)
        overall = ratings["official_success"]["overall"]
        self.assertEqual((overall["observed_clusters"], overall["clusters"], overall["missing_bounds"]), (0, 13, [-1, 1]))
        summary = analysis.repetition_results(result, {}, PairedRepetitions(2, 3))
        self.assertEqual(summary["first_inputs"]["solve"], dict(assigned_groups=380, groups_with_input=0,
                                                                complete_identical_groups=0))
        self.assertEqual(summary["writer_contents"]["assigned_groups"], 190)
        with self.assertRaises(ContractError):
            analysis.repetition_results(result, {("solve", "t", "existing", "fact", 1): ["one", "changed"]},
                                        PairedRepetitions(2, 3))

    def test_equal_fixed_forms_are_not_weighted_by_number_of_observed_repeats(self):
        rows = [("a", "t", "fact", i, False, True) for i in range(6)]
        rows += [("a", "t", "lesson", 0, True, False)] + [("a", "t", "lesson", i, None, None) for i in range(1, 6)]
        s = clustered_contrast(rows, cluster_unit="customer")
        self.assertEqual(s["difference"], 0)
        self.assertEqual(s["observed_pairs"], 7)

    def test_full_design_one_customer_improves_with_five_different_tasks(self):
        from collections import Counter

        spec = cycle.read(cycle.ROOT / PROFILE)
        cases = cycle.read(cycle.ROOT / spec["preflight_file"])["cases"]
        customers = {c["task_id"]: c["customer"] for c in cases}
        frequent, count = Counter(customers.values()).most_common(1)[0]
        self.assertEqual(count, 5)
        result = dict(trials={"existing": [], "candidate": []}, writing_outcomes={"rows": []})
        for policy, rows in result["trials"].items():
            for task, customer in customers.items():
                changes = policy == "candidate" and customer == frequent
                for form in cycle.bank.matched.FORMS:
                    for draw in (1, 2):
                        result["writing_outcomes"]["rows"].append(dict(task=task, form=form, policy=policy,
                                  writer_draw=draw, rating=dict(unsupported_or_contradicted=False)))
                        for repeat in (1, 2, 3):
                            rows.append(dict(task=task, arm=form + "__preload", writer_draw=draw,
                                              reader_repeat=repeat, success=changes,
                                              account_opening=dict(effects=dict(all_target_accounts_opened=changes))))
        report = analysis.clustered_results(result, customers)
        primary = report["official_success"]["overall"]
        self.assertEqual((primary["assigned_pairs"], primary["observed_pairs"], primary["observed_clusters"]),
                         (570, 570, 13))
        self.assertAlmostEqual(primary["difference"], 1 / 13)
        self.assertEqual((primary["positive_clusters"], primary["two_sided_cluster_sign_p"]), (1, 1.0))
        self.assertEqual(report["all_requested_accounts_opened"]["by_form"]["fact"]["observed_pairs"], 114)
        self.assertEqual(report["items_with_unsupported_or_contradicted"]["overall"]["observed_pairs"], 190)
