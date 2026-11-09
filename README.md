<p align="center">
  <img src="docs/assets/logo.svg" alt="Outpost logo" width="128">
</p>

<h1 align="center">Outpost</h1>

<p align="center"><b>Self-hosted AI agents for air-gapped GitLab:</b> pipeline fixing, planning, code writing,
review, multi-agent flows and chat, running on your own GPUs and runners.</p>

---

A self-hosted alternative to GitLab Duo Agent Platform for an air-gapped GitLab Ultimate instance. It uses
only standard GitLab APIs, an OpenAI-compatible vLLM endpoint (e.g. Qwen3.8-27B) and GitLab runners as the
sandbox. Requirements and rationale: [`Self-Built GitLab DAP on Air-Gapped GitLab.md`](Self-Built%20GitLab%20DAP%20on%20Air-Gapped%20GitLab.md).

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

`@agent help` lists the agents and flows enabled for a project. Agents are YAML + prompt templates in
[`agents/`](agents); see [docs/agents.md](docs/agents.md) to tune or add one.

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

`/admin` is a server-rendered page with no JavaScript or CDN. It shows each agent's task counts, failures, MR
acceptance rate, reactions, median duration and tool use, plus running flows, queue depth and recent tasks.
`/metrics` serves the same numbers in Prometheus format. Both need the admin token: browsers use HTTP Basic
with any user name and the token as the password, and Prometheus uses `Authorization: Bearer`. Failed or 👎
tasks can be exported as [eval fixtures](docs/agents.md#turning-real-failures-into-fixtures), so real failures
become regression tests.

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
  access can trigger agents or approve flow gates. Bot-authored events are ignored, so agents cannot trigger each
  other. Agents are chained only by flows, which the orchestrator runs explicitly (acyclic, at most 10 steps).
* **Loop limits.** Each agent has a cap on attempts and a wall-clock limit. The pipeline fixer stops after N fix attempts
  on its own branch. Every run ends with an MR or a comment saying what was tried.
* **Focused context.** Collectors extract the failing part of job traces, the diff since the last green pipeline,
  and similar slices, within a per-agent token budget (~30K by default).

## Repository layout

| Path | Contents |
| --- | --- |
| `orchestrator/` | FastAPI webhook receiver, router, task queue, context collectors, GitLab tools, flow engine, custom-agent catalog, inline agents, output handlers, feedback, dashboard and metrics, fixture export |
| `agents/` | agent definitions (`*.yaml`), prompt templates (`prompts/*.md.j2`) and flows (`flows/*.yaml`) |
| `sandbox/` | `agent-runner` (runs inside the CI job), engines (Aider, mini-SWE-agent, read-only tool loop), image Dockerfiles, sandbox project CI |
| `deploy/` | docker compose stack, runner config + egress firewall, config templates, GitLab bootstrap script |
| `build/` | internet-side image build, offline verification, air-gap-side load/push |
| `eval/` | offline evaluation harness with seeded-failure fixtures, and `export_task.py` to turn real tasks into fixtures |
| `scripts/` | vLLM tool-calling smoke test, stub LLM |

## Getting started

1. **Internet build host:** `build/build-all.sh` builds the images and checks that they run with no network.
   It writes `dist/agent-platform-<ver>.tar`.
2. **Carry the tarball across the air gap.** Then run `REGISTRY=registry.internal ./load-and-push.sh` from the extracted bundle.
3. **Follow [docs/deployment.md](docs/deployment.md):** check vLLM, create the service account, run the bootstrap,
   set up the runner, start the compose stack, and run a pilot project.

## Development

```bash
cd orchestrator && pip install -e ../sandbox -e '.[dev]' && pytest   # router, tools, flows, chat, outputs vs fake GitLab
cd sandbox && pip install -e '.[dev]' && pytest          # runner loop, change collection, tool loop (needs git + bash)
```

## What it does not do (vs. GitLab DAP)

* There is no chat panel or IDE integration. Chat happens in issue and MR comment threads.
* There are no GitLab-tuned prompts. Prompt quality is ours to own, so run `eval/` after every prompt, model or engine change.
* Issue-to-MR on non-trivial codebases is the hard part. Expect to spend most of the effort on evaluation, not plumbing.
