#!/usr/bin/env bash
# Prove the images work with NO internet. Everything runs on a Docker --internal network whose only other
# member is a stub LLM, so any hidden download (model price maps, tokenizers, update checks) fails here
# on the build host, not later on site.
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION=${1:?usage: airgap-check.sh <version>}
SANDBOX="agent-platform/sandbox-python:$VERSION"
ORCH="agent-platform/orchestrator:$VERSION"
NET="agent-airgap-check-$$"
STUB="stub-llm-$$"
WORK=$(mktemp -d)

cleanup() {
  docker rm -f "$STUB" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
fail() { echo "AIRGAP CHECK FAILED: $*" >&2; exit 1; }
OFFLINE_ERRORS='NameResolutionError|Temporary failure in name resolution|getaddrinfo failed|Name or service not known|Failed to establish a new connection'
LLM_ENV=(-e AGENT_LLM_BASE_URL="http://stub-llm:8000/v1" -e AGENT_LLM_MODEL="qwen3.8-27b")
# Run as the host user so git accepts the bind-mounted repo (no "dubious ownership") and files stay writable.
AS_ME=(-u "$(id -u):$(id -g)" -e HOME=/tmp)

docker network create --internal "$NET" >/dev/null
docker run -d --rm --name "$STUB" --network "$NET" --network-alias stub-llm \
  -v "$PWD/scripts:/scripts:ro" --entrypoint /opt/agent/bin/agent-python "$SANDBOX" \
  /scripts/stub_llm.py --port 8000 >/dev/null

echo "-- orchestrator imports and loads the agent and flow catalog offline (incl. agent-runner for inline chat)"
docker run --rm --network "$NET" --entrypoint python "$ORCH" -c \
  "from app.main import create_app; import app.inline; from app.registry import Registry; r = Registry.load('/app/agents'); print('agents:', len(r.agents), 'flows:', len(r.flows))" \
  || fail "orchestrator image"

echo "-- aider starts offline"
docker run --rm --network "$NET" "$SANDBOX" aider --version || fail "aider --version"

echo "-- agent-runner edit flow (classify + aider + tests) against the stub LLM"
mkdir -p "$WORK/repo"
cp -r eval/fixtures/py-off-by-one/repo/. "$WORK/repo/"
cp build/airgap-bundles/edit.json "$WORK/bundle.json"
cp build/airgap-bundles/readonly.json "$WORK/bundle-ro.json"

# shellcheck disable=SC2016  # expanded inside the container
docker run --rm --network "$NET" -v "$WORK:/work" -w /work/repo "${AS_ME[@]}" "${LLM_ENV[@]}" --entrypoint bash "$SANDBOX" -c '
  git init -q && git add -A && git -c user.name=c -c user.email=c@c commit -qm init &&
  sed -i "s/__SHA__/$(git rev-parse HEAD)/" /work/bundle.json /work/bundle-ro.json &&
  agent-runner --bundle-file /work/bundle.json --repo-dir /work/repo --result-file /work/result.json
' > "$WORK/run.log" 2>&1 || true
cat "$WORK/run.log"
if grep -Eq "$OFFLINE_ERRORS" "$WORK/run.log"; then fail "a component tried to reach the internet (see log above)"; fi
[ -f "$WORK/result.json" ] || fail "agent-runner produced no result"
grep -q '"classification": "code"' "$WORK/result.json" || fail "classification step did not reach the stub LLM"
if grep -q '"tests_passed": true' "$WORK/result.json"; then
  echo "   aider applied the stub's edit and the fixture tests passed"
else
  echo "   WARNING: aider ran offline but the stub edit did not make tests pass; inspect the log above"
fi

echo "-- agent-runner read-only tool loop"
docker run --rm --network "$NET" -v "$WORK:/work" -w /work/repo "${AS_ME[@]}" "${LLM_ENV[@]}" "$SANDBOX" \
  agent-runner --bundle-file /work/bundle-ro.json --repo-dir /work/repo --result-file /work/result-ro.json \
  || fail "read-only flow"
grep -q '"status": "success"' "$WORK/result-ro.json" || fail "read-only flow did not succeed"

echo "AIRGAP CHECK PASSED"
