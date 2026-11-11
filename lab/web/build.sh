#!/usr/bin/env bash
# Assemble the deployable lab site in lab/web/dist: the static page, the compiled replay player, the VM image
# manifest, and the board seed.
# The seed is copied without being printed or opened (repo policy on chatter-bearing files).
#   lab/web/build.sh            (expects dist/vm/manifest.json from lab/browser/image/build.sh)
set -euo pipefail
ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
WEB="$ROOT/lab/web"; OUT="$WEB/dist"
SEED="${SEED_FILE:-$ROOT/lab/seeds/seed.json}"
[[ -f "$ROOT/dist/vm/manifest.json" ]] || { echo "missing dist/vm/manifest.json; run lab/browser/image/build.sh" >&2; exit 1; }
rm -rf "$OUT"; mkdir -p "$OUT/data"
cp -R "$WEB/public/." "$OUT/"
# The swarmpit.dev replay player (docs/browser-swarm.md §7): compile site/src and copy only the compiled js and the
# two stylesheets the player needs into dist/player/ (index.html links /player/css/*, playerhost.js imports
# /player/js/*). Never site/public/data: that holds published run bundles, not part of the lab.
SITE="$ROOT/site"
[[ -x "$SITE/node_modules/.bin/tsc" ]] || (cd "$SITE" && npm ci --silent)
(cd "$SITE" && ./node_modules/.bin/tsc -p .)
mkdir -p "$OUT/player/css"
cp -R "$SITE/public/js" "$OUT/player/js"
cp "$SITE/public/css/site.css" "$SITE/public/css/player.css" "$OUT/player/css/"
[[ -f "$OUT/player/js/player/player.js" && -f "$OUT/player/js/data/memoryLoader.js" ]] || { echo "player build incomplete" >&2; exit 1; }
# The converter turns every finished swarm (and upload) into a trace; a lab without it cannot store any run.
[[ -f "$OUT/player/js/convert/index.js" ]] || { echo "no converter (site/src/convert) in the player build" >&2; exit 1; }
[[ -f "$OUT/vmframe.html" && -f "$OUT/js/swarm.js" ]] || { echo "swarm runtime missing from public/" >&2; exit 1; }
cp "$ROOT/dist/vm/manifest.json" "$OUT/vm-manifest.json"
cp "$SEED" "$OUT/data/seed.json"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert isinstance(d.get("threads"), list)' "$OUT/data/seed.json" \
  && echo "seed ok" || { echo "seed failed schema check" >&2; exit 1; }
echo "built lab site in lab/web/dist ($(find "$OUT" -type f | wc -l | tr -d ' ') files)"
