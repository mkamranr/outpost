# Deployment (air-gapped)

You need:
* GitLab Ultimate, reachable from the orchestrator host and the runner host
* a Docker host for the orchestrator
* a Docker host for the `agent-sandbox` runner (can be the same machine for a pilot)
* vLLM serving the model with tool calling enabled, reachable from the runner host and, for the inline agents
  (`chat`, `issue-triage`, `mr-describer`, `release-notes`), from the orchestrator host
* an internal container registry
* for `security-remediator`: GitLab Ultimate security scanners (SAST, dependency scanning, secret detection) in
  the projects' pipelines, with their analyzer images mirrored to the internal registry

## 0. Build and transfer (internet side)

```bash
build/build-all.sh            # edit build/versions.env first (VERSION, base images, runner helper version)
```

The script builds `orchestrator`, `agent-layer` and `sandbox-python`, pulls Postgres and the runner helper,
and runs `build/airgap-check.sh`. That check runs everything on a Docker network with no internet access against
a stub LLM, so hidden downloads fail on the build host instead of on site. The script then writes
`dist/agent-platform-<ver>.tar` (+ `.sha256`). Carry both files across.

Air-gap side:

```bash
sha256sum -c agent-platform-<ver>.tar.sha256
tar -xf agent-platform-<ver>.tar && cd agent-platform-<ver>
REGISTRY=registry.internal ./load-and-push.sh
```

**Sandbox images for other toolchains** (Node, Java, Go…): copy `sandbox/Dockerfile.example-python`, change the
base image, and bake in the project's build and test dependencies (the sandbox has no internet at run time).
Build it on the internet side with `--build-arg AGENT_LAYER=agent-platform/agent-layer:<ver>`. The agent layer is
a self-contained `/opt/agent` (its own Python), so it can be copied onto any glibc-based image that has `bash` and `git`.

## 1. vLLM

Start vLLM with tool calling enabled, for example:

```bash
vllm serve /models/qwen3.8-27b --served-model-name qwen3.8-27b \
  --enable-auto-tool-choice --tool-call-parser hermes --max-model-len 65536 --api-key <key>
```

Use the tool-call parser documented for your Qwen release. Then verify it:

```bash
python3 scripts/check-vllm.py --base-url http://vllm.internal:8000/v1 --model qwen3.8-27b --api-key-file key.txt
```

All four checks must pass. Multi-step tool calling is what the read-only agents depend on.
If you serve under a different model name, update `sandbox/aider/model-settings.yml` and `model-metadata.json`
in the agent layer, or set `AGENT_AIDER_EDIT_FORMAT` as a sandbox CI variable.

## 2. Service account and tokens

1. Create a **service account** (Admin > Users, or the group service accounts API), e.g. `agent-bot`.
2. Give it **Developer** on each target project and on the sandbox project.
3. Create a **group or project access token** for it, with scope `api` and an expiry date.
   Save it as `deploy/secrets/gitlab_bot_token`.
4. Optional: if a project's work must appear under a named engineer, save that engineer's PAT
   (scopes `api`, or `read_api` + `write_repository`, with an expiry) as another secret file. Reference it with
   `token_ref` in `projects.yaml`.
   The `api` scope also covers the GraphQL queries `security-remediator` uses to read scanner findings. It
   only reads through GraphQL and refuses mutations.
5. On each target project, protect `main` and require MR approval. Agents then cannot merge alone, and agent
   branches stay unprotected, so protected CI variables never reach sandbox jobs.

## 3. Secrets and configuration

```bash
cd deploy
cp .env.example .env                                  # GitLab URL, bot username, sandbox project, PUBLIC_URL...
cp config/projects.yaml.example config/projects.yaml  # onboard projects, choose agents, test commands
for s in webhook_secret ticket_secret admin_token postgres_password; do openssl rand -hex 32 > secrets/$s; done
echo "postgresql+psycopg://agent:$(cat secrets/postgres_password)@postgres:5432/agent" > secrets/database_url
chmod 600 secrets/*; sudo chown 10001 secrets/*
```

`PUBLIC_URL` must be reachable **from sandbox jobs**. The runner firewall must allow it. Sandbox jobs use it
to fetch their task, post results, and call GitLab tools (`/api/tasks/<id>/tools/<name>`, authenticated by
the task ticket).

**Chat** runs inside `orchestrator-worker`, not in a sandbox job. Set `LLM_BASE_URL` and `LLM_MODEL` in `.env`
(and `LLM_API_KEY_FILE` if vLLM needs a key), then add `chat` to a project's `agents:`. `CHAT_WORKERS` sets how
many conversations run at once. They don't use sandbox slots. Chat can only read GitLab through the bot's
token, and every write it makes is its reply.

