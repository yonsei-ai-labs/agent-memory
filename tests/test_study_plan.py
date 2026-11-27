import json
from pathlib import Path
import tempfile
import unittest

from agent_memory_eval.contracts.core import ContractError, digest


def write_ref(root, name, text):
    path = Path(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return dict(path=name, sha256=digest(path.read_bytes()))


def example(root):
    setup = write_ref(root, "protocols/setup.json", "{}\n")
    writer = write_ref(root, "protocols/instructions/writer.txt", "Write one memory from the public experience.\n")
    return dict(
        format="agent-memory-study-plan/v1",
        id="bank-round-01",
        purpose="development",
        question="Does stating the prior outcome first change reuse of failed decisions?",
        benchmark=dict(adapter="tau-banking", setup=setup, tasks=["task_004", "task_023"]),
        first_pass=dict(draws=3, reuse=[dict(recording="tau-banking-source-screen-01")]),
        writing=dict(instruction=writer, feedback="result_only", representations={
            "fact": dict(text="Record observed facts."),
            "outcome-first": dict(text="Begin with the prior task outcome, then the facts."),
        }),
        reading={
            "preload": dict(read_mode="preload", notice=dict(text="Notes from earlier work follow.")),
            "on_demand": dict(read_mode="on_demand", notice=dict(text="Earlier notes can be listed."),
                              tool_descriptions=dict(memory_list="Browse note titles.", memory_get="Read one note.")),
        },
        conditions=[dict(representation="fact", reading="preload"),
                    dict(representation="outcome-first", reading="on_demand")],
        repeats=1,
        budget=dict(max_usd="7.00"),
        retry=dict(http_codes=[502, 503], wait_seconds=[120, 600]),
    )


class StudyPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.plan = example(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, plan):
        from agent_memory_eval.study.plan import load_plan
        path = self.root / "protocols/studies/plan.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(plan), encoding="utf-8")
        return load_plan(path, self.root)

    def test_valid_plan_lists_declared_conditions_and_arms(self):
        from agent_memory_eval.study.plan import arm_name, conditions, plan_digest
        plan = self.load(self.plan)
        self.assertEqual(conditions(plan), [("fact", "preload"), ("outcome-first", "on_demand")])
        self.assertEqual(arm_name("outcome-first", "on_demand"), "outcome-first__on_demand")
        self.assertEqual(len(plan_digest(plan)), 64)

    def test_content_text_reads_inline_and_pinned_files(self):
        from agent_memory_eval.study.plan import content_text
        plan = self.load(self.plan)
        self.assertEqual(content_text(self.root, plan["writing"]["representations"]["fact"]), "Record observed facts.")
        self.assertEqual(content_text(self.root, plan["writing"]["instruction"]),
                         "Write one memory from the public experience.")

    def test_unknown_field_is_rejected(self):
        with self.assertRaisesRegex(ContractError, "Invalid study plan"):
            self.load(dict(self.plan, extra=True))

    def test_changed_pinned_file_is_rejected(self):
        (self.root / "protocols/instructions/writer.txt").write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "Pinned asset changed"):
            self.load(self.plan)

    def test_duplicate_condition_is_rejected(self):
        plan = dict(self.plan, conditions=self.plan["conditions"] + [self.plan["conditions"][0]])
        with self.assertRaisesRegex(ContractError, "Duplicate condition"):
            self.load(plan)

    def test_reserved_and_double_underscore_names_are_rejected(self):
        for name in ("no_memory", "empty_preload", "fact__v2"):
            reps = dict(self.plan["writing"]["representations"], **{name: dict(text="x")})
            plan = dict(self.plan, writing=dict(self.plan["writing"], representations=reps))
            with self.subTest(name=name), self.assertRaisesRegex(ContractError, "Invalid study plan"):
                self.load(plan)

    def test_condition_must_use_declared_policies(self):
        plan = dict(self.plan, conditions=[dict(representation="lesson", reading="preload")])
        with self.assertRaisesRegex(ContractError, "Undeclared representation or reading"):
            self.load(plan)

    def test_one_reader_per_read_mode(self):
        reading = dict(self.plan["reading"], preload_v2=dict(read_mode="preload", notice=dict(text="Other notice.")))
        with self.assertRaisesRegex(ContractError, "one reader per read mode"):
            self.load(dict(self.plan, reading=reading))

    def test_tool_descriptions_only_for_on_demand(self):
        reading = dict(self.plan["reading"])
        reading["preload"] = dict(reading["preload"], tool_descriptions=dict(memory_list="a", memory_get="b"))
        with self.assertRaisesRegex(ContractError, "Tool descriptions apply to on_demand readers"):
            self.load(dict(self.plan, reading=reading))

    def test_reader_needs_a_notice_or_tool_descriptions(self):
        reading = dict(self.plan["reading"], preload=dict(read_mode="preload"))
        with self.assertRaisesRegex(ContractError, "needs a notice, tool descriptions, or both"):
            self.load(dict(self.plan, reading=reading))

    def test_first_pass_only_plan_needs_no_policies(self):
        from agent_memory_eval.study.plan import conditions
        plan = {k: v for k, v in self.plan.items() if k not in ("writing", "reading")}
        plan.update(conditions=[], first_pass=dict(draws=3))
        self.assertEqual(conditions(self.load(plan)), [])

    def test_policies_are_required_once_a_condition_is_declared(self):
        plan = {k: v for k, v in self.plan.items() if k != "reading"}
        with self.assertRaisesRegex(ContractError, "Conditions need declared writing and reading"):
            self.load(plan)

    def test_decision_record_pins_its_evidence(self):
        evidence = write_ref(self.root, "runs/step-2/analysis/slots.json", "[]\n")
        decision = dict(hypothesis="Memories omit the prior outcome.", prediction="Fewer repeated failed decisions.",
                        criterion="Report recoveries and regressions against the own first pass.", evidence=[evidence])
        self.load(dict(self.plan, decision=decision))
        (self.root / "runs/step-2/analysis/slots.json").write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "Pinned asset changed"):
            self.load(dict(self.plan, decision=decision))

    def test_confirmation_tasks_are_held_out_members_of_the_plan(self):
        benchmark = dict(self.plan["benchmark"], confirmation_tasks=["task_023"])
        self.load(dict(self.plan, benchmark=benchmark))
        benchmark = dict(self.plan["benchmark"], confirmation_tasks=["task_999"])
        with self.assertRaisesRegex(ContractError, "Confirmation tasks must be planned tasks"):
            self.load(dict(self.plan, benchmark=benchmark))
        benchmark = dict(self.plan["benchmark"], confirmation_tasks=list(self.plan["benchmark"]["tasks"]))
        with self.assertRaisesRegex(ContractError, "leave development tasks"):
            self.load(dict(self.plan, benchmark=benchmark))

    def test_budget_must_be_a_decimal_string(self):
        with self.assertRaisesRegex(ContractError, "Invalid study plan"):
            self.load(dict(self.plan, budget=dict(max_usd="7 dollars")))


if __name__ == "__main__":
    unittest.main()
