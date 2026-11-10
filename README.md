<p align="center">
  <img src="docs/assets/logo.svg" alt="Outpost logo" width="128">
</p>

<h1 align="center">Outpost</h1>

<p align="center"><b>Self-hosted AI agents for air-gapped GitLab:</b> pipeline fixing, planning, code writing,
review, security remediation, issue triage, MR descriptions and release notes, multi-agent flows and chat,
running on your own GPUs and runners. Feedback on every agent's work and a metrics dashboard are built in.</p>

---

A self-hosted alternative to GitLab Duo Agent Platform for an air-gapped GitLab Ultimate instance. It uses
only standard GitLab APIs, an OpenAI-compatible vLLM endpoint (e.g. Qwen3.8-27B) and GitLab runners as the
sandbox.

## Agents

| Agent | Triggered by | Produces |
| --- | --- | --- |
| `pipeline-fixer` | any failed pipeline (automatic); `@agent fix-pipeline` | retry for flaky jobs, or a tested fix MR |
| `planner` | label `agent::plan`; `@agent plan` on an issue | implementation plan comment |
| `code-writer` | `@agent approve` / label `agent::implement` on an issue | draft MR implementing the plan |
| `code-reviewer` | `@agent review`; label `agent::review` (re-reviews each push) | inline MR comments + summary |
| `mr-assist` | `@agent fix …` / `@agent change …` in an MR thread | commit, or follow-up MR on human branches |
| `security-analyst` | `@agent security` on an MR | security triage report |
| `test-writer` | `@agent tests` on an MR | added tests, verified passing |
| `chat` | any other `@agent <question>` on an issue or MR; `@agent chat …` | a threaded answer, and a handoff to the right agent or flow |
| `security-remediator` | `@agent remediate` on an MR; after green default-branch pipelines when `security_remediator.auto` | root-cause fixes for SAST / dependency / secret findings: commit, follow-up MR or Draft MR |
| `mr-describer` | MR opened with an empty description (automatic); `@agent describe` | what / why / how to test / risks, in a marked block of the description |
| `release-notes` | `@agent release-notes v1.2.0..v1.3.0` on an issue or MR | grouped release notes from the commits and MRs in the range |
| `issue-triage` | issue opened (automatic, any author); `@agent triage` | suggested labels, likely duplicates, questions for the reporter |
| `conflict-resolver` | `@agent rebase` / `@agent resolve-conflicts` on an MR | GitLab rebase when there are no conflicts; otherwise conflicts resolved and tested in a replacement Draft MR |

`@agent help` lists the agents and flows enabled for a project. Each agent runs only in projects that list it
under `agents:` in `projects.yaml`. Agents are YAML + prompt templates in [`agents/`](agents); see
[docs/agents.md](docs/agents.md) to tune or add one.

### Agentic tools

While they work, agents call read-only **GitLab tools** for their own project: issues, MRs and their diffs and
threads, pipelines, jobs and job logs, CI lint, files at a ref, and code search. Sandbox jobs still hold no GitLab
token. They call the tools through the orchestrator (`POST /api/tasks/{id}/tools/{name}`) with their task ticket.
Each agent has its own tool allowlist and a budget of 60 calls per task. Edit agents spend a short investigation
with these tools before they start editing.

### Flows (several agents in sequence)

| Flow | Started by | Steps |
| --- | --- | --- |
| `issue-to-mr` | `@agent build` / label `agent::flow::issue-to-mr` on an issue | planner → **human approval** → code-writer → code-reviewer + security-analyst on the new MR |
| `fix-and-review` | `@agent fix-and-review` on an MR | pipeline-fixer → code-reviewer on the fix MR |
| `mr-deep-review` | `@agent deep-review` / label `agent::flow::deep-review` on an MR | code-reviewer + security-analyst → test-writer |
| `mr-secure` | `@agent secure` on an MR | security-analyst → security-remediator (fixes what the audit and the scanners found) |

The flow keeps one checklist comment up to date on the issue or MR. A failed step stops the flow. Approval
gates wait for `@agent approve <corrections>` or the `agent::approved` label. `@agent stop` cancels a flow.
Flows are YAML files in [`agents/flows/`](agents/flows).

### Chat

When `chat` is enabled for a project, any `@agent …` that isn't a command becomes a conversation in that
thread. Each reply in the thread continues it. Chat runs **inside the orchestrator** and needs no sandbox job,
so it answers in seconds. It has only the read-only tools and cannot change anything. When a specialist fits the
request, chat hands off: read-only agents such as a review start straight away. For agents and flows that
change code, chat posts the exact command and a human confirms by sending it.

### Security remediation

