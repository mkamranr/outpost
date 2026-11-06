#!/usr/bin/env bash
# Restrict sandbox containers to GitLab, vLLM and the orchestrator only.
# Run on the runner host (as root) after Docker starts; re-run after Docker restarts (or install as a systemd unit).
#   GITLAB_IPS="10.0.0.10" VLLM_IPS="10.0.0.20" ORCH_IPS="10.0.0.30" REGISTRY_IPS="10.0.0.40" ./egress-firewall.sh
set -euo pipefail

NETWORK=${NETWORK:-agent-sandbox}
SUBNET=${SUBNET:-172.30.0.0/24}
: "${GITLAB_IPS:?set GITLAB_IPS}" "${VLLM_IPS:?set VLLM_IPS}" "${ORCH_IPS:?set ORCH_IPS}"

if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
  docker network create --driver bridge --subnet "$SUBNET" \
    -o com.docker.network.bridge.name=br-agent "$NETWORK"
fi

CHAIN=AGENT-SANDBOX
iptables -N "$CHAIN" 2>/dev/null || iptables -F "$CHAIN"
iptables -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
for ip in $GITLAB_IPS $VLLM_IPS $ORCH_IPS ${REGISTRY_IPS:-} ${DNS_IPS:-}; do
  iptables -A "$CHAIN" -d "$ip" -j RETURN
done
iptables -A "$CHAIN" -j LOG --log-prefix "agent-sandbox-drop: " --log-level 4 -m limit --limit 10/min
iptables -A "$CHAIN" -j DROP

# DOCKER-USER is evaluated before Docker's own rules for forwarded container traffic.
iptables -C DOCKER-USER -s "$SUBNET" -j "$CHAIN" 2>/dev/null || iptables -I DOCKER-USER -s "$SUBNET" -j "$CHAIN"
echo "sandbox egress limited to: $GITLAB_IPS $VLLM_IPS $ORCH_IPS ${REGISTRY_IPS:-} ${DNS_IPS:-}"