**Inline agents.** `issue-triage`, `mr-describer` and `release-notes` run in the worker the same way chat does,
share `CHAT_WORKERS`, and need the same `LLM_*` settings. Enable them per project in `agents:` and tune them in
`projects.yaml`:

```yaml
  mr_describer:
    on_open: only_if_empty      # or `always`; `@agent describe` always writes
  triage:
    apply_labels: false         # true: add the suggested labels instead of only suggesting them
```

`issue-triage` runs for every new issue, including issues from authors without Developer access. It is limited
to 20 runs per project per hour (`max_per_hour` in `agents/issue-triage.yaml`).

**Security remediator.** Add `security-remediator` to a project's `agents:` for `@agent remediate` and the
`mr-secure` flow. Automatic runs after green default-branch pipelines are off until you enable them:

```yaml
  security_remediator:
    auto: true
    branches: ["main"]
    min_severity: high          # critical | high | medium | low
    max_open_mrs: 2             # pause automatic runs while this many agent::security-fix MRs are open
```

Teams list accepted risks and false positives in `.gitlab/agent-security-ignore.yml` on the default branch (finding
ids or identifiers such as `CVE-…`/`CWE-…`, optionally `{id: …, reason: …}`). Agents can never change that file.

**Conflict resolver.** Add `conflict-resolver` to a project's `agents:` for `@agent rebase`. It needs no other
settings. For MRs that are behind without conflicts, it calls GitLab's rebase API only on `agent/*` branches (or
on every branch with `allow_push_to_mr_branch: true`). The bot account therefore needs push access to `agent/*`
branches, which it already has for its other MRs. With conflicts, it runs in the sandbox like any edit agent and
opens a replacement Draft MR.

**Team instructions.** Nothing to configure. A team that commits `.gitlab/agent-instructions.md` to its default
branch has every agent read it (example: `deploy/repo-example/agent-instructions.md`).

**Feedback and eval export** (optional, in `.env`):

| Variable | Default | Effect |
| --- | --- | --- |
| `FEEDBACK_POLL_SECONDS` | 300 | How often the worker reads 👍/👎 on agent notes and MRs (0 turns polling off) |
| `FEEDBACK_WINDOW_DAYS` | 14 | How long after a task its reactions are still read |
| `FIXTURE_MAX_REPO_MB` | 20 | Fixture exports include the whole repository up to this size, otherwise only the files the task touched |

