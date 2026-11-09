<!--
Optional per-repository file: .gitlab/agent-instructions.md on the default branch.
Every agent (built-in, custom, inline and sandbox) reads it as part of its prompt. Changes in a merge request
have no effect until they are merged. It is always protected: no agent can edit it.
Keep it short: anything past ~2000 tokens (~6000 characters) is cut off.
-->
# How we work in this repository

## Conventions
- Python 3.11, type hints on every public function, `ruff` formatting.
- Tests use pytest fixtures from `tests/conftest.py`; never `unittest.TestCase`.
- Database access goes through `app/db/queries.py`; never build SQL strings by hand.

## Reviews
- Focus on correctness, security and missing tests. Do not comment on naming or formatting; the linter does that.
- Anything under `app/billing/` needs a test for each changed branch of logic.

## Architecture notes
- `app/api/` is the HTTP layer only; business rules live in `app/services/`.
- Background jobs must be idempotent: they can run twice after a worker restart.
