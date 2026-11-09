# Agents: tuning and adding

An agent is one YAML file in `agents/` plus one or more Jinja2 prompt templates in `agents/prompts/`.
The orchestrator loads them at start-up. It rejects unknown keys and duplicate commands, so a typo fails loudly.
To change prompts on site without rebuilding, mount `../agents:/app/agents:ro` in `deploy/docker-compose.yml`,
then restart.

## Definition reference

```yaml
name: code-reviewer               # unique; used in projects.yaml `agents:` lists
description: One line shown by `@agent help`.
mode: readonly                    # edit = changes files and runs tests; readonly = analyses only
engine: llm                       # edit: aider | mini-swe     readonly: llm (tool loop with list/read/grep)
                                  #   or inline (runs in the orchestrator, GitLab tools only; built-in agents only)
output: review                    # pipeline_fix | merge_request | mr_commit | note | review | chat
                                  #   | mr_description | triage (inline only) | security_fix
context: [mr, mr_diff]            # collectors, see below
tools: [get_merge_request, get_job_log]  # GitLab tools the agent may call while it runs, see below
context_budget_tokens: 32000      # split across collectors; a 27B model does best on 20-40K focused tokens
max_attempts: 3                   # edit agents: engine→test iterations inside one sandbox job
timeout_minutes: 20               # wall clock for the sandbox job
classify: false                   # pipeline fixer only: run prompts.classify first (code / ci_config / flaky)
allow_ci_changes: false           # may this agent edit .gitlab-ci.yml / .gitlab/ci/?
max_per_hour: 20                  # optional cap on tasks per project per hour (set it on automatic triggers)
note_marker: "<!-- agent:plan -->"  # note outputs: hidden marker other agents can find (planner → code-writer)
note_suffix: "Reply `{mention} approve`…"
prompts:
  main: code-reviewer.md.j2       # required; `classify` also when classify: true
triggers:
  - event: note                   # "@agent <command> <free text>"
    commands: [review]
    targets: [merge_request]      # issue | merge_request | commit
  - event: label                  # fires when the label is added
    label: agent::review
    targets: [merge_request]
  - event: merge_request          # MR lifecycle; "update" only fires when new commits were pushed
    actions: [open, reopen, update]
    require_label: agent::review
  - event: issue                  # issue lifecycle: open, update, close, reopen
    actions: [open]
    allow_any_author: true        # also for authors without Developer access (built-in read-only agents only)
  - event: pipeline
    status: failed
```

Every trigger needs Developer access from the person who fired it, except `merge_request` / `issue` triggers
with `allow_any_author: true`. Those exist for issue triage, where Reporters open most issues. Such agents must be
read-only and built in, and should set `max_per_hour`.

## Collectors (`orchestrator/app/collectors/`)