**Flows** need no configuration. A flow is offered wherever all of its agents are enabled; `@agent help` lists
them. **Custom agents** stay off until you set `custom_agents.enabled: true` for a project in `projects.yaml`
(see [agents.md](agents.md#custom-agents-gitlabagents)).

## 4. GitLab bootstrap

GitLab admin: **Admin > Settings > Network > Outbound requests**. Allow webhook requests to the orchestrator
host (local network). Then, with a Maintainer/Owner token (used only for setup):

```bash
python3 deploy/setup/bootstrap.py --gitlab-url https://gitlab.internal --token-file ~/setup_token \
  --sandbox-project agent-platform/sandbox --orchestrator-url http://agent-orchestrator.internal:8080 \
  --webhook-secret-file deploy/secrets/webhook_secret --bot-username agent-bot \
  --llm-base-url http://vllm.internal:8000/v1 --llm-model qwen3.8-27b --llm-api-key-file key.txt \
  --project platform/payments-api            # repeat --project; add --dry-run to preview
```

The script is idempotent. It sets up:
* **The sandbox project:** private, shared runners off, CI file, LLM variables, and pipeline variables allowed for Developers.
* **Each target project:**
  * the webhook (pipeline, note, issue and MR events)
  * the sandbox added to the target's CI job-token allowlist, so jobs can clone it read-only
  * the `agent::*` labels, including `agent::security-fix`
  * Developer membership for the bot

Re-run it after upgrading to create labels that new releases add.

## 5. Sandbox runner

On the runner host:

```bash
sudo GITLAB_IPS="10.0.0.10" VLLM_IPS="10.0.0.20" ORCH_IPS="10.0.0.30" REGISTRY_IPS="10.0.0.40" \
  deploy/runner/egress-firewall.sh       # creates the `agent-sandbox` network and the egress allowlist
```

1. Register a runner on the sandbox project with tag **`agent-sandbox`**, "Run untagged jobs" **off**, and protected **off**.
2. Merge `deploy/runner/config.toml.example` into `/etc/gitlab-runner/config.toml`. The key settings are:
   * Docker executor, `privileged = false`, no `docker.sock`
   * `allowed_images` limited to `registry.internal/agent-platform/*`
   * `network_mode = "agent-sandbox"`
   * `limit` = the number of agent sessions the GPU serves without queueing
3. Keep `MAX_CONCURRENT_SANDBOXES` in `.env` at or below the runner `limit`.

Test the egress restriction from a sandbox container: `curl -m 5 https://example.com` must fail, and
`curl $AGENT_LLM_BASE_URL/models` must succeed.

## 6. Start the orchestrator

```bash
cd deploy && docker compose up -d && docker compose ps
curl -s localhost:8080/healthz          # {"ok": true, "agents": [...]}
docker compose logs -f orchestrator-worker
```

Put a TLS reverse proxy in front if GitLab requires HTTPS webhooks. Because `/admin` uses HTTP Basic with the
admin token, serve it over HTTPS only. Ideally, expose `/admin`, `/metrics` and `/api/tasks` only on the operations
network. GitLab needs only `/webhook`, and sandbox jobs need only `/api/tasks/<id>/…`. `GET /api/tasks` with header
`X-Admin-Token: $(cat secrets/admin_token)` lists recent tasks, their status, outcome and sandbox pipeline.

**Dashboard and metrics.** Open `http://<orchestrator>:8080/admin` and log in with any user name and the admin
token as the password. It shows each agent's tasks, failures, MR acceptance rate, 👍/👎, LLM tokens and dismissed review comments.
Have Prometheus scrape the same numbers:

```yaml
scrape_configs:
  - job_name: outpost
    metrics_path: /metrics
    authorization: {type: Bearer, credentials_file: /etc/prometheus/outpost_admin_token}
    static_configs: [{targets: ["agent-orchestrator.internal:8080"]}]
```

Useful alerts and graphs:
* `outpost_queue_depth{lane="sandbox"}` staying above `MAX_CONCURRENT_SANDBOXES`
* a falling ratio of `outpost_feedback{kind="mr_merged"}` to `outpost_feedback{kind="mr_closed"}` after a prompt or
  model change
* `outpost_llm_tokens` per agent, for GPU sizing and for spotting a prompt that suddenly grows. All three metrics
  are gauges over the dashboard window (30 days), not counters.
* a rising share of `outpost_review_comments{state="dismissed"}`: the review prompt needs work

## 7. Pilot

1. On a pilot project with `test` configured, push a commit that breaks a unit test.
2. Within a minute or two:
   * the pipeline fails
   * the orchestrator log shows a `pipeline-fixer` task
   * a pipeline runs in the sandbox project
   * a Draft MR `agent/pipeline-fixer-pipeline-<id>-…` appears, with a comment on the commit
3. The MR pipeline should go green. If it fails, the fixer iterates on its branch, up to 3 attempts.
4. Comment `@agent help` on an MR to see the enabled agents and flows, then try `@agent review`.
5. With `chat` enabled, comment `@agent why did the last pipeline fail?` on an MR. A reply should land in the
   thread within seconds and cite the job log. Then ask `@agent can you review this?`: chat should start the
   `code-reviewer`.
6. On a small issue, comment `@agent build`. A checklist comment tracks the `issue-to-mr` flow. After the plan
   is posted, reply `@agent approve`. A Draft MR follows, and then a review of that MR.
7. Optional agents. Each one must be added to the project's `agents:` list:
   * `issue-triage`: open an issue as a Reporter, and a triage comment appears.
   * `mr-describer`: open an MR with an empty description, and a description block appears.
   * `release-notes`: comment `@agent release-notes <previous tag>..main`.
   * `security-remediator`: needs GitLab Ultimate security scanners in the pipeline. Comment `@agent remediate`
     on an MR whose pipeline has SAST findings in the files it changes. Automatic runs on the default branch
     stay off until you set `security_remediator.auto: true`.
   * `conflict-resolver`: on an MR that conflicts with `main`, comment `@agent rebase`. A replacement Draft MR
     `… (conflicts resolved)` appears, linked from the original.
8. Commit a short `.gitlab/agent-instructions.md` to the pilot's default branch (for example "do not comment on
   naming"). Run `@agent review` again: the review should follow it.
9. Open `/admin`: the pilot's tasks are listed, with their tokens. React 👎 on one of the reviewer's inline
   comments, and within `FEEDBACK_POLL_SECONDS` the dashboard counts it as dismissed.
   `GET /api/review-memory?project_id=<id>` lists it, and the next `@agent review` is told not to repeat it. The
   task's **Export fixture** link downloads it as an eval case.
10. Run the offline evaluation and keep its report as your baseline:

```bash
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/w" -w /w -e AGENT_LLM_BASE_URL=http://vllm.internal:8000/v1 -e AGENT_LLM_MODEL=qwen3.8-27b \
  registry.internal/agent-platform/sandbox-python:<ver> agent-python eval/run.py
```

## Upgrading

1. Build and transfer the new bundle as in step 0, push the images, and set `VERSION` in `.env`.
2. If you mount your own `agents/` directory, copy in the new agent YAML files and prompt templates. The
   orchestrator refuses to start when a template is missing.
3. Merge new settings from `.env.example` and `config/projects.yaml.example` into your copies.
4. Restart: `docker compose up -d`. Both the API and the worker update the database schema on start-up. They create
   new tables (such as task links and feedback) and add new nullable columns, so no manual migration is needed.
5. Re-run `deploy/setup/bootstrap.py` for each project to add new labels.

Feedback starts at the upgrade: tasks finished before it have no links, so their reactions are not counted. Tasks
from before the tool-call log existed export without `tool_responses`.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No task created | Webhook "Recent events" in project settings. The project must be listed in `projects.yaml`. Human commands need Developer access. |
| Task fails at dispatch | Worker log. Bot token missing or expired, `sandbox_image` unset, or pipeline variables not allowed on the sandbox project. |
| Sandbox job stays pending | Runner tag `agent-sandbox`, runner `limit`, and whether `allowed_images` matches the image. |
| Sandbox can't clone | The sandbox project must be in the target's **CI/CD > Job token permissions** allowlist. Internal CA: runner `tls-ca-file`. |
| Sandbox can't post result | `PUBLIC_URL` reachable from the runner network (firewall `ORCH_IPS`). Ticket expiry is set by `QUEUE_GRACE_MINUTES`. |
| "did not finish: timed out" | Raise the agent's `timeout_minutes`, or lower concurrency so vLLM is not queueing. |
| Aider makes no edits | Try `AGENT_AIDER_EDIT_FORMAT=whole` (sandbox CI variable). Check that vLLM's `--max-model-len` covers the prompt. |
| `@agent <question>` says "I don't know the command" | Add `chat` to the project's `agents:`. |
| `security-remediator` says "no security findings" | The MR's head pipeline must run the scanners, and on an MR only findings in the changed files count. Older GitLab versions without `securityReportFindings` in GraphQL need the scanner jobs to also keep `gl-*-report.json` under `artifacts:paths`. |
| A new issue gets no triage comment | Add `issue-triage` to `agents:`. The webhook needs issue events (bootstrap enables them). The worker log shows `hourly limit` when `max_per_hour` was reached. |
| `mr-describer` does nothing on MR open | By default it only writes when the description is (nearly) empty; the task outcome says `skipped: … already has a description`. Use `@agent describe`, or set `mr_describer.on_open: always`. |
| `release-notes` asks for a range | Give two refs that exist in the project: `@agent release-notes v1.2.0..v1.3.0` (or one ref, up to the default branch). |
| Security fixes stop appearing automatically | `max_open_mrs` open MRs labelled `agent::security-fix`: merge or close them. The task outcome says `… security fix MRs are already open`. |
| Fixture export returns 409 | The task was skipped before it collected context, so there is nothing to replay. |
| `/admin` or `/metrics` returns 401 | `ADMIN_TOKEN_FILE` must be set (compose sets it) and the token sent as the HTTP Basic password or `Authorization: Bearer`. |
| No 👍/👎 on the dashboard | Reactions are polled every `FEEDBACK_POLL_SECONDS` (300) for `FEEDBACK_WINDOW_DAYS` (14). Only notes and MRs posted after this release are linked. |
| Chat replies "I could not answer that" | Worker log. `LLM_BASE_URL`/`LLM_MODEL` unset, or vLLM is unreachable from the orchestrator host. |
| A flow is missing from `@agent help` | Every agent in the flow must be enabled for the project. |
| A flow stays at "waiting for approval" | Approve with `@agent approve` (Developer access) or the `agent::approved` label on the same issue/MR. |
| `@agent rebase` says to use the Rebase button | The MR has no conflicts and its branch belongs to a person; agents rebase only `agent/*` branches unless `allow_push_to_mr_branch` is set. |
| `@agent rebase` refuses an MR with conflicts | The MR changes more than 50 files compared with its target, or touches protected paths; a replacement MR could not carry those. Resolve it by hand or with GitLab's conflict editor. |
| Tokens show `–` on the dashboard | The task ran before usage was recorded, or its engine reports none (mini-swe releases without raw responses count only calls). |
| The reviewer still repeats a dismissed point | Only 👎 (net of 👍) or a thread resolved with the file unchanged counts, and only for comments posted since this release. Check `GET /api/review-memory?project_id=…`. |
| The reviewer avoids a point it should make | A wrong entry in review memory: `DELETE /api/review-memory/<id>` with the admin token. |
| A custom agent is missing | `@agent help` lists rejected `.gitlab/agents/` files with the reason. It must be on the default branch, and `custom_agents.enabled` must be set. |