`security-remediator` fixes what GitLab Ultimate's security scanners (SAST, dependency scanning, secret
detection) report, at the root cause: it parameterises queries, moves secrets into configuration and upgrades
vulnerable dependencies. It runs the project's tests like any edit agent, and the result goes through the same
path checks.
* **Where findings come from.** The pipeline's findings are read through GitLab GraphQL, with queries only and
  mutations refused. On older instances it falls back to `gl-*-report.json` artifacts. On an MR, only findings in
  the files the MR changes count.
* **What counts.** Findings below `security_remediator.min_severity` (default `high`) are ignored, and so are those
  listed in `.gitlab/agent-security-ignore.yml`. Humans maintain that file; it is always protected, so an agent
  can never silence a finding instead of fixing it.
* **When it runs.** `@agent remediate` on an MR commits the fix, or opens a follow-up MR on a human branch. The
  `mr-secure` flow first runs `security-analyst` and passes its report on. With `security_remediator.auto: true`,
  green pipelines on the default branch get a Draft MR labelled `agent::security-fix`. Automatic runs pause
  while `max_open_mrs` such MRs are open, and no sandbox starts when there is nothing to fix.

### Issue triage, MR descriptions and release notes

These lightweight agents run **inside the orchestrator**, like chat. They answer in seconds and never touch code.
* **`issue-triage`** runs on every new issue, including issues from Reporters, who usually lack Developer
  access. It is capped at 20 runs per hour per project. It posts one comment with suggested labels (only labels
  the project has, never `agent::*` trigger labels), likely duplicates it checked exist, and questions about
  missing details. With `triage.apply_labels: true` it adds the labels as well.
* **`mr-describer`** writes *What / Why / How to test / Risks* from the diff and the linked issues. The text goes
  inside `<!-- outpost:description -->` markers: running it again replaces only that block, and the author's own
  text is never changed. On MR open it runs only when the description is empty (`mr_describer.on_open`);
  `@agent describe` always runs.
* **`release-notes`** groups the commits and merge requests between two refs into Features, Fixes, Breaking
  changes and Security, for example `@agent release-notes v1.2.0..v1.3.0`.

### Team instructions

