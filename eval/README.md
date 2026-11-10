# Offline evaluation

`eval/run.py` runs agents against fixed cases and scores the results. It renders the real prompts from `agents/`
and runs the real sandbox runner, or the inline tool loop for inline agents, against your vLLM. It needs no
GitLab: tool calls are answered from each case's recorded responses.

Re-run it after every prompt, model, engine or vLLM change, and compare the pass rate with the previous report.
For the effect on real work, compare the dashboard numbers too (see
[docs/agents.md](../docs/agents.md#measuring-on-real-work)).

## Running

Run inside a sandbox image. It has agent-runner, Jinja2, PyYAML and pydantic in `/opt/agent`, plus the toolchain
the cases' tests need:

```bash
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/w" -w /w \
  -e AGENT_LLM_BASE_URL=http://vllm.internal:8000/v1 -e AGENT_LLM_MODEL=qwen3.8-27b \
  registry.internal/agent-platform/sandbox-python:<ver> agent-python eval/run.py
```

| Option | Effect |
| --- | --- |
| `--case <name>` | Run only this case (repeatable) |
| `--engine aider\|mini-swe` | Override the edit engine for edit agents |
| `--include-drafts` | Also run cases marked `draft: true` |
| `--out <dir>` | Output directory (default `eval/out/`) |

For each case, `eval/out/<case>/` gets `bundle.json` (exactly what the agent received) and `result.json`.
`eval/out/report.md` and `report.json` hold the summary, including skipped drafts. The exit code is non-zero when
any case fails.

## Cases

| Case | Agent | What it checks |
| --- | --- | --- |
| `py-off-by-one` | pipeline-fixer | Classifies a broken `add()` as a code failure; the fix makes the tests pass |
| `py-missing-import` | pipeline-fixer | Restores an import a refactor removed |
| `ci-yaml-broken` | pipeline-fixer | Classifies invalid `.gitlab-ci.yml` as `ci_config` and fixes it |
| `flaky-network` | pipeline-fixer | Classifies an unreachable mirror as flaky and changes nothing |
| `review-uses-job-log` | code-reviewer | Reads the failing job log and comments on `stats.py` |
| `chat-explains-failure` | chat | Answers from the job log, not from guesses |
| `chat-routes-review` | chat | Hands a review request to `code-reviewer` instead of reviewing itself |
| `mr-describe-basic` | mr-describer | Description covers what changed, why (from the linked issue) and how to test |
| `triage-duplicate` | issue-triage | Finds the duplicate issue and suggests the `bug` and `storage` labels |
| `vuln-sql-injection` | security-remediator | Parameterises an SQL query; an injection test fails until it does |
| `merge-conflict-basic` | conflict-resolver | Keeps both sides of a conflict (the MR's quantity, main's renamed field); no markers left, tests pass |

## Case format

A case is `eval/fixtures/<name>/case.yaml`, plus a `repo/` directory for agents that edit or read code. The
harness copies `repo/` into a fresh git repository for every run. Conflict-resolver cases also have
`source/` and `target/`: files laid over `repo/` (the merge base) and committed as two branches, with the
source checked out and the target passed as the merge target. Inline agents (`chat`, `mr-describer`,
`release-notes`, `issue-triage`) need no `repo/`.

```yaml
agent: pipeline-fixer               # required
description: What the case is about and what a good result is.
instruction: ""                     # the text after the @agent command
target: {kind: pipeline, pipeline_id: 1, ref: main}   # the event, as templates see it in `target`
test: ["python -m pytest -q"]       # must pass for tests_pass (re-run by the harness, not trusted from the agent)
setup: []
protected_paths: [".gitlab/agent.yml"]
engine: aider                       # optional engine override
context:                            # what the agent's collectors would have produced, keyed by collector
  failed_jobs: [...]
catalog: [...]                      # chat only: the agents and flows it may hand off to
tool_responses:                     # canned GitLab tool results: a string, or a list returned in order
  get_job_log: "FAILED test_calc.py::test_add ..."
expect:                             # every listed check must pass; a case with no checks fails
  classification: code
  tests_pass: true
draft: true                         # optional: skipped unless --include-drafts
```

| `expect` key | Passes when |
| --- | --- |
| `classification` | The pipeline fixer's classification equals it (`code`, `ci_config`, `flaky`) |
| `changes` | The agent changed files (`true`) or did not (`false`) |
| `tests_pass` | The `test` commands pass on the final tree, and there are changes |
| `body_contains` | Every string occurs in the comment body (case-insensitive) |
| `comment_on` | A review comment is on this file |
| `handoff` | Chat handed off to this agent or flow |
| `tools_used` | Every listed tool was called |
| `no_conflict_markers` | The run reported conflicted files and none of them still has `<<<<<<<` / `>>>>>>>` lines |
| `labels` | Triage suggested all of these labels |
| `duplicate_of` | Triage named this issue as a duplicate |

`tests/test_fixtures_export.py` renders every case through the harness in the orchestrator test suite, so a
broken case or a prompt variable a case lacks fails CI, not an eval run.

## Turning real failures into cases

Every task stores the context it collected and its tool calls, so a task that went wrong can be replayed here.
Export it from the dashboard (`/admin` → **Export fixture**) or with:

```bash
python eval/export_task.py --url https://outpost.internal --token "$ADMIN_TOKEN" --task <task id> --name my-case
```

The export holds the task's `context`, `instruction`, `target`, `tool_responses`, the project's `test` / `setup`
commands, and a snapshot of the repository at the task's commit. If the repository is larger than
`FIXTURE_MAX_REPO_MB`, only the files the task read or changed are included. A `source:` block links back to the
task and its feedback.

The case arrives as `draft: true` with a guessed `expect`. Then:
1. Replace the `description` with what a good result is.
2. Set `expect`. For edit agents, prefer `tests_pass` with a test that fails until the problem is really fixed.
3. Remove `draft: true`.
4. Run `eval/run.py --case my-case`.

It should fail with the current prompt, and pass once the prompt, model or engine is fixed.

## Writing a case by hand

Keep the repository small and the failure specific. Seed exactly one problem, and make the expectation
something only a correct fix satisfies. For example, `vuln-sql-injection` includes a test that an injection
string matches no rows. To get realistic `context`, export a similar real task and trim it, or copy
`bundle.json` from a previous run.
