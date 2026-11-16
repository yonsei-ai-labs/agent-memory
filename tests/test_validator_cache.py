"""Schema compilation reuse preserves validation and separates schema roots."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import ValidationError

from agent_memory_eval.contracts import validation


class ValidatorCacheTests(unittest.TestCase):
    def setUp(self):
        validation._compiled_validators.cache_clear()
        self.addCleanup(validation._compiled_validators.cache_clear)

    def test_repeated_lookups_compile_once_and_keep_invalid_records_rejected(self):
        with patch.object(validation.Draft202012Validator, "check_schema",
                          wraps=validation.Draft202012Validator.check_schema) as compile_schema:
            first = validation.validators()
            compiled = compile_schema.call_count
            self.assertGreater(compiled, 0)
            for _ in range(20):
                checks = validation.validators()
                with self.assertRaises(ValidationError):
                    checks["journal-state"].validate({})
            self.assertEqual(compile_schema.call_count, compiled)
            self.assertIs(first["journal-state"], checks["journal-state"])

    def test_mutating_a_callers_mapping_does_not_poison_the_cache(self):
        first = validation.validators()
        first.pop("journal-state")
        first["event"] = None
        second = validation.validators()
        self.assertIn("journal-state", second)
        self.assertIsNotNone(second["event"])

    def test_different_schema_roots_do_not_share_validation_rules(self):
        with tempfile.TemporaryDirectory() as temporary:
            roots = []
            for i, kind in enumerate(("integer", "string")):
                root = Path(temporary) / str(i)
                schemas = root / "schemas/v1.1"
                schemas.mkdir(parents=True)
                (schemas / "value.schema.json").write_text(json.dumps({
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "https://example.invalid/value", "type": kind,
                }))
                roots.append(root)
            results = []
            for root in roots:
                with patch.object(validation, "ROOT", root), patch.object(validation, "SCHEMAS", root / "schemas/v1.1"):
                    results.append(validation.validators()["value"])
            self.assertTrue(results[0].is_valid(1))
            self.assertFalse(results[0].is_valid("text"))
            self.assertTrue(results[1].is_valid("text"))
            self.assertFalse(results[1].is_valid(1))


if __name__ == "__main__":
    unittest.main()
