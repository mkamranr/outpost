#!/usr/bin/env bash
# Run on the INTERNET-CONNECTED build host. Produces dist/agent-platform-<VERSION>.tar, a self-contained bundle
# to carry into the air gap and install with load-and-push.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=versions.env
source build/versions.env

OUT=bundle/agent-platform-$VERSION
rm -rf "$OUT" && mkdir -p "$OUT/images" "$OUT/manifests" dist

echo "==> orchestrator image"
docker build --pull -f orchestrator/Dockerfile --build-arg PYTHON_BASE="$PYTHON_BASE" \
  -t "agent-platform/orchestrator:$VERSION" .

echo "==> agent layer (standalone Python + Aider + mini-SWE-agent + agent-runner)"
docker build --pull -f sandbox/Dockerfile.agent-layer \
  --build-arg BUILD_BASE="$BUILD_BASE" --build-arg PBS_URL="$PBS_URL" \
  -t "agent-platform/agent-layer:$VERSION" sandbox

echo "==> example sandbox image (python toolchain + agent layer)"
docker build --pull -f sandbox/Dockerfile.example-python \
  --build-arg AGENT_LAYER="agent-platform/agent-layer:$VERSION" \
  -t "agent-platform/sandbox-python:$VERSION" sandbox

for img in "$POSTGRES_IMAGE" $EXTRA_IMAGES; do
  echo "==> pull $img"
  docker pull "$img"
done

echo "==> offline verification"
build/airgap-check.sh "$VERSION"

echo "==> lock files"
docker run --rm --entrypoint cat "agent-platform/orchestrator:$VERSION" /app/requirements.lock \
  > "$OUT/manifests/orchestrator.lock"
docker run --rm --entrypoint cat "agent-platform/sandbox-python:$VERSION" /opt/agent/requirements.lock \
  > "$OUT/manifests/agent-layer.lock"

echo "==> save images"
: > "$OUT/images.txt"
save() {  # save <image> <file-stem>
  docker save "$1" | gzip -1 > "$OUT/images/$2.tar.gz"
  echo "$1 $2.tar.gz" >> "$OUT/images.txt"
}
save "agent-platform/orchestrator:$VERSION" orchestrator
save "agent-platform/agent-layer:$VERSION" agent-layer
save "agent-platform/sandbox-python:$VERSION" sandbox-python
save "$POSTGRES_IMAGE" postgres
for img in $EXTRA_IMAGES; do save "$img" "$(echo "$img" | tr '/:' '__')"; done

echo "==> configuration, scripts, docs"
cp -r deploy agents eval docs scripts "$OUT/"
mkdir -p "$OUT/sandbox"
cp -r sandbox/sandbox-project sandbox/Dockerfile.example-python sandbox/aider "$OUT/sandbox/"
cp build/load-and-push.sh build/versions.env README.md "$OUT/"
find "$OUT/deploy/secrets" -type f ! -name README.md -delete
rm -f "$OUT/deploy/.env" "$OUT/deploy/config/projects.yaml"
find "$OUT" -name '__pycache__' -prune -exec rm -rf {} +

(cd "$OUT" && find . -type f ! -name MANIFEST.sha256 -print0 | sort -z | xargs -0 sha256sum > MANIFEST.sha256)
tar -C bundle -cf "dist/agent-platform-$VERSION.tar" "agent-platform-$VERSION"
sha256sum "dist/agent-platform-$VERSION.tar" > "dist/agent-platform-$VERSION.tar.sha256"
echo "bundle ready: dist/agent-platform-$VERSION.tar ($(du -h "dist/agent-platform-$VERSION.tar" | cut -f1))"
