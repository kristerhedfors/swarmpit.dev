#!/usr/bin/env bash
# Configure Cloudflare for the in-browser lab (docs/browser-sandbox-plan.md, section 6).
#
# Idempotent: safe to re-run. Prints only step names, ok/FAIL and Cloudflare error codes;
# never the token. Reads CLOUDFLARE_API_TOKEN (or CLOUDFLARE_ACCOUNT_TOKEN) and
# CLOUDFLARE_ACCOUNT_ID from the main checkout's .env, or from $CF_ENV_FILE.
#
#   lab/web/cloudflare/setup.sh            # zone + R2 + rules, then deploy the lab placeholder
#   lab/web/cloudflare/setup.sh --dry-run  # show what would be called, change nothing
#
# Note: the cache and transform rules are written as the zone's whole phase entrypoint,
# replacing any other rules in those two phases (swarmpit.dev has none today).
set -euo pipefail

DRY=0; [[ "${1:-}" == "--dry-run" ]] && DRY=1
HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${CF_ENV_FILE:-$(git -C "$HERE" rev-parse --path-format=absolute --git-common-dir)/../.env}"
[[ -f "$ENV_FILE" ]] || { echo "missing env file: $ENV_FILE" >&2; exit 1; }
eval "$(grep -E '^CLOUDFLARE_(API_TOKEN|ACCOUNT_TOKEN|ACCOUNT_ID)=' "$ENV_FILE")"
TOKEN="${CLOUDFLARE_API_TOKEN:-${CLOUDFLARE_ACCOUNT_TOKEN:-}}"
ACCOUNT="${CLOUDFLARE_ACCOUNT_ID:-}"
[[ -n "$TOKEN" && -n "$ACCOUNT" ]] || { echo "token or account id missing in $ENV_FILE" >&2; exit 1; }

ZONE_NAME=swarmpit.dev
LAB_HOST=lab.swarmpit.dev
VM_HOST=vm.swarmpit.dev
BUCKET=swarmpit-vm
API=https://api.cloudflare.com/client/v4

# call METHOD PATH [JSON] -> prints the response body
call() {
  local m=$1 p=$2 body=${3:-}
  if (( DRY )) && [[ "$m" != GET ]]; then echo '{"success":true,"result":null,"errors":[]}'; return; fi
  curl -sS -X "$m" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    ${body:+--data "$body"} "$API$p"
}
# report LABEL [ok-codes...] < response -> "LABEL ok|FAIL [codes]"; returns 1 on failure
report() {
  python3 -c '
import sys, json
label, ok_codes = sys.argv[1], set(map(int, sys.argv[2:]))
d = json.load(sys.stdin)
codes = [e.get("code") for e in d.get("errors", [])]
ok = d.get("success") or (codes and all(c in ok_codes for c in codes))
print(label.ljust(34), "ok" if ok else "FAIL", codes if codes else "")
sys.exit(0 if ok else 1)' "$@"
}
step() { local label=$1; shift; call "$@" | report "$label" || FAILED=1; }
FAILED=0
(( DRY )) && echo "dry run: no changes will be made"

ZONE=$(call GET "/zones?name=$ZONE_NAME&account.id=$ACCOUNT" | python3 -c 'import sys,json; r=json.load(sys.stdin)["result"]; print(r[0]["id"] if r else "")')
[[ -n "$ZONE" ]] || { echo "zone $ZONE_NAME not found in account" >&2; exit 1; }
echo "zone $ZONE_NAME found"

echo "== zone settings (step 0)"
step "ssl=strict"                PATCH "/zones/$ZONE/settings/ssl" '{"value":"strict"}'
step "always_use_https=on"       PATCH "/zones/$ZONE/settings/always_use_https" '{"value":"on"}'
step "min_tls_version=1.2"       PATCH "/zones/$ZONE/settings/min_tls_version" '{"value":"1.2"}'
step "rocket_loader=off"         PATCH "/zones/$ZONE/settings/rocket_loader" '{"value":"off"}'
step "email_obfuscation=off"     PATCH "/zones/$ZONE/settings/email_obfuscation" '{"value":"off"}'
step "automatic_https_rewrites=off" PATCH "/zones/$ZONE/settings/automatic_https_rewrites" '{"value":"off"}'
step "tiered_caching=on"         PATCH "/zones/$ZONE/argo/tiered_caching" '{"value":"on"}'
step "smart_tiered_cache=on"     PATCH "/zones/$ZONE/cache/tiered_cache_smart_topology_enable" '{"value":"on"}'
step "dnssec=active"             PATCH "/zones/$ZONE/dnssec" '{"status":"active"}'

