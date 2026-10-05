#!/usr/bin/env python3
"""Validate the Inbox dialogue contract JSON Schemas and examples.

This module is a dependency free (Python standard library only) subset of
JSON Schema draft 2020-12.  It is vendored into the Trosa test suite so the
tests can validate dialogue payloads against the frozen contract schemas
without reaching outside this repository.

The module has no import side effects: it only reads files when a caller
calls :func:`check_schemas`, :func:`check_examples` or :func:`main`.
``SCHEMA_DIR`` and ``EXAMPLES_DIR`` default to the ``schemas/`` and
``examples/`` directories that sit next to this file and may be reassigned by
the caller.

The validation engine supports: type, enum, const, required, properties,
additionalProperties, items, min/max items, min/max length, pattern,
minimum/maximum, exclusiveMinimum/exclusiveMaximum, oneOf/anyOf/allOf/not,
$ref and $defs.  That subset is everything the contract schemas use.
"""

import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_DIR = os.path.join(HERE, "schemas")
EXAMPLES_DIR = os.path.join(HERE, "examples")

# Words that must never appear as a declared field in the schemas.
BANNED_FIELD = "kind|severity|missing_facts|response_schema|decision\\.options"

# Schema files: no bare banned token anywhere.
BANNED_SCHEMA_RE = re.compile(r"(?<![\w])(?:%s)(?![\w])" % BANNED_FIELD)

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$"
)

TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


class Fail(Exception):
    pass


def load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def json_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise Fail("unsupported value type: %r" % type(value))


def matches_type(value, expected):
    actual = json_type(value)
    if expected == "number":
        return actual in ("number", "integer")
    if expected == "integer":
        return actual == "integer"
    return actual == expected


def navigate(document, pointer):
    if not pointer:
        return document
    node = document
    for raw in pointer.lstrip("/").split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            node = node[int(token)]
        else:
            node = node[token]
    return node


class Validator:
    def __init__(self, schema_dir):
        self.schema_dir = schema_dir

    def resolve(self, ref, current_root):
        file_part, _, pointer = ref.partition("#")
        if not file_part:
            return current_root, pointer, current_root
        path = os.path.join(self.schema_dir, file_part)
        if not os.path.exists(path):
            raise Fail("unresolved $ref file: %s" % file_part)
        doc = load_json(path)
        return doc, pointer, doc

    def validate(self, schema, value, path="$", root=None):
        errors = []
        self._validate(schema, value, path, root if root is not None else schema, errors)
        return errors

    def _validate(self, schema, value, path, root, errors):
        if schema is True or schema == {}:
            return
        if schema is False:
            errors.append("%s: value forbidden by schema" % path)
            return
        if "$ref" in schema:
            doc, pointer, new_root = self.resolve(schema["$ref"], root)
            target = navigate(doc, pointer)
            self._validate(target, value, path, new_root, errors)
            return

        if "allOf" in schema:
            for sub in schema["allOf"]:
                self._validate(sub, value, path, root, errors)
        if "anyOf" in schema:
            if not any(not self._sub_errors(sub, value, path, root) for sub in schema["anyOf"]):
                errors.append("%s: no anyOf branch matched" % path)
        if "oneOf" in schema:
            matched = [sub for sub in schema["oneOf"] if not self._sub_errors(sub, value, path, root)]
            if len(matched) != 1:
                errors.append("%s: expected exactly one oneOf branch, matched %d" % (path, len(matched)))
        if "not" in schema:
            if not self._sub_errors(schema["not"], value, path, root):
                errors.append("%s: matched a forbidden schema" % path)

        if "const" in schema and value != schema["const"]:
            errors.append("%s: expected const %r" % (path, schema["const"]))
        if "enum" in schema and value not in schema["enum"]:
            errors.append("%s: %r not in enum %r" % (path, value, schema["enum"]))

        expected_types = schema.get("type")
        if expected_types is not None:
            if isinstance(expected_types, str):
                expected_types = [expected_types]
            if not any(matches_type(value, item) for item in expected_types):
                errors.append("%s: expected type %s, got %s" % (path, expected_types, json_type(value)))
                return

        if isinstance(value, str):
            if "minLength" in schema and len(value) < schema["minLength"]:
                errors.append("%s: shorter than %d" % (path, schema["minLength"]))
            if "maxLength" in schema and len(value) > schema["maxLength"]:
                errors.append("%s: longer than %d" % (path, schema["maxLength"]))
            if "pattern" in schema and not re.search(schema["pattern"], value):
                errors.append("%s: does not match pattern" % path)
            fmt = schema.get("format")
            if fmt == "uuid" and not UUID_RE.match(value):
                errors.append("%s: not a uuid" % path)
            if fmt == "date-time" and not DATETIME_RE.match(value):
                errors.append("%s: not a date-time" % path)

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in schema and value < schema["minimum"]:
                errors.append("%s: below minimum %s" % (path, schema["minimum"]))
            if "maximum" in schema and value > schema["maximum"]:
                errors.append("%s: above maximum %s" % (path, schema["maximum"]))
            if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
                errors.append("%s: not above exclusiveMinimum" % path)
            if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
                errors.append("%s: not below exclusiveMaximum" % path)

        if isinstance(value, list):
            if "minItems" in schema and len(value) < schema["minItems"]:
                errors.append("%s: fewer than %d items" % (path, schema["minItems"]))
            if "maxItems" in schema and len(value) > schema["maxItems"]:
                errors.append("%s: more than %d items" % (path, schema["maxItems"]))
            if "items" in schema:
                for index, item in enumerate(value):
                    self._validate(schema["items"], item, "%s[%d]" % (path, index), root, errors)

        if isinstance(value, dict):
            if "minProperties" in schema and len(value) < schema["minProperties"]:
                errors.append("%s: fewer than %d properties" % (path, schema["minProperties"]))
            if "maxProperties" in schema and len(value) > schema["maxProperties"]:
                errors.append("%s: more than %d properties" % (path, schema["maxProperties"]))
            for name in schema.get("required", []):
                if name not in value:
                    errors.append("%s: missing required property %r" % (path, name))
            properties = schema.get("properties", {})
            additional = schema.get("additionalProperties", True)
            for name, item in value.items():
                child = "%s.%s" % (path, name)
                if name in properties:
                    self._validate(properties[name], item, child, root, errors)
                elif additional is False:
                    errors.append("%s: unexpected property %r" % (path, name))
                elif isinstance(additional, dict):
                    self._validate(additional, item, child, root, errors)

    def _sub_errors(self, schema, value, path, root):
        sub = []
        self._validate(schema, value, path, root, sub)
        return sub


