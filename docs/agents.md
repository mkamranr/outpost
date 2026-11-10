# Agents: tuning and adding

An agent is one YAML file in `agents/` plus one or more Jinja2 prompt templates in `agents/prompts/`.
The orchestrator loads them at start-up. It rejects unknown keys and duplicate commands, so a typo fails loudly.
To change prompts on site without rebuilding, mount `../agents:/app/agents:ro` in `deploy/docker-compose.yml`,
then restart.

## Built-in agents

| Agent | Mode / engine | Output | Context | Started by |
| --- | --- | --- | --- | --- |
| `pipeline-fixer` | edit / aider | `pipeline_fix` | `failed_jobs`, `ci_config`, `pipeline_diff` | failed pipeline; `fix-pipeline` |
| `planner` | readonly / llm | `note` | `issue`, `issue_notes` | `plan`; `agent::plan` |
| `code-writer` | edit / aider | `merge_request` | `issue`, `issue_notes` | `approve`, `implement`; `agent::implement` |
| `code-reviewer` | readonly / llm | `review` | `mr`, `mr_diff` | `review`; `agent::review` |
| `mr-assist` | edit / aider | `mr_commit` | `mr`, `discussion`, `mr_diff` | `fix`, `change`, `do` |
| `security-analyst` | readonly / llm | `note` | `mr`, `mr_diff` | `security` |
| `test-writer` | edit / aider | `mr_commit` | `mr`, `mr_diff` | `tests`, `test` |
| `security-remediator` | edit / aider | `security_fix` | `vulnerabilities` | `remediate`; green pipeline (opt-in) |
| `conflict-resolver` | edit / aider | `conflict_resolution` | `mr`, `mr_diff` | `rebase`, `resolve-conflicts` |
| `chat` | readonly / inline | `chat` | `subject`, `discussion` | `chat`, `ask`, any other text |
| `mr-describer` | readonly / inline | `mr_description` | `mr`, `mr_diff`, `linked_issues` | MR open; `describe` |
| `release-notes` | readonly / inline | `note` | `ref_range` | `release-notes <from>..<to>` |
| `issue-triage` | readonly / inline | `triage` | `issue`, `project_labels` | issue open (any author); `triage` |

A project runs only the agents listed in its `agents:` in `projects.yaml`. A repository's `.gitlab/agent.yml` can
narrow that list but never extend it.

## Team instructions (`.gitlab/agent-instructions.md`)

Teams teach every agent their house rules without touching prompts: conventions, what reviews should focus on,
architecture notes. The orchestrator reads the file from the **default branch** (an MR can't change its own
instructions) and appends it to the main prompt of every agent: built-in, custom, inline and sandbox. It goes in
a `<team-instructions>` block, after the agent's own prompt, with a note that it can't override the safety rules,
protected paths or untrusted-data rules. It is cut off after ~2000 tokens, cached per default-branch head and
rechecked at most every minute. Like `.gitlab/agent.yml`, it is always protected. See
[`deploy/repo-example/agent-instructions.md`](../deploy/repo-example/agent-instructions.md).

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
  # system: my-agent.system.md.j2 # inline agents only: replaces the default system prompt
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
read-only and built in, and should set `max_per_hour`. When a Reporter's event matches several agents, only the
`allow_any_author` ones run.

