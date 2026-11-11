# swarmpit-instigation dataset viewer

A local viewer for the `instigation.dataset.v1` package. It has no dependencies and no build step,
and it is meant for curating training data.

> **Adversarial content.** Real packages hold board seeds and agent responses that were built to draw
> autonomous agents into communicating. Use them only for defensive research and do not publish
> them. Never open a real package from a coding-agent session, and do not screenshot it there.
> Develop against the placeholder fixture below.

## Running it

- **Real package:** run the `serve [--port N]` subcommand of `lab/study/dataset.py`. The server binds to
  127.0.0.1 only and is read-only.
- **Placeholder fixture (for development):** run
  `python3 lab/study/dataset_viewer/fixture/serve_fixture.py`, then open http://127.0.0.1:8765/.

### Serving layout

`dataset.py serve` is expected to use the same layout as `serve_fixture.py`:

| path | serves |
|---|---|
| `/`, `/index.html`, `/viewer.js`, `/lib.js`, `/viewer.css` | files from this directory |
| `/data/<file>` | the latest package: `manifest.json`, `schema.json`, `DATASHEET.md`, `seeds.jsonl`, `evaluations.jsonl`, `responses.jsonl`, `pairs.jsonl`, `sft.jsonl` |
| `/favicon.ico` | `204` |

You can point the viewer at a different package directory on the same origin with
`?data=<relative/path/>`. It rejects absolute and cross-origin URLs.

## Views

All view state lives in the URL hash, for example `#view=seeds&split=train&label=instigated&sort=ci_lo`.
Reloading the page or sharing the URL keeps the selection.

1. **Overview**
   - file counts, plus sha256 checks against `manifest.json`
   - split sizes, including the number of lineage roots per split
   - label balance for seeds and for episodes
   - label balance per split; seeds with no evaluation are counted as unevaluated, never as negatives
   - a coverage matrix of goal·model·setup (agent count, step budget) × channel, showing the writer rate (the share of episodes with at least one writer). Clicking a cell filters the seeds.
   - trigger-code distribution and composition (origin, generation, length band, dedup)
2. **Seeds**
   - facets: split, origin, generation, length band, trigger code, context, label (instigated, not instigated, pile-in, unevaluated; judged within the selected context when one is set), near-dup cluster. A context is goal · model · channel · agent count · step budget, and id search
   - sort by writer rate, CI95 lower bound, best or mean score, evaluation count, pile-in rate, generation or chars
   - 50 rows per page
3. **Seed detail**
   - the board rendered like a forum, plus the flat `text` field
   - an outcome table per context
   - each episode with its outcome, per-agent trigger codes and the responses it elicited
   - lineage (root→seed chain and children) and near-dup siblings
   - a link to the pairs this seed appears in
   - prev and next links within the current filtered, sorted list
4. **Pairs:** filter by context, split or seed. Opening a pair shows the chosen and rejected boards side by side, with the margin.
5. **Responses:** agent posts elicited in episodes, filtered by trigger code, context or kind.
6. **Export:** applies the current seed filter to every file. Pairs are kept only when both of their seeds are selected. With a context selected, evaluations, responses, pairs and sft rows are also restricted to exactly that context. Each file downloads as JSONL, built in the browser, and its first row is `{"_header": {dataset, schema, version, file, filter, sort, selected_seeds, exported_ts, rows, note}}`.
7. **Banner:** the "Adversarial content — for defensive research; do not publish" banner stays visible on every view.

Keys: `1`–`5` switch views, `/` focuses the seed search, `j`/`k` step to the next or previous seed in
the detail view, and `Esc` goes back to the list or closes a pair. A skip link is provided, focus is
visible, and filter results are announced through an `aria-live` region.

## Safety properties

- All dataset text reaches the page as text nodes (`textContent`). The code has no `innerHTML` or
  any other HTML-string sink, and it never evaluates loaded content. `test_dataset_viewer.mjs`
  enforces this, and renders a markup-shaped placeholder probe to show that it stays inert.
- A strict CSP in `index.html`:
  - `default-src 'none'`
  - `script-src 'self'`, `style-src 'self'`, `connect-src 'self'`
  - no inline script and no remote origins
- Links are built only from view state (hash links) or the package's data path. Content is never used as a URL.
- Malformed JSONL lines are skipped and counted, never echoed.

## Files

- `index.html`, `viewer.css`, `viewer.js`: the UI. `viewer.js` is an ES module.
- `lib.js`: pure helpers for parsing, facets, sorting, hash state, aggregates and export. It is unit-tested under `node --test`.
- `fixture/make_fixture.py`: writes a synthetic **placeholder** study tree (registry, candidates with lineage and near-duplicates, run dirs with content sinks and trigger artifacts) into a temp dir and runs the real builder (`study/dataset.py build`) over it. Every string is lorem filler, and the viewer is always tested against exactly what the builder emits. Nothing is committed: `--out DIR` gets the version files, `--root DIR` the whole builder output dir.
- `fixture/serve_fixture.py`: a loopback dev server that builds the placeholder package into a temp dir and serves it.

## Tests

```sh
node --test lab/study/test_dataset_viewer.mjs
```

The tests cover:

- the helpers and the static safety checks
- the fixture contract on builder output: determinism, manifest sha256s, schema types, root-hash splits, pairs within a split
- an end-to-end run through the real `dataset.py serve` (CSP, `/data/` layout, integrity badges, seed detail, pairs, responses)
- a headless-Chromium UI smoke test of every view, keyboard navigation, the export download, inert rendering and the phone layout. It runs only when playwright-core and a Chromium binary can be found (`PLAYWRIGHT_CORE`, `CHROME_PATH`; default candidates are `/tmp/dsv-pw/node_modules/playwright-core` and the ms-playwright chromium-1217 cache).

Screenshots of the placeholder fixture are written to `/tmp/dataset-viewer-shots/`.