| Name | Gives the prompt |
| --- | --- |
| `failed_jobs` | Failed, non-allow-failure jobs, with traces trimmed to the error regions and the tail |
| `ci_config` | `.gitlab-ci.yml`, plus the merged YAML with includes resolved |
| `pipeline_diff` | Commits and diff since the last green pipeline on the ref |
| `issue` / `issue_notes` | Issue body; discussion; the latest plan **posted by the bot** (forged plans are ignored) |
| `mr` / `mr_diff` | MR metadata and diff |
| `discussion` | The thread a command came from, including the file and line it is attached to |
| `subject` | Summary of the issue, MR (with head pipeline) or commit a comment is on, for chat |
| `linked_issues` | Issues an MR refers to (`#12` in its description, or a `12-…` source branch) |
| `project_labels` | The project's labels and descriptions, without `agent::*` trigger labels |
| `ref_range` | Commits and referenced MRs between two refs taken from the instruction (`v1.2.0..v1.3.0`) |
| `vulnerabilities` | Security scanner findings of the pipeline (or the MR's head pipeline, limited to the files it changes), filtered by `security_remediator.min_severity` and `.gitlab/agent-security-ignore.yml`, with code around each location |

Templates receive `context.<collector>`, `instruction` (the text after the command), `repo` (`path`, `ref`,
`sha`), `project` (merged config), `agent`, `bot_username`, and `catalog` (chat only: the agents and flows it can
hand off to). Wrap anything users or logs wrote in
`<untrusted-data source="…">` and include `_untrusted.md.j2` at the top.

To add a collector, write a function decorated with `@collector("name")` that returns JSON-serialisable data
within `ctx.budget_tokens` (use `truncate()`). Import its module in `collectors/__init__.py`.

## Outputs (`orchestrator/app/outputs/`)

| Output | Effect |
| --- | --- |
| `pipeline_fix` | flaky → retry jobs + comment; fix → lint CI → new `agent/…` branch + Draft MR into the failing ref (or a commit on an existing agent branch) |
| `merge_request` | New `agent/…` branch from the default branch + Draft MR "Closes #iid" |
| `mr_commit` | Agent-owned MR branch → commit; human-owned → stacked Draft MR targeting it (unless `allow_push_to_mr_branch`) |
| `note` | Comment on the issue, MR or commit (in the triggering thread when there is one) |
| `review` | Line-anchored MR discussions; comments that cannot be anchored go into the summary |
| `chat` | Reply in the thread; an optional handoff starts a read-only agent, or posts the command for an agent or flow that changes code |
| `mr_description` | Writes the MR description inside `<!-- outpost:description -->` markers: replaces an earlier block, otherwise appends, so the author's text stays |
| `triage` | One comment with suggested labels (existing, non-`agent::` labels only; applied when `triage.apply_labels`), verified duplicates and questions for the reporter |
| `security_fix` | On an MR: like `mr_commit`. After a pipeline: new `agent/…-vuln-<id>` branch + Draft MR labelled `agent::security-fix`, listing the findings in scope |

All changes pass `outputs/changes.py` before anything is committed. That check rejects:
* path traversal and `.git/`
* protected paths
* CI files, unless `allow_ci_changes` is set
* more than 50 files, or any file over 1 MB

## Tools (`orchestrator/app/tools/`)

Agents with a `tools:` list get these as function-calling tools next to `list_files` / `read_file` / `grep`.
The orchestrator runs every call against the task's own project. Arguments can't name another project. Results
come back wrapped as untrusted data and truncated to about 10K characters. Each task can make 60 calls.

| Tool | Returns |
| --- | --- |
| `get_issue`, `list_issue_notes`, `search_issues` | Issue details, comments, text search |
| `get_merge_request`, `get_mr_diff`, `list_mr_discussions`, `search_merge_requests` | MR details and head pipeline, diff (optionally one file), threads with their file and line |
| `list_pipelines`, `get_pipeline_jobs`, `get_job_log`, `lint_ci` | Pipelines by ref/status, jobs, a job log focused on errors, CI lint result |
| `get_file`, `list_tree`, `search_code` | A file at a ref, a directory listing, code search on the default branch |

Read-only agents use the tools throughout their loop. Edit agents (Aider, mini-SWE-agent) can't call tools, so
the runner first runs a short investigation loop with them. Its findings and suspect files are passed to the
engine. To add a tool, decorate a function in `tools/gitlab.py` with `@tool(name, description, parameters,
required)`. It must stay read-only, and `test_every_tool_is_read_only` checks that.

## Flows (`agents/flows/*.yaml`)

```yaml
name: issue-to-mr
description: One line shown by `@agent help`.
triggers:                         # note or label only
  - event: note
    commands: [build]
    targets: [issue]
steps:
  - agent: planner
  - gate: approval                # waits for "@agent approve <corrections>" or label agent::approved
  - agent: code-writer            # the approval's corrections become its instruction
  - agent: [code-reviewer, security-analyst]   # a list runs in parallel
    target: produced_mr           # subject (default) | produced_mr: the MR the previous agent step opened
    when: success                 # success (default): every task of the previous step succeeded | always
```

A flow is offered in a project only when every agent it uses is enabled there. Each step creates ordinary tasks,
so the same sandbox, policies and outputs apply. Later steps also see a short summary of what earlier steps
produced. Each subject can have one running flow. `@agent stop` stops it, and the checklist comment on the
subject shows progress. Registry loading rejects:
* a flow that starts or ends with a gate
* more than 10 steps
* unknown agents
* a command another agent or flow already uses
* `produced_mr` after a step that cannot open an MR

## Custom agents (`.gitlab/agents/*.yaml`)

The platform admin enables them per project in `projects.yaml`:

```yaml
custom_agents:
  enabled: true          # default false
  allow_edit: false      # true lets them change code (still via Draft MRs / stacked MRs only)
  engines: [llm]         # add aider / mini-swe together with allow_edit
  tools: [get_issue, get_merge_request, get_mr_diff, get_job_log]   # defaults to all non-file tools
  max_agents: 10
```

Developers then commit agent files to the **default branch** (changes in MRs have no effect until merged).
See [`deploy/repo-example/agents/changelog.yaml`](../deploy/repo-example/agents/changelog.yaml):

```yaml
# .gitlab/agents/changelog.yaml  -> available as `custom/changelog`
name: changelog
description: Drafts a changelog entry for a merge request.
mode: readonly           # default
output: note             # note | review | merge_request | mr_commit
context: [mr, mr_diff]   # issue | issue_notes | mr | mr_diff | discussion
tools: [get_merge_request]
prompt: |
  Write a Keep-a-Changelog entry for MR !{{ context.mr.iid }} "{{ context.mr.title }}".
  <untrusted-data source="diff">{{ context.mr_diff.diff }}</untrusted-data>
  Call `submit` with {"body": "<the entry>"}.
triggers:
  - event: note
    commands: [changelog]
    targets: [merge_request]
```

The untrusted-data preamble is always put in front of the prompt. Custom agents:
* can't use pipeline or MR-lifecycle triggers, built-in commands or labels, note markers, CI changes or the inline engine;
* must stay within the admin's tool and engine allowlists, with timeouts of 45 minutes or less;
* are skipped when invalid. `@agent help` shows why.

`.gitlab/agents/` and `.gitlab/agent.yml` are always protected paths, so no agent can change them. A repo can turn
custom agents off with `custom_agents: false` in `.gitlab/agent.yml`, but it can't turn them on.

## Example: add a "docs-writer" agent

```yaml
# agents/docs-writer.yaml
name: docs-writer
description: Updates README/docs to match the code changed in an MR.
mode: edit
engine: aider
output: mr_commit
context: [mr, mr_diff]
prompts: {main: docs-writer.md.j2}
triggers:
  - event: note
    commands: [docs]
    targets: [merge_request]
```

Write `agents/prompts/docs-writer.md.j2`, add `docs-writer` to the project's `agents:` in `projects.yaml`,
restart, and comment `@agent docs` on an MR. Add an `eval/fixtures/<case>/` covering it before relying on it.

## Evaluating changes

`eval/run.py` renders the real prompts from `agents/` for each fixture. It runs the real sandbox runner
against vLLM and re-runs the tests itself to score the result. A fixture is a `repo/` directory plus a
`case.yaml` with the `agent`, `test` commands, the `context` a collector would have produced, and `expect`
(`classification`, `tests_pass`, `changes`, `body_contains`, `comment_on`, `handoff`, `tools_used`, and for
issue triage `labels` and `duplicate_of`).
`tool_responses` maps tool names to canned results (a string, or a list returned in order). `catalog` gives the
chat agent its handoff table. Inline agents such as `chat` need no `repo/`.
Add a fixture for every real failure an agent gets wrong. That is how prompt quality improves without regressions.

### Turning real failures into fixtures

Every task stores the context it collected and the results of its tool calls, so a task that went wrong can be
replayed offline. On the dashboard (`/admin`), failed tasks and tasks with a 👎 or a closed MR have an
**Export fixture** link. From a shell:

```bash
python eval/export_task.py --url https://outpost.internal --token "$ADMIN_TOKEN" --task <task id> --name my-case
```

This writes `eval/fixtures/my-case/` with the task's `context`, `instruction`, `target`, `tool_responses` and a
snapshot of the repository at the task's commit. If the repository is larger than `FIXTURE_MAX_REPO_MB`, only
the files the task read or changed are included. The case starts as `draft: true` with a guessed `expect`, and
`eval/run.py` skips drafts (or runs them with `--include-drafts`). Write down what a good result is, complete
`expect`, and remove `draft: true`.
