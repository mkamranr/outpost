# Self-Built GitLab DAP on Air-Gapped GitLab

Oct 23, 2026 · @mohammed rashed

## Verdict

Yes, a self-built Duo Agent Platform (DAP) alternative is doable. It needs only standard GitLab APIs, the OpenAI-compatible endpoint vLLM already exposes, and GitLab runners as the sandbox.

The case for building rather than buying is strong: GitLab's own self-hosted option needs an extra add-on, and it does not list Qwen3.8-27B as a supported model.

## The off-the-shelf option

GitLab now supports running DAP against a customer's own vLLM, and names on-prem vLLM as its recommended serving platform for open-source models.

| Item | GitLab DAP Self-Hosted | Self-built |
| --- | --- | --- |
| Licence on an offline instance | Duo Agent Platform Self-Hosted add-on, billed through an Enterprise License Agreement | Ultimate only; all APIs used are included |
| Supported models (19.0) | Devstral 2 123B, GLM-5.1, Kimi-K2.6, MiniMax-M2.7 | Any model with reliable tool calling, including Qwen3.8-27B |
| Prompts and evaluation | Tuned and evaluated by GitLab | Owned by us |
| User interface | Native Duo Chat, IDE, flows | Issue and MR comments |

Ultimate alone does not cover DAP Self-Hosted. Even with the add-on, Qwen3.8-27B would sit outside GitLab's supported list.

## Recommended architecture

A thin orchestrator receives GitLab events and hands each task to a sandbox job on a runner, where the agent edits, builds and tests before anything reaches an MR.

&#91;embedded content: agent platform architecture · 7 components\]

The orchestrator never edits code itself. Iteration happens inside the runner job in seconds; the MR pipeline is the independent final check.

### Components

- **Orchestrator** (inside the air gap, beside GitLab). Receives webhooks: pipeline events with `status=failed`, `@agent` mentions in issue or MR comments, and a label such as `agent::implement`. Decides what to do, then starts a sandbox job.
- **Sandbox runner job.** A dedicated pipeline on a runner tagged `agent-sandbox` clones the repo and runs a headless coding agent. It edits files, runs build and tests in the container, and iterates against vLLM. Only when local tests pass does it push a branch and open an MR.
- **Why not edit through the API.** Editing via the API and waiting for the MR pipeline costs one 5–15 minute cycle per attempt; in-container iteration costs seconds.
- **Agent inside the job.** No need to write the loop from scratch: OpenHands, Aider, OpenCode, mini-SWE-agent or Qwen Code all run headless against an OpenAI-compatible endpoint. Mirror the chosen agent's image and dependencies into the internal registry.

### Workflows, in order of return on effort

1. **Pipeline fixer (start here).** Fetch failed jobs and their traces (`GET /projects/:id/jobs/:job_id/trace`), the `.gitlab-ci.yml` and the diff that broke it. Classify the failure as code bug, CI config or infra/flaky.
   - Code and CI config failures get a fix attempt; flaky failures get a retry and a comment.
   - Validate CI config changes with `POST /projects/:id/ci/lint` before committing.
2. **Issue to MR.** The issue description becomes a plan comment; a human approves it; the sandbox implements it and opens an MR.
3. **MR review and assist.** The agent comments on diffs; a reviewer replies `@agent fix this` to trigger a change.

## What decides whether it works

Least privilege, isolated runners and disciplined context matter more than the model choice.

- **Token model.** Use a Service Account (included in Ultimate) with a group or project access token as the default: actions are attributed to a bot identity and the token survives staff changes. Support a personal access token as the alternative, selectable per project in the orchestrator's configuration, for when work must appear under a named engineer; limit its scopes (api, or read\_api plus write\_repository), set an expiry, and keep it in the orchestrator's secret store, never in CI variables. Either way, the identity gets the Developer role only. Protected branches plus MR approval rules mean the agent can never merge to `main` alone.
- **Runner isolation.** Use a Docker or Kubernetes executor, unprivileged, with egress limited to GitLab and the vLLM endpoint. Agent branches are unprotected, so protected CI variables (deploy keys, prod secrets) never reach the sandbox.
- **Prompt injection.** Issue text, MR comments and job logs are untrusted input to an agent holding a write token. Least privilege is the real defence, not prompt wording.
- **Context discipline.** Do not fill the 260K window. Extract the failing section of a job trace and grep for errors first. A 27B dense model reasons better on 20–40K tokens of focused context than on 200K.
- **Tool calling in vLLM.** Run with `--enable-auto-tool-choice` and the correct Qwen tool-call parser. Test multi-step tool use on real repositories before trusting it.
- **Loop limits.** Cap fix attempts (for example 3) and wall-clock time per job. Always end with a readable MR or comment saying what was tried, including on failure.
- **Concurrency.** Each sandbox job holds an agent session against vLLM. Limit concurrent jobs on the `agent-sandbox` runner tag to what the GPU serves without queueing.

## Gaps against DAP and next step

Build the pipeline fixer first: it has a clear success signal (the pipeline turns green), narrow scope and immediate value.

What the self-built platform will not have:

- No native Duo Chat panel or IDE integration; issue and MR comments are the interface.
- No GitLab-tuned prompts or evaluations; prompt quality and regression tests are ours to own.
- Reliable issue-to-MR on non-trivial codebases is the hard part. Most of the effort goes into evaluation, not plumbing.

Once the pipeline fixer is proven, extend the same orchestrator and sandbox to issue-to-MR, then to MR assist.

## Sources

- [More AI models for GitLab Duo Agent Platform Self-Hosted](https://about.gitlab.com/blog/more-ai-models-for-duo-agent-platform-self-hosted/) — GitLab blog, May 2026
- [Self-hosted models](https://docs.gitlab.com/ee/administration/gitlab_duo_self_hosted.html) — GitLab Docs
