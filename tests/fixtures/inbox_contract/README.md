# Inbox dialogue contract fixtures

These files are **verbatim copies** of the frozen Inbox dialogue contract
schemas and their example envelopes. They are vendored into the Trosa test
suite so that Inbox-dialogue JSON payloads can be validated against the
contract without any test-time reference to the sela repository.

## Source

- `schemas/*.schema.json` — copied from `docs/proposals/inbox-dialogue-schemas/*.schema.json`
- `examples/*.json` — copied from `docs/proposals/inbox-dialogue-schemas/examples/*.json`
- `validator.py` — adapted from `tools/check_inbox_contract.py`

The upstream repository is **sela** (source checkout at `/Users/luoxin/Sela`).

## Copy date

2026-10-05

## Sync rule

- **Do not edit** the `schemas/`, `examples/` or `validator.py` files here as
  the source of truth.
- Make any contract change in the sela source first, then re-sync the copies
  into this directory.
- `validator.py` is the only file that is *adapted* rather than copied
  verbatim: all sela-specific paths and the contract-document check were
  removed, and `SCHEMA_DIR` / `EXAMPLES_DIR` now default to the local sibling
  `schemas/` and `examples/` directories. The JSON Schema validation engine
  itself is unchanged.
- Do not edit the sela repository from a Trosa checkout.