def check_schemas(schema_dir=None):
    schema_dir = schema_dir or SCHEMA_DIR
    errors = []
    schemas = sorted(glob.glob(os.path.join(schema_dir, "*.schema.json")))
    if not schemas:
        errors.append("no schema files found")
    for path in schemas:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        banned = BANNED_SCHEMA_RE.findall(text)
        if banned:
            errors.append("%s declares type driven fields: %s" % (os.path.basename(path), sorted(set(banned))))
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            errors.append("%s is not valid JSON: %s" % (os.path.basename(path), exc))
            continue
        if not isinstance(doc, dict):
            errors.append("%s is not a JSON object" % os.path.basename(path))
    return schemas, errors


def check_examples(validator, schema_paths, examples_dir=None):
    examples_dir = examples_dir or EXAMPLES_DIR
    errors = []
    known = {os.path.basename(path)[: -len(".schema.json")] for path in schema_paths}
    examples = sorted(glob.glob(os.path.join(examples_dir, "*.json")))
    if not examples:
        errors.append("no example files found")
    count = 0
    for path in examples:
        name = os.path.basename(path)
        try:
            envelope = load_json(path)
        except json.JSONDecodeError as exc:
            errors.append("%s is not valid JSON: %s" % (name, exc))
            continue
        if not isinstance(envelope, dict) or "schema" not in envelope or "value" not in envelope:
            errors.append("%s is not an envelope with 'schema' and 'value'" % name)
            continue
        stem = envelope["schema"]
        if stem not in known:
            errors.append("%s references unknown schema %r" % (name, stem))
            continue
        schema = load_json(os.path.join(validator.schema_dir, stem + ".schema.json"))
        problems = validator.validate(schema, envelope["value"], path="$")
        if problems:
            for problem in problems:
                errors.append("%s: %s" % (name, problem))
        else:
            count += 1
    return examples, count, errors


def main():
    problems = []

    schema_paths, schema_errors = check_schemas()
    problems.extend(schema_errors)

    validator = Validator(SCHEMA_DIR)
    examples, passed, example_errors = check_examples(validator, schema_paths)
    problems.extend(example_errors)

    print("schemas checked: %d" % len(schema_paths))
    print("examples checked: %d (%d valid)" % (len(examples), passed))

    if problems:
        print("")
        print("FAIL (%d problem(s)):" % len(problems))
        for problem in problems:
            print("  - %s" % problem)
        return 1

    print("")
    print("PASS: local Inbox dialogue schemas and examples are consistent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
