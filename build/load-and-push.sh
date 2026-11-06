#!/usr/bin/env bash
# Run on the AIR-GAPPED side, from inside the extracted bundle directory:
#   tar -xf agent-platform-<ver>.tar && cd agent-platform-<ver> && REGISTRY=registry.internal ./load-and-push.sh
# Verifies checksums, loads every image, retags it under $REGISTRY and pushes it (NO_PUSH=1 to only load+tag).
set -euo pipefail
cd "$(dirname "$0")"
: "${REGISTRY:?set REGISTRY, e.g. registry.internal}"

echo "==> verifying checksums"
sha256sum --quiet -c MANIFEST.sha256

while read -r image file; do
  echo "==> $image"
  docker load -i "images/$file" >/dev/null
  case "$image" in
    agent-platform/*) target="$REGISTRY/$image" ;;
    */*/*)            target="$REGISTRY/${image#*/}" ;;   # drop the upstream registry host
    */*)              target="$REGISTRY/$image" ;;
    *)                target="$REGISTRY/library/$image" ;;
  esac
  docker tag "$image" "$target"
  if [ "${NO_PUSH:-}" != "1" ]; then docker push "$target"; fi
  echo "    -> $target"
done < images.txt

echo
echo "Images are in $REGISTRY. Next: docs/deployment.md (compose stack, runner, bootstrap)."