Put your conventions, review focus and architecture notes in `.gitlab/agent-instructions.md` on the default
branch, and every agent reads them. MRs can't change the instructions before they are merged, and no agent can
edit the file. See [docs/agents.md](docs/agents.md#team-instructions-gitlabagent-instructionsmd).

### Custom agents

If the platform admin allows it for a project (`custom_agents` in `projects.yaml`), teams can add their own
agents as `.gitlab/agents/*.yaml` on the default branch. Each file is a prompt, triggers, collectors and tools.
They're read-only by default, namespaced `custom/<name>`, can't take built-in commands or labels, and are only
read from the default branch. `@agent help` lists them, along with any files that were rejected and why. See
[docs/agents.md](docs/agents.md#custom-agents-gitlabagents).

### Feedback and dashboard

Outpost records whether agent work helped:
* 👍 / 👎 on the agents' notes and MRs, polled every `FEEDBACK_POLL_SECONDS`. Withdrawn reactions are removed.
* agent MRs merged, or closed without merging
* pipelines going green on agent branches
* inline review comments the team dismissed (👎, or resolved without a change). The reviewer is shown them
  next time, so it learns the team's taste instead of repeating itself
  ([review memory](docs/agents.md#review-memory))

Use these numbers to compare prompt, model and engine changes on real work.

`/admin` is a server-rendered page with no JavaScript or CDN. It shows each agent's task counts, failures, MR
acceptance rate, reactions, median duration, tool use and LLM tokens, plus running flows, queue depth and
recent tasks.
`/metrics` serves the same numbers in Prometheus format. Both need the admin token: browsers use HTTP Basic
with any user name and the token as the password, and Prometheus uses `Authorization: Bearer`. Failed or 👎
tasks can be exported as [eval fixtures](docs/agents.md#turning-real-failures-into-fixtures), so real failures
become regression tests:

```bash
python eval/export_task.py --url https://outpost.internal --token "$ADMIN_TOKEN" --task <task id> --name my-case
# edit eval/fixtures/my-case/case.yaml: fill in `expect`, remove `draft: true`
```

### Operator API

| Endpoint | Auth | Purpose |
| --- | --- | --- |
| `POST /webhook` | webhook secret | GitLab events in |
| `GET /admin` | admin token (HTTP Basic) | dashboard |
| `GET /metrics` | admin token (Bearer) | Prometheus metrics |
| `GET /api/tasks` | admin token | recent tasks as JSON |
| `GET /api/review-memory?project_id=` / `DELETE /api/review-memory/{id}` | admin token | list or forget dismissed review comments |
| `GET /api/tasks/{id}/fixture` | admin token | a task as an eval fixture (`.tar.gz`) |
| `GET /healthz` | none | liveness and the loaded agents |

The sandbox uses `/api/tasks/{id}/bundle`, `/result` and `/tools/{name}` with its short-lived ticket.

## How it works

```
GitLab ──webhook──► orchestrator (FastAPI + worker + Postgres)          [holds the GitLab write token]
  ▲                   route event → agent, collect focused context, trigger sandbox pipeline
  │                          │ AGENT_TASK_ID + short-lived ticket (no GitLab token)
  │                          ▼
  │               sandbox job on runner tag `agent-sandbox` (unprivileged Docker, egress: GitLab + vLLM only)
  │                 clone target repo with CI_JOB_TOKEN (read) → Aider / tool loop vs vLLM
  │                 → edit, build, test, iterate (≤ N attempts) → POST result (files / comments)
  │                          │
  └──── API writes ◄─────────┘ orchestrator validates paths, lints CI, commits to agent/* branch, opens MR
```

* **The write token never enters the sandbox.** The job returns file contents. The orchestrator re-validates them
  (protected paths, CI files, size, path traversal) and commits them through the Commits API.
* **Agents never merge.** Agents write only to `agent/*` branches and open *Draft* MRs. Protected `main` plus
  approval rules keep humans in charge. On a human's MR branch, agents open a stacked follow-up MR instead of pushing.
* **Untrusted input.** Issue text, comments and logs are wrapped as untrusted data in prompts. Model output is
  sanitised so it cannot run GitLab quick actions (`/merge`, `/approve`) or ping `@all`. Only users with Developer
  access can trigger agents or approve flow gates. The one exception is triggers marked `allow_any_author`, which
  are limited to built-in read-only agents such as issue triage and are rate limited. Bot-authored events are
  ignored, so agents cannot trigger each other. Agents are chained only by flows, which the orchestrator runs explicitly (acyclic, at most 10 steps).
* **Loop limits.** Each agent has a cap on attempts and a wall-clock limit. The pipeline fixer stops after N fix attempts
  on its own branch. Every run ends with an MR or a comment saying what was tried.
* **Focused context.** Collectors extract the failing part of job traces, the diff since the last green pipeline,
  scanner findings with the code around them, and similar slices, within a per-agent token budget (~30K by default).
* **Measured, not assumed.** Reactions and outcomes (merged or closed MRs, green pipelines) are recorded for every
  task. Each task also stores its context and tool results, so any run can be replayed offline as an eval case.

## Repository layout

| Path | Contents |
| --- | --- |
| `orchestrator/` | FastAPI webhook receiver, router, task queue, context collectors, GitLab tools, flow engine, custom-agent catalog, inline agents, output handlers, feedback, dashboard and metrics, fixture export |
| `agents/` | agent definitions (`*.yaml`), prompt templates (`prompts/*.md.j2`) and flows (`flows/*.yaml`) |
| `sandbox/` | `agent-runner` (runs inside the CI job), engines (Aider, mini-SWE-agent, read-only tool loop), image Dockerfiles, sandbox project CI |
| `deploy/` | docker compose stack, runner config + egress firewall, config templates, GitLab bootstrap script |
| `build/` | internet-side image build, offline verification, air-gap-side load/push |
| `eval/` | offline evaluation harness with seeded-failure fixtures, and `export_task.py` to turn real tasks into fixtures; see [eval/README.md](eval/README.md) |
| `scripts/` | vLLM tool-calling smoke test, stub LLM |

## Getting started

1. **Internet build host:** `build/build-all.sh` builds the images and checks that they run with no network.
   It writes `dist/agent-platform-<ver>.tar`.
2. **Carry the tarball across the air gap.** Then run `REGISTRY=registry.internal ./load-and-push.sh` from the extracted bundle.
3. **Follow [docs/deployment.md](docs/deployment.md):** check vLLM, create the service account, run the bootstrap,
   set up the runner, start the compose stack, and run a pilot project.

## Development

```bash
cd orchestrator && pip install -e ../sandbox -e '.[dev]' && pytest   # router, tools, flows, inline agents, security, feedback, outputs vs fake GitLab
cd sandbox && pip install -e '.[dev]' && pytest          # runner loop, change collection, tool loop (needs git + bash)
```

## What it does not do (vs. GitLab DAP)

* There is no chat panel or IDE integration. Chat happens in issue and MR comment threads.
* There are no GitLab-tuned prompts. Prompt quality is ours to own, so run `eval/` after every prompt, model or engine
  change, and export the failures the dashboard shows into new fixtures.
* Issue-to-MR on non-trivial codebases is the hard part. Expect to spend most of the effort on evaluation, not plumbing.
