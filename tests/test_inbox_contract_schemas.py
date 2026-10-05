"""Self-contained validation of Inbox-dialogue payloads against contract schemas.

The contract schemas, their example envelopes and a dependency-free
JSON-Schema subset validator are vendored under
``tests/fixtures/inbox_contract/``.  This test only reads those fixtures; it
never reads the sela repository.
"""

import importlib.util
import json
import unittest
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "inbox_contract"
SCHEMA_DIR = FIXTURE_DIR / "schemas"
EXAMPLES_DIR = FIXTURE_DIR / "examples"
VALIDATOR_PATH = FIXTURE_DIR / "validator.py"

# Schemas that legitimately have no standalone example envelope.  Verified
# empty: all 20 contract schemas are exercised by at least one example.
SCHEMAS_WITHOUT_EXAMPLE = frozenset()


def _load_validator():
    """Import the vendored validator from its file path.

    Loading by path keeps the fixture importable without making
    ``tests/fixtures`` a Python package.
    """
    spec = importlib.util.spec_from_file_location(
        "inbox_contract_validator", str(VALIDATOR_PATH)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class InboxContractSchemasTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.validator = _load_validator()
        cls.engine = cls.validator.Validator(str(SCHEMA_DIR))
        cls.schema_files = sorted(SCHEMA_DIR.glob("*.schema.json"))
        cls.example_files = sorted(EXAMPLES_DIR.glob("*.json"))

    def schema_stems(self):
        return {path.name[: -len(".schema.json")] for path in self.schema_files}

    def test_fixture_dirs_present(self):
        self.assertTrue(SCHEMA_DIR.is_dir(), "missing schema dir: %s" % SCHEMA_DIR)
        self.assertTrue(EXAMPLES_DIR.is_dir(), "missing examples dir: %s" % EXAMPLES_DIR)
        self.assertTrue(self.schema_files, "no schema fixtures in %s" % SCHEMA_DIR)
        self.assertTrue(self.example_files, "no example fixtures in %s" % EXAMPLES_DIR)
        # The vendored validator must resolve fixtures relative to itself, not
        # to any external repository.
        self.assertEqual(
            Path(self.validator.SCHEMA_DIR).resolve(),
            SCHEMA_DIR.resolve(),
            "validator SCHEMA_DIR does not point at the local fixtures",
        )
        self.assertEqual(
            Path(self.validator.EXAMPLES_DIR).resolve(),
            EXAMPLES_DIR.resolve(),
            "validator EXAMPLES_DIR does not point at the local fixtures",
        )

    def test_every_example_validates(self):
        known = self.schema_stems()
        for path in self.example_files:
            with self.subTest(example=path.name):
                envelope = _load_json(path)
                self.assertIsInstance(
                    envelope, dict, "%s is not a JSON object" % path.name
                )
                self.assertIn(
                    "schema", envelope, "%s is missing the 'schema' key" % path.name
                )
                self.assertIn(
                    "value", envelope, "%s is missing the 'value' key" % path.name
                )
                stem = envelope["schema"]
                self.assertIn(
                    stem,
                    known,
                    "%s references unknown schema %r" % (path.name, stem),
                )
                schema = _load_json(SCHEMA_DIR / (stem + ".schema.json"))
                errors = self.engine.validate(schema, envelope["value"], path="$")
                self.assertEqual(
                    errors,
                    [],
                    "%s (schema %r) failed validation:\n  %s"
                    % (path.name, stem, "\n  ".join(errors)),
                )

    def test_every_schema_is_exercised_or_allowlisted(self):
        known = self.schema_stems()
        referenced = set()
        for path in self.example_files:
            envelope = _load_json(path)
            if isinstance(envelope, dict) and isinstance(envelope.get("schema"), str):
                referenced.add(envelope["schema"])

        gaps = sorted(
            stem
            for stem in known
            if stem not in referenced and stem not in SCHEMAS_WITHOUT_EXAMPLE
        )
        self.assertEqual(
            gaps,
            [],
            "schemas with no example and not in SCHEMAS_WITHOUT_EXAMPLE: %s" % gaps,
        )

        stale = sorted(set(SCHEMAS_WITHOUT_EXAMPLE) - known)
        self.assertEqual(
            stale,
            [],
            "SCHEMAS_WITHOUT_EXAMPLE names schemas that do not exist: %s" % stale,
        )

    def test_validator_rejects_invalid_instance(self):
        # Negative case: prove the validator can actually fail.  The error
        # schema requires ``error`` to be an object; a string must be rejected.
        schema = _load_json(SCHEMA_DIR / "error.schema.json")
        invalid = {"error": "boom"}
        errors = self.engine.validate(schema, invalid, path="$")
        self.assertTrue(
            errors, "validator accepted a deliberately invalid error envelope"
        )
        self.assertIn(
            "$.error",
            "\n".join(errors),
            "validator failure did not name the offending JSON path",
        )


if __name__ == "__main__":
    unittest.main()
