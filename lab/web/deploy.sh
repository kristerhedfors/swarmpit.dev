#!/usr/bin/env bash
# Upload the VM image to R2 (if not already there) and deploy the lab site to lab.swarmpit.dev.
# Reads the Cloudflare token and account id from the main checkout's .env (see lab/web/cloudflare/setup.sh).
#   lab/browser/image/build.sh && lab/web/build.sh && lab/web/deploy.sh
set -euo pipefail
ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
ENV_FILE="${CF_ENV_FILE:-$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)/../.env}"
eval "$(grep -E '^CLOUDFLARE_(API_TOKEN|ACCOUNT_TOKEN|ACCOUNT_ID)=' "$ENV_FILE")"
export CLOUDFLARE_API_TOKEN="${CLOUDFLARE_API_TOKEN:-${CLOUDFLARE_ACCOUNT_TOKEN:-}}" CLOUDFLARE_ACCOUNT_ID
# the wrangler pinned in lab/web/package.json (npm ci), not whatever 4.x npx would fetch
WRANGLER="$ROOT/lab/web/node_modules/.bin/wrangler"
[[ -x "$WRANGLER" ]] || { echo "missing $WRANGLER; run: (cd lab/web && npm ci)" >&2; exit 1; }
NAME=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["filename"])' "$ROOT/dist/vm/manifest.json")
if curl -sfI "https://vm.swarmpit.dev/vm/$NAME" >/dev/null; then
  echo "image vm/$NAME already in R2"
else
  echo "uploading vm/$NAME"
  "$WRANGLER" r2 object put "swarmpit-vm/vm/$NAME" --remote --file "$ROOT/dist/vm/$NAME" \
    --content-type application/octet-stream --cache-control "public, max-age=31536000, immutable" >/tmp/swarmpit-r2.log 2>&1 \
    || { echo "upload failed (see /tmp/swarmpit-r2.log)"; exit 1; }
fi
(cd "$ROOT/lab/web/worker" && "$WRANGLER" deploy >/tmp/swarmpit-wrangler.log 2>&1) \
  && echo "deployed lab.swarmpit.dev" || { echo "deploy failed (see /tmp/swarmpit-wrangler.log)"; exit 1; }
