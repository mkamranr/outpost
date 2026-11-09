# Secrets

Each file holds one secret and is mounted read-only at `/run/secrets/<name>`.
Do not commit real values; `.gitignore` excludes everything here except this README.

| File | Contents |
| --- | --- |
| `webhook_secret` | Random string; set as the "Secret token" on each project webhook. `openssl rand -hex 32` |
| `ticket_secret` | Random string used to sign sandbox task tickets. `openssl rand -hex 32` |
| `admin_token` | Random string for the operator endpoints: the `/admin` dashboard (HTTP Basic password), `/metrics` (`Authorization: Bearer`), `GET /api/tasks` and fixture export (header `X-Admin-Token`). `openssl rand -hex 32` |
| `postgres_password` | Database password. |
| `database_url` | `postgresql+psycopg://agent:<postgres_password>@postgres:5432/agent` |
| `gitlab_bot_token` | Group or project access token of the service account. Role: Developer. Scope: `api`. Set an expiry. |
| `<name>` | Any further token referenced by `token_ref` in `config/projects.yaml`, e.g. a named engineer's PAT. |

The GitLab tokens are only ever read by the orchestrator. They are never passed to sandbox jobs or CI variables.

Permissions: `chmod 600 secrets/*`. The containers run as UID 10001, so `chown 10001 secrets/*`, or make the
files group-readable by a group that UID belongs to.