Loading also rejects:
* `merge_request` or `issue` triggers without `actions`
* `allow_any_author` on any other trigger type
* the `mr_description` and `triage` outputs on an engine other than `inline`

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
| `review_memory` | Up to 20 of this agent's earlier inline comments in the project that the team dismissed (👎, or the thread resolved without the file changing). Fixed budget of 1500 tokens |
| `linked_issues` | Issues an MR refers to (`#12` in its description, or a `12-…` source branch) |
| `project_labels` | The project's labels and descriptions, without `agent::*` trigger labels |
| `ref_range` | Commits and referenced MRs between two refs taken from the instruction (`v1.2.0..v1.3.0`) |
| `vulnerabilities` | Security scanner findings of the pipeline (or the MR's head pipeline, limited to the files it changes), filtered by `security_remediator.min_severity` and `.gitlab/agent-security-ignore.yml`, with code around each location |

Templates receive:
* `context.<collector>`
* `instruction` (the text after the command)
* `target` (the event: `kind`, `noteable_type`, `iid`, `pipeline_id`, `ref`, `sha`…)
* `repo` (`path`, `ref`, `sha`)
* `project` (merged config)
* `agent`
* `bot_username`
* `catalog` (chat only: the agents and flows it can hand off to)

Templates are rendered with strict undefined: a missing key is an error, so use `| default(…)` or `is defined`
for optional fields. Wrap anything users or logs wrote in
`<untrusted-data source="…">` and include `_untrusted.md.j2` at the top.

To add a collector, write a function decorated with `@collector("name")` that returns JSON-serialisable data
within `ctx.budget_tokens` (use `truncate()`). Import its module in `collectors/__init__.py`.
`@collector("name", budget=N)` gives a small collector a fixed budget, taken off the top, so it doesn't shrink the
others' even split. A collector gets
`ctx.target`, `ctx.project`, `ctx.sha` / `ctx.ref` and `ctx.instruction` (`ref_range`, for example, reads its range
from it). A collector that raises is recorded as `{"error": …}` and the agent still runs.

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

## Inline agents

Inline agents (`engine: inline`) run in the orchestrator worker, not in a sandbox job. They have no checkout:
they read the project only through the GitLab tools in their `tools:` list. They answer in seconds and take
capacity from `CHAT_WORKERS`, separate from `MAX_CONCURRENT_SANDBOXES`. Only built-in read-only agents can be
inline. The model finishes by calling `submit`, whose shape depends on the output:

| Output | `submit` arguments |
| --- | --- |
| `note`, `mr_description` | `{"body": "<markdown>"}` |
| `chat` | `{"body": "…", "handoff": {"target": "<agent or flow>", "instruction": "…"}}` (handoff optional) |
| `triage` | `{"summary": "…", "labels": [...], "duplicates": [{"iid": 12, "reason": "…"}], "missing_info": [...]}` |

The output handler, not the model, decides what reaches GitLab. Triage drops labels the project lacks and
`agent::*` labels, and keeps only duplicates that exist. The MR description handler strips the marker comments
from the model's text, so it can't write outside its block. Automatic `mr-describer` runs skip descriptions
the author already wrote before any model call.

## Conflict resolver

`@agent rebase` on a merge request brings it up to date with its target branch:

* **Behind but no conflicts.** On `agent/*` branches (or with `allow_push_to_mr_branch`) the orchestrator calls
  GitLab's rebase API and reports the result. No sandbox or model is used. On branches people own, it points to
  GitLab's **Rebase** button instead, because agents don't rewrite human branches.
* **Conflicts.** The sandbox also fetches the target branch and runs `git merge --no-commit`. The engine then
  resolves the files with conflict markers, and the project's tests run. An attempt fails while any
  `<<<<<<<` / `>>>>>>>` line is left. The result, diffed against the target head, is committed to
  `agent/conflict-resolver-mr-<iid>-…` cut from the target. That becomes a **replacement Draft MR**, linked from
  the original. Nothing on the original branch changes, and a human decides which MR to merge.
* **Limits.** The Commits API can't write merge commits, so the replacement squashes the MR's history into one
  commit. The run is refused up front when the MR touches more than 50 files compared with the target, or
  touches protected paths: the replacement couldn't carry those changes. CI files go through the same checks as
  every other agent's changes. Fork MRs, closed MRs and MRs GitLab is still checking are answered without
  running anything.

The eval case `eval/fixtures/merge-conflict-basic` shows the fixture layout for merges: `repo/` is the merge
base, and `source/` and `target/` are the two sides.

## Security remediator

`security-remediator` gets its findings from the `vulnerabilities` collector, not from a tool.
* **Where findings come from.** The collector reads the pipeline's findings through GitLab GraphQL
  (`securityReportFindings`, queries only). On older instances it falls back to `gl-*-report.json` job artifacts.
  For a pipeline event it uses that pipeline; for `@agent remediate` on an MR, the MR's head pipeline, keeping only
  findings in files the MR changes.
* **Filtering.** It keeps findings at or above `security_remediator.min_severity` (critical > high > medium >
  low), drops ignored ones, sorts by severity, keeps the top 10, and attaches the code around each location.
* **The ignore list.** It lives in `.gitlab/agent-security-ignore.yml` on the default branch:

```yaml
ignore:
  - CVE-2023-12345                       # an identifier (CVE, CWE, scanner rule)...
  - id: 3f2a9c1e-…                       # ...or a finding id
    reason: test fixture, not shipped
```

  A commented example is [`deploy/repo-example/agent-security-ignore.yml`](../deploy/repo-example/agent-security-ignore.yml).
  The file is always a protected path. Any change an agent proposes to it is rejected, so findings can only be
  silenced by a human through a normal MR.
* **When it skips.** No sandbox starts when nothing matches. A command run replies saying so, and an automatic
  run just records `skipped`. Automatic runs also skip while `max_open_mrs` MRs labelled `agent::security-fix` are
  open. Inside the `mr-secure` flow it always runs, because the `security-analyst` report from the previous step
  is part of its prompt.

The prompt forbids suppression comments, scanner exclusions and deleted tests. It also says to bump
dependencies only when the offline mirror has the fixed version. The fix summary lists each finding as fixed or
not fixed, with the reason.

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
required)`. It must stay read-only, and `test_every_tool_is_read_only` checks that. It allows GET requests only,
which is why security findings, read through GraphQL POST queries, come from a collector instead of a tool.

Every tool call and its (truncated) result is stored on the task, so the task can be exported as an eval fixture
with real `tool_responses`.

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

Shipped flows:

| Flow | Command / label | Steps |
| --- | --- | --- |
| `issue-to-mr` | `build`; `agent::flow::issue-to-mr` | planner → approval → code-writer → code-reviewer + security-analyst on the new MR |
| `fix-and-review` | `fix-and-review` | pipeline-fixer → code-reviewer on the fix MR |
| `mr-deep-review` | `deep-review`; `agent::flow::deep-review` | code-reviewer + security-analyst → test-writer |
| `mr-secure` | `secure` | security-analyst → security-remediator |

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
* can't use pipeline, MR-lifecycle or issue triggers, `allow_any_author`, built-in commands or labels, note
  markers, CI changes or the inline engine;
* must stay within the admin's tool and engine allowlists, with timeouts of 45 minutes or less;
* are skipped when invalid. `@agent help` shows why.

`.gitlab/agents/`, `.gitlab/agent.yml`, `.gitlab/agent-security-ignore.yml` and `.gitlab/agent-instructions.md`
are always protected paths, so no agent can change them. A repo can turn
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

## Example: add an inline "explain-failure" agent

A read-only agent that answers in seconds, with no sandbox:

```yaml
# agents/explain-failure.yaml
name: explain-failure
description: Explains in plain words why the latest pipeline of an MR failed.
mode: readonly
engine: inline
output: note
timeout_minutes: 4
context: [mr]
tools: [get_merge_request, get_pipeline_jobs, get_job_log]
max_per_hour: 30
prompts: {main: explain-failure.md.j2}
triggers:
  - event: note
    commands: [why]
    targets: [merge_request]
