#!/usr/bin/env bash
# Build the i386 ext2 disk image for the in-browser lab.
#   lab/browser/image/build.sh [size]   (default 296M: under the 300 MiB wrangler upload limit and the 512 MB Cloudflare cache limit)
# Output: dist/vm/swarm-<sha12>.ext2 and dist/vm/manifest.json
# Only an allowlist of lab files is staged (no seeds, runs or other data); seeds are delivered at runtime.
set -euo pipefail
SIZE=${1:-296M}
ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
OUT="$ROOT/dist/vm"; STAGE="$ROOT/dist/vm-stage"
rm -rf "$STAGE"; mkdir -p "$OUT" "$STAGE/lab/board"
cp "$ROOT"/lab/board/*.py "$STAGE/lab/board/"
cp "$ROOT/lab/agent/harness.py" "$ROOT/lab/agent/setup_vm.py" "$STAGE/lab/"
cp "$ROOT/lab/browser/guest/labctl.py" "$STAGE/lab/"
cp "$ROOT/lab/browser/image/Dockerfile" "$STAGE/"

docker build -q --platform=linux/386 -t swarmpit-vm "$STAGE" >/dev/null
docker rm -f swarmpit-vm-export >/dev/null 2>&1 || true
docker create --platform=linux/386 --name swarmpit-vm-export swarmpit-vm >/dev/null
docker export swarmpit-vm-export -o "$STAGE/rootfs.tar"
docker rm swarmpit-vm-export >/dev/null

docker run --rm -v "$STAGE:/work" debian:bookworm-slim sh -c "
  apt-get update -qq >/dev/null && apt-get install -y -qq e2fsprogs >/dev/null &&
  mkdir /rootfs && tar -xf /work/rootfs.tar -C /rootfs &&
  mkfs.ext2 -q -r 0 -b 4096 -d /rootfs /work/swarm.ext2 $SIZE"

SHA=$(shasum -a 256 "$STAGE/swarm.ext2" | cut -c1-12)
NAME="swarm-$SHA.ext2"
mv "$STAGE/swarm.ext2" "$OUT/$NAME"
BYTES=$(stat -f %z "$OUT/$NAME" 2>/dev/null || stat -c %s "$OUT/$NAME")
printf '{"filename":"%s","sha12":"%s","bytes":%s}\n' "$NAME" "$SHA" "$BYTES" > "$OUT/manifest.json"
rm -rf "$STAGE"
echo "built $NAME ($((BYTES / 1048576)) MB)"
