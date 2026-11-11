#!/usr/bin/env bash
# Post-deploy checks for lab.swarmpit.dev and vm.swarmpit.dev (plan section 6, step 10).
# Prints only status lines. Pass an image path (e.g. vm/swarm-<sha12>.ext2) to also check R2.
set -uo pipefail
LAB=https://lab.swarmpit.dev
VM=https://vm.swarmpit.dev
IMG=${1:-}
check() { if eval "$2" >/dev/null 2>&1; then echo "ok   $1"; else echo "FAIL $1"; FAILED=1; fi; }
FAILED=0
H=$(curl -sI "$LAB/" | tr -d '\r')
check "lab / returns 200"            'grep -q "^HTTP/[0-9.]* 200" <<<"$H"'
check "lab / is text/html"           'grep -qi "^content-type: text/html" <<<"$H"'
check "lab COOP same-origin"         'grep -qi "^cross-origin-opener-policy: same-origin" <<<"$H"'
check "lab COEP require-corp"        'grep -qi "^cross-origin-embedder-policy: require-corp" <<<"$H"'
check "lab CSP present"              'grep -qi "^content-security-policy:" <<<"$H"'
check "lab no-transform"             'grep -qi "^cache-control:.*no-transform" <<<"$H"'
J=$(curl -sI "$LAB/js/app.js" | tr -d '\r')
check "lab /js/app.js returns 200"   'grep -q "^HTTP/[0-9.]* 200" <<<"$J"'
check "assets carry COEP"            'grep -qi "^cross-origin-embedder-policy: require-corp" <<<"$J"'
F=$(curl -sI "$LAB/vmframe" | tr -d '\r')
check "lab /vmframe returns 200 (no redirect)" 'grep -q "^HTTP/[0-9.]* 200" <<<"$F"'
check "lab /vmframe carries COEP"    'grep -qi "^cross-origin-embedder-policy: require-corp" <<<"$F"'
check "lab /vmframe.html resolves"   'curl -sfL -o /dev/null "$LAB/vmframe.html"'
check "lab converter present"        'curl -sf -o /dev/null "$LAB/player/js/convert/index.js"'
check "lab player present"           'curl -sf -o /dev/null "$LAB/player/js/player/player.js"'
check "lab vm-manifest.json present" 'curl -sf "$LAB/vm-manifest.json" | python3 -c "import json,sys; assert isinstance(json.load(sys.stdin).get(\"filename\"), str)"'
check "r2.dev not used (vm host resolves)" 'host vm.swarmpit.dev'
if [[ -n "$IMG" ]]; then
  curl -s -o /dev/null -H 'Range: bytes=0-1' "$VM/$IMG"       # fill cache without Origin
  R=$(curl -s -o /dev/null -D - -H 'Range: bytes=0-1' -H "Origin: $LAB" "$VM/$IMG" | tr -d '\r')
  check "vm range 206"               'grep -q "^HTTP/[0-9.]* 206" <<<"$R"'
  check "vm ACAO lab origin"         'grep -qi "^access-control-allow-origin: $LAB" <<<"$R"'
  check "vm CORP cross-origin"       'grep -qi "^cross-origin-resource-policy: cross-origin" <<<"$R"'
  check "vm cache HIT"               'grep -qi "^cf-cache-status: HIT" <<<"$R"'
  check "vm HEAD content-length"     'curl -sI "$VM/$IMG" | grep -qi "^content-length: [1-9]"'
fi
echo "Also check in the browser console on $LAB: crossOriginIsolated === true"
exit $FAILED