```

The template ends by asking for `submit` with `{"body": "…"}`. For an eval case, give it `tool_responses` for the
tools it calls; inline cases need no `repo/`.

## Evaluating changes

Full reference: [eval/README.md](../eval/README.md). `eval/run.py` renders the real prompts from `agents/` for each fixture. It runs the real sandbox runner
against vLLM and re-runs the tests itself to score the result. A fixture is a `repo/` directory plus a
`case.yaml` with the `agent`, `test` commands, the `context` a collector would have produced, and `expect`
(`classification`, `tests_pass`, `changes`, `body_contains`, `comment_on`, `handoff`, `tools_used`, and for
issue triage `labels` and `duplicate_of`).
`tool_responses` maps tool names to canned results (a string, or a list returned in order). `catalog` gives the
chat agent its handoff table. Inline agents such as `chat` need no `repo/`.
Add a fixture for every real failure an agent gets wrong. That is how prompt quality improves without regressions.

### Measuring on real work

The dashboard (`/admin`) and `/metrics` show, per agent:
* MR acceptance (merged ÷ merged + closed)
* 👍 / 👎 on its comments
* green pipelines on its branches
* failures, median time and tool calls
* LLM tokens (prompt / completion) and requests, in total and per task
  (`outpost_llm_tokens{agent,type}`, `outpost_llm_calls{agent}`)

Token usage comes from the `usage` field of every response from the OpenAI-compatible endpoint (the sandbox's own
calls, inline agents) and from Aider's per-call `Tokens: … sent, … received` lines. For `mini-swe` it's best
effort: if the installed release doesn't keep the raw responses, only the number of calls is counted. Tasks
from before this was recorded show `–`. Use these numbers to size GPUs and to spot prompts that grow out of hand.

After changing a prompt, model or engine, compare these for a week or two against the period before, next to
the offline eval pass rate. A rise in 👎 or closed MRs is the cue to export those tasks as fixtures.

### Review memory

Every inline comment a review agent posts is recorded with its file, line and the MR head it was made on. The
feedback poller then marks a comment **dismissed** when either:
* it has at least one 👎, and no more 👍 than 👎, or
* its thread was resolved while the commented file stayed the same between that head and the MR's current head.

The next review in the project gets those comments through the `review_memory` collector (it's on
`code-reviewer` by default), with the instruction not to raise the same kind of point again unless it's clearly
a real bug. Teams teach the reviewer just by reacting and resolving, as they already do.

The heuristic can be wrong: a thread resolved with "will fix later" also counts. `GET
/api/review-memory?project_id=<id>` lists what a project's reviewer is told to avoid. `DELETE
/api/review-memory/<id>` forgets one entry. Both need the admin token. `/admin` shows dismissed / posted
comments, and `/metrics` has `outpost_review_comments{agent,state="posted|dismissed"}`. A dismissal rate that
keeps rising is a sign the review prompt needs work.

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