echo "== R2 bucket (step 1)"
# 10004: bucket already exists
call POST "/accounts/$ACCOUNT/r2/buckets" "{\"name\":\"$BUCKET\"}" | report "bucket $BUCKET" 10004 || FAILED=1
step "bucket CORS" PUT "/accounts/$ACCOUNT/r2/buckets/$BUCKET/cors" "$(cat <<EOF
{"rules":[{"allowed":{"origins":["https://$LAB_HOST"],"methods":["GET","HEAD"],"headers":["Range"]},
 "exposeHeaders":["Content-Range","Content-Length","ETag","Accept-Ranges"],"maxAgeSeconds":86400}]}
EOF
)"
step "r2.dev disabled" PUT "/accounts/$ACCOUNT/r2/buckets/$BUCKET/domains/managed" '{"enabled":false}'
if call GET "/accounts/$ACCOUNT/r2/buckets/$BUCKET/domains/custom" \
   | python3 -c 'import sys,json; d=json.load(sys.stdin); sys.exit(0 if any(x.get("domain")==sys.argv[1] for x in (d.get("result") or {}).get("domains",[])) else 1)' "$VM_HOST"; then
  echo "custom domain $VM_HOST             ok (already connected)"
else
  step "custom domain $VM_HOST" POST "/accounts/$ACCOUNT/r2/buckets/$BUCKET/domains/custom" \
    "{\"domain\":\"$VM_HOST\",\"zoneId\":\"$ZONE\",\"enabled\":true,\"minTLS\":\"1.2\"}"
fi

echo "== cache and header rules (step 2)"
cache_rule() {  # $1: extra action_parameters JSON fragment
  cat <<EOF
{"rules":[{"description":"vm-immutable","expression":"(http.host eq \"$VM_HOST\")","action":"set_cache_settings",
 "action_parameters":{"cache":true,"edge_ttl":{"mode":"bypass_by_default"},"browser_ttl":{"mode":"respect_origin"}$1}}]}
EOF
}
if ! call PUT "/zones/$ZONE/rulesets/phases/http_request_cache_settings/entrypoint" \
     "$(cache_rule ',"origin_range_requests":{"mode":"on"}')" | report "cache rule vm-immutable"; then
  echo "  retrying without origin_range_requests (not accepted by the API); set it in the dashboard if available"
  step "cache rule vm-immutable (basic)" PUT "/zones/$ZONE/rulesets/phases/http_request_cache_settings/entrypoint" "$(cache_rule '')"
fi
step "header rule vm-corp" PUT "/zones/$ZONE/rulesets/phases/http_response_headers_transform/entrypoint" "$(cat <<EOF
{"rules":[{"description":"vm-corp","expression":"(http.host eq \"$VM_HOST\")","action":"rewrite",
 "action_parameters":{"headers":{
  "Cross-Origin-Resource-Policy":{"operation":"set","value":"cross-origin"},
  "X-Content-Type-Options":{"operation":"set","value":"nosniff"},
  "Access-Control-Allow-Origin":{"operation":"set","value":"https://$LAB_HOST"},
  "Access-Control-Expose-Headers":{"operation":"set","value":"Content-Range, Content-Length, ETag, Accept-Ranges"}}}}]}
EOF
)"

echo "== lab placeholder Worker (step 3)"
if (( DRY )); then
  echo "would run: wrangler deploy in lab/web/worker"
else
  (cd "$HERE/../worker" && CLOUDFLARE_API_TOKEN="$TOKEN" CLOUDFLARE_ACCOUNT_ID="$ACCOUNT" \
     npx --yes wrangler@4 deploy >/tmp/swarmpit-wrangler.log 2>&1) \
    && echo "wrangler deploy swarmpit-lab        ok" \
    || { echo "wrangler deploy swarmpit-lab        FAIL (see /tmp/swarmpit-wrangler.log)"; FAILED=1; }
fi

echo "== DNSSEC DS record (add at the registrar unless it is Cloudflare Registrar)"
call GET "/zones/$ZONE/dnssec" | python3 -c '
import sys, json
r = json.load(sys.stdin).get("result") or {}
print("status:", r.get("status"))
if r.get("ds"): print("DS:", r["ds"])'

exit $FAILED
