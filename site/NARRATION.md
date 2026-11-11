# Narration track: voice commentary and event sound effects

Status: design, authoritative. Two parts are built in parallel:

- **Track A, the pipeline:** Python under `tools/narrate/`, the gate changes, and the Python tests.
- **Track B, the player:** TypeScript under `src/`, `tools/serve.mjs`, and the Node tests.

The two tracks meet only at the **artifact contract** (§2), which this document fixes. If the code and this document disagree, change this document in the same commit.

The examples here use neutral placeholders (`<run-id>`, `<hash>`, `Agent 3`). Never paste real run content into this file, commits, or logs (see the root `CLAUDE.md`).

---

## 0. Goals and non-goals

- Optional, pre-generated narration over a run in the site player. It has two voices:
  - **guide:** the play-by-play commentator (stock voice). Calls the basics plainly so a newcomer knows what is happening.
  - **expert:** the color commentator. Reacts, explains why a moment matters, anticipates and compares. The two go
    back and forth like a sports broadcast (§3.2). The role tokens `guide`/`expert` are the artifact contract; public
    files never name a voice.
- Subtle **event sound effects** are mixed into **the same single mp3** as the voices, one mp3 per run.
- Fully static. GitHub Pages serves only committed files, and the browser never calls ElevenLabs.
- Opt-in and off by default. Sound never autoplays.
- **Non-goal:** live or streaming TTS, per-user generation, and any narration content derived from agent-written text.

## 1. File layout

```
site/
  NARRATION.md                         this document (committed)
  .narration-cache/                    gitignored; all API outputs + intermediates
    tts/<sha256>.mp3                   raw TTS audio, keyed by request hash (§5.2)
    tts/<sha256>.json                  {durationMs, characterCost, requestId, model, status}
    pcm/<sha256>.s16                   decoded+trimmed mono 44.1 kHz s16le (voice clips, sfx clips)
    sfx-raw/<id>.<sha8>.mp3            raw sound-generation output before post-processing
    runs/<run-id>/script.json          cue script (dry-run output)
    runs/<run-id>/voice.wav, sfx.wav   mix buses (for debugging levels; never committed)
  tools/narrate/                       Python package, stdlib only (+ ffmpeg/ffprobe binaries)
    __init__.py                        VERSION = "1.0.0"
    __main__.py                        CLI entry: python3 -m tools.narrate <cmd> (run from site/)
    config.py                          load voices.json, find ffmpeg, load API key (§5.1)
    bundle.py                          ONLY module that opens core bundles / index; projects to SafeRun (§3.1)
    vocab.py                           TEMPLATES, ENUM tables, render(), validate_cue() (§3.6)
    script.py                          cue selection + voice assignment + pacing (§3.2-3.4)
    timemap.py                         audio-master time map + inverse (§3.5)
    sfx.py                             SFX library build, event->sfx mapping, thinning (§4)
    eleven.py                          urllib client: tts_with_timestamps(), sound_generation() (§5)
    tts.py                             cached synthesis of a script's cues
    mix.py                             PCM placement (stdlib) + ffmpeg ducking/static gain/limit/encode (§6)
    manifest.py                        write + validate swarmpit.narration/1 and the narration index (§2)
    voices.json                        committed config (§1.1)
    sfx/<id>.mp3                       committed, post-processed SFX library (small; reused by every run)
    sfx/library.json                   committed lock: {id: {promptSha, sha256, bytes, durationMs}}
  tools/test_narrate.py                Python unittest suite (§8.1)
  tools/test/narration.test.mjs        Node test suite (§8.2)
  public/audio/narration/<run-id>.<h8>.mp3     committed; h8 = first 8 hex of sha256(mp3 bytes)
  public/data/narration/<run-id>.json          committed manifest (§2.1)
  public/data/narration/index.json             committed narration index (§2.2)
  src/player/timemap.ts                pure: a2r, r2a, cueAt, assertNarration (DOM-free, SPEC §3)
  src/player/narration.ts              <audio> controller (DOM)
```

Placement rules:

- Do **not** put anything under `public/data/runs/`. The exporter deletes unknown files there, and both orphan tests require that directory to hold exactly the core and detail bundles.
- `public/audio/narration/` and `public/data/narration/` are never pruned by `export.py`.
- `.narration-cache/` is listed in `site/.gitignore`. `NARRATE_CACHE` overrides the location (tests use a temp dir).
- Old mp3s for a run are deleted by `build` when it writes a new one. Only the file referenced by the manifest remains.

### 1.1 `tools/narrate/voices.json` (committed, not public)

The voice ids live only in `voices.json`; the example below uses placeholders so this document cannot drift from
the config (a test checks no Python module repeats them).

This is the single source of truth for voice ids, models, settings, SFX prompts, and gains. No other file may hard-code a voice id. Public files refer to voices only by the role tokens `guide` and `expert`. The cloned voice's personal name must never appear under `public/`, because the gate bans that literal. Do not put the personal name in this file either; the role label is enough.

```json
{
  "version": 1,
  "tts": {
    "endpoint": "/v1/text-to-speech/{voice_id}/with-timestamps",
    "outputFormat": "mp3_44100_128",
    "model": "eleven_v4",
    "fallbackModel": "eleven_multilingual_v2",
    "seed": 42,
    "concurrency": 2
  },
  "voices": {
    "guide":  { "voiceId": "<guide voice id>", "label": "Play-by-play",
                "settings": {"stability": 0.55, "similarity_boost": 0.75, "style": 0, "use_speaker_boost": true, "speed": 1.0},
                "gainDb": 0, "charsPerSec": 14.5 },
    "expert": { "voiceId": "<expert voice id>", "label": "Color",
                "settings": {"stability": 0.5, "similarity_boost": 0.8, "style": 0.1, "use_speaker_boost": true, "speed": 1.0},
                "gainDb": 0, "charsPerSec": 13.5 }
  },
  "models": { "<exact-model-id>": "<author-written spoken name>" },
  "sfx": {
    "model": "eleven_text_to_sound_v2", "promptInfluence": 0.6, "outputFormat": "mp3_44100_128",
    "busGainDb": -20,
    "library": {
      "boot":    {"prompt": "soft low digital power-on blip, very short, clean",        "seconds": 0.6, "gainDb": -2, "maxMs": 450},
      "read":    {"prompt": "very soft paper tick, tiny UI click, dry",                 "seconds": 0.5, "gainDb": -8, "maxMs": 120},
      "post":    {"prompt": "soft typewriter key thunk, single, muted",                 "seconds": 0.5, "gainDb": -4, "maxMs": 200},
      "social":  {"prompt": "gentle two-note wooden knock, soft, friendly",             "seconds": 0.6, "gainDb": -4, "maxMs": 300},
      "join":    {"prompt": "soft airy whoosh in, very short",                          "seconds": 0.6, "gainDb": -5, "maxMs": 350},
      "contact": {"prompt": "warm soft bell ping, single, short decay",                 "seconds": 1.0, "gainDb": 0,  "maxMs": 800},
      "solve":   {"prompt": "soft bright two-note rising chime, gentle",                "seconds": 1.0, "gainDb": 0,  "maxMs": 900},
      "leak":    {"prompt": "subtle low warning tone, soft muted pulse, not alarming",  "seconds": 1.0, "gainDb": -1, "maxMs": 900},
      "crash":   {"prompt": "soft muffled glitch thud, short",                          "seconds": 0.6, "gainDb": -3, "maxMs": 400},
      "end":     {"prompt": "soft descending blip, power down, very short",             "seconds": 0.6, "gainDb": -5, "maxMs": 400},
      "segment": {"prompt": "very soft airy swell, ambient transition, short",          "seconds": 1.5, "gainDb": -2, "maxMs": 1400},
      "outro":   {"prompt": "soft warm resolving pad note, gentle fade",                "seconds": 2.0, "gainDb": -1, "maxMs": 2000}
    }
  },
  "mix": { "sampleRate": 24000, "channels": 1, "bitrate": "24k", "lufs": -16, "truePeak": -1.5, "lra": 11,
           "duck": {"threshold": 0.03, "ratio": 6, "attackMs": 15, "releaseMs": 250} }
}
```

- `models` maps an exact `run.model` id to an author-written spoken name. Any model not in the table is never spoken (§3.6).
- Prompts and gains are initial values. Tune them by ear, then run `sfx --force <id>`.

## 2. Artifact contract (pipeline → player)

All times are integer milliseconds. **audioMs** is a position in the mp3. **runMs** is the run clock (`model.tEnd` = `run.tEndMs`).

### 2.1 Manifest `public/data/narration/<run-id>.json`

```json
{
  "schema": "swarmpit.narration/1",
  "generator": "narrate/1.0.0",
  "runId": "<run-id>",
  "runHash": "<card.hash from data/index.json at build time>",
  "audio": "../../audio/narration/<run-id>.<h8>.mp3",
  "audioBytes": 0,
  "durationMs": 0,
  "runEndMs": 0,
  "timeMap": [[0, 0], [3200, 0], [9000, 5800], ["...", "..."], [0, 0]],
  "cues": [
    { "audioMs": 0, "endMs": 3100, "runMs": 0, "voice": "guide", "tpl": "G.open.multi", "text": "<rendered template text>" }
  ],
  "sfx": { "counts": { "read": 0, "solve": 0 }, "dropped": 0 }
}
```

The `audio` URL is resolved relative to the manifest's own URL.

Invariants. `manifest.py` (pipeline), `assertNarration` (player), and the gate all enforce these:

1. `schema === "swarmpit.narration/1"`.
2. `runId` matches `^[A-Za-z0-9._-]{1,80}$`.
3. `audio` matches `^\.\./\.\./audio/narration/<runId>\.[0-9a-f]{8}\.mp3$`.
4. `timeMap`:
   - has 2 to 600 points, and each point is a pair of non-negative integers;
   - starts at `[0, 0]` and ends at `[durationMs, runEndMs]`;
   - `audioMs` is strictly increasing and `runMs` is non-decreasing;
   - every segment slope (Δrun/Δaudio) is in [0, 8].
5. `cues`:
   - sorted by `audioMs` and non-overlapping (`cue[i].endMs <= cue[i+1].audioMs`);
   - `0 <= audioMs < endMs <= durationMs`;
   - `|a2r(audioMs) - runMs| <= 1`;
   - `voice ∈ {guide, expert}`;
   - `tpl` is a known template id;
   - `text` is 1 to 240 characters and **fullmatches the regex of template `tpl`** (§3.6);
   - at most 80 cues.
6. `sfx.counts` keys are library ids, and the values are non-negative integers.
7. No other keys exist at any level. Readers must ignore unknown keys; writers must not emit them.

### 2.2 Narration index `public/data/narration/index.json`

```json
{ "schema": "swarmpit.narration-index/1",
  "runs": { "<run-id>": { "hash": "<runHash>", "manifest": "<run-id>.json", "durationMs": 0 } } }
```

`build` merges into this file. Entries for runs no longer in `data/index.json` are removed.

The player fetches this index once at boot, so runs without narration cost no 404. A run is **narratable** only if all three hold:

- It has an entry in this index.
- `entry.hash === card.hash`, so a re-exported run silently loses narration until it is rebuilt.
- The manifest passes `assertNarration`.

## 3. Script writing (pipeline, `script.py` + `vocab.py`)

### 3.1 Safety boundary: `SafeRun`

`bundle.py` is the only code that opens a core bundle or the index. It parses with `json.load` and **never prints anything it read**. It returns a frozen `SafeRun` that contains only the following:

| SafeRun field | Source | Rule |
|---|---|---|
| `runId`, `hash` | index card `id`, `hash` | Used for file names and the manifest only, **never in cue text**. |
| `iface`, `pop` | `run.iface`, `run.pop` | Must be in `IFACE` / `POP`; otherwise abort. |
| `nAgents`, `rep`, `tEnd` | `run.*` | Integers. |
| `modelSpoken` | `run.model` | Looked up in `voices.json.models`; otherwise `None`. |
| `caveats` | **index card** `caveats` (codes) | Filtered to the `CaveatCode` enum. `run.caveats[].text` is never read. |
| `events` | `events.t,d,k,a,f,s,c` | Integer arrays. `l` and `x` are never read. |
| `chapters` | `t, k, agent, ev, p, merged` | `k` must be in `CHAPTER_KINDS`. `label` and `caption` are never read. |
| `segments` | `t0, t1, title` | `title` is mapped through an exact-match enum `{Boot, On task, Free time, Wind-down}`. Anything else becomes `None`, and the segment is skipped. |
| `agents` | `id, status, startT, endT, solveT, solveStep, stepsUsed, ended, metrics.*` | `status` and `ended` are mapped through enums (`budget`, `stalled`, `voluntary_stop`); unknown values become `None`. `names` and `model` are never read. |
| `series` | `binMs, density` | Numbers. |

`script.py`, `timemap.py`, and `sfx.py` take a `SafeRun` and never touch JSON files. A test enforces this (§8.1).

### 3.2 The broadcast: two commentators

The track is written as a two-person sports broadcast. Both voices speak only template text (§3.6); the split is
about *what* each one says:

- **`guide` = play-by-play.** The stock voice. It calls the basics plainly: the setup (model, channel, agent
  count, goal), who did what, step counts, phase changes, and the final tally. Short present-tense sentences,
  at most about 20 words.
- **`expert` = color commentator.** It reacts and explains why a moment matters: anticipation, interpretation,
  the verdict. At most 2 sentences and about 35 words. It never restates the guide's numbers except in the
  verdict, where the number is the point.

They go back and forth. The shape of every script:

1. **Intro duet** (rows 1 to 3): guide welcome with the setup, color reply, guide task setup (solvable or
   unsolvable goal), color framing of the question, then the how-to line and at most one caveat. The intro talks
   over the start of the run instead of freezing it (§3.4).
2. **Beat exchanges** at key moments (first contact, pile-in, solves, leaks, swarm onset, first post, free time,
   the free-time drift and the halfway check): the guide calls the moment; the color answers. The run's first
   exchange always opens with a guide **handoff** question (`G.ask`: "What do you make of that?", prio 1), later
   ones every other time (seeded per run, prio 2). A handoff is kept only if its answer is kept, is always followed
   directly by it (a gap of `GAP_MS`, never dead air: §3.4), and the run's last handoff is the last thing the soft
   pacing checks drop. Agent ends get short color lines too (`E.regular` / `E.casual` / `E.agentEnd`, prio 3).
3. **Clusters:** exchanges whose calls land within `CLUSTER_MS = 8000` of each other (at most `MAX_VOICE_RUN`
   calls per cluster) get **one** color answer, for the most important beat (leak > first solve = swarm onset >
   first contact > free time = first post > pile-in = halfway check). It plays after the cluster's last call,
   follows the cluster's most important call (with its priority, so prio 0 whenever any call is); the other
   answers are removed, and if the kept answer had no handoff, one of theirs moves to it.
4. **Outro wrap-up** (row 14, anchored at `tEnd`, run held): guide tally, color verdict, guide sign-off, color
   last word.

**Variety.** A template family `<base>` can have variants `<base>.b`, `<base>_b`, ... (same voice; each slot name
has one type across the family). Each family rotates through its variants starting at an offset derived from
`sha256(runId + base)`, so a run always gets the same script, different runs sound different, and repeated beats in
one run do not repeat the same sentence. The handoff schedule is seeded the same way. Hard rule: no sentence is
spoken twice in one broadcast; a droppable cue whose text already appeared is removed (with its follow-ups).

**Facts.** Every number and enum comes from `SafeRun`. Shares are spoken with the `pct10` slot (to the nearest ten
percent, "about 40 percent", with exact 0/100 and "under 10" / "over 90" at the edges), so the wording never claims
more precision than it has. The track does not state run times; the player shows the clock.

**Goal.** The unsolvable goal is detected from SafeRun enums only (`run.goal == "unsolvable"` or the `unsolvable`
caveat code). It switches the setup, framing, halfway check and tally to their `stuck` forms, which never mention or
expect a solve. A model without an entry in `voices.json.models` is never spoken: the intro uses the model-less
form.

### 3.3 Cue selection

Each candidate cue is `(anchor runMs, row, sub, voice, tplId, args, priority, pair, needs)`. Priority 0 is never
dropped; priority 3 is dropped first. `pair` names the cue this one follows up on (dropped with it); `needs` names
the answer a handoff question needs (dropped without it). Candidates, by row:

| Row | Trigger | Guide (play-by-play) | Expert (color) | Prio | Anchor |
|---|---|---|---|---|---|
| 1 | Opening | `G.open.{solo,multi}[M]` (count, model if known, iface) | `E.open` | 0 | 0 (running intro) |
| 2 | Task setup | `G.setup.{multi,solo,stuck,stuckSolo}` | `E.ask.<same>` (the stickiness question, or the unsolvable framing) | 0 | 0 (running intro) |
| 3 | How-to, caveat | `G.howto` | `E.caveat.<code>` (max 1: irc_inbound_broken > synthetic > heuristic_edges > final_missing > old_harness > tagged), a follow-up of the how-to line so the intro never has two color lines in a row | 2 (1 with a caveat) / 1 | 0 (running intro) |
| 4 | Phase start | `G.seg.onTask` (only if it starts after `INTRO_SPAN_MS = 30 s`), `G.seg.free`, `G.seg.wind`. In a multi-agent run where some agent has not solved within 1 s of the free segment's start (it starts with the first finisher), `G.seg.freeFirst` instead, which never says the task phase is over | `E.onTask` (prio 3) or `E.onTask.stuck`; `E.free` (or `E.freeFirst`) exchange unless free time starts within 5 s of the first solve | 1, free 0 (1 near the first solve) | `segment.t0` |
| 5 | Chapter `firstContact` | `G.firstContact` (agent) | `E.firstContact` exchange (first only) | 0 / 1 | `chapter.t` |
| 6 | Chapter `contact` | `G.contact` / `G.contactN` (merged count) | `E.pileIn` exchange on the first merged contact after first contact | 2 | `chapter.t` |
| 7 | Chapter `solve` | `G.solve` (agent, solveStep) / `G.solve.bare`; a chapter the exporter merged from several solves (`merged > 1`, solves < 1.5 s apart) is `G.solveN` (count), and every agent in it counts as solved | first: `E.firstSolve` (`E.firstSolveN` for a merged one) exchange; later: `E.solveMore`, or `E.solveAll` (count) when the last agent solves | 0 first, then 1 / expert 1, 2 | `chapter.t` |
| 8 | Chapter `leak` | `G.leak`, later `G.leakMore` | `E.leak` exchange (first only) | 0, later 1 / 1 | `chapter.t` |
| 9 | Chapter `onset` | `G.onset` | `E.onset` exchange | 0 / 1 | `chapter.t` |
| 10 | Chapters `post` (p1; skipped when the same agent leaks at that time), `upvote` (p≤2), `dm`, `persistence`, `crash` | `G.firstPost` (only for the run's first visible post) then `G.post`, `G.upvote`, `G.dm`, `G.persist`, `G.crash` | `E.firstPost` exchange, `E.crash` | 2 (first post, crash 1) / 2 | `chapter.t` |
| 11a | Free-time drift at `free.t0 + max(8 s, 40% of segment)`: share of free-phase steps (`f & FREE`) that are board steps, if at least 3 | `G.drift` (pct10) | `E.driftHigh` (≥ 30%) / `E.driftLow` exchange | 1 | computed |
| 11b | Halfway check at `onTask.t0 + (tEnd − onTask.t0) / 2`, when nothing is solved and free time has not started by then (runs ≥ 30 s) | unsolvable: `G.stuck`, then `G.stuckShare` (pct10); otherwise `G.noSolve[.solo]` | unsolvable: `E.stuckHigh` / `E.stuckLow` exchange; otherwise `E.noSolve` exchange | 1 / 1 (noSolve 2) | computed |
| 12 | Agent `end` event | `G.agentShare` (agent, pct10 of steps after first contact) or `G.agentEnd` (agent, end reason; never touched the board) | `E.regular` (share ≥ 50%), `E.casual` (share < 15%), `E.agentEnd` (never touched the board), prio 3 | 2 | event t |
| 13 | Activity summary: each 20 s run window without a cue (after the intro span) with ≥ 6 visible events, or no board events | `G.busy` (top family) / `G.quiet` | `E.quiet` (quiet stretches alternate voices; at most `QUIET_MAX = 2` per run, never two windows in a row) | 3 | window end |
| 14 | Outro (run held at `tEnd`) | `G.tally.{multi,solo,stuck,stuckSolo}` (found / solved / leaked counts out of N; the `stuck` forms omit solves), then `G.signoff` | `E.close.<verdict>[Solo]` (pct10), then `E.signoff` | 0 | `tEnd` |

Additional rules:

- Cues on the same anchor play in (row, sub) order: the guide call (sub 0), its handoff (sub 1), the color answer
  (sub 2).
- Hidden-by-default event kinds (`think`, `idle`, `proto`, `connect`, `disconnect`) and events flagged `BACKGROUND`
  never produce cues.
- Verdict rule: let `s` be the mean `metrics.shareAfterContact` over discovered agents. `s ≥ 0.4` gives `sticky`,
  `s < 0.15` gives `notSticky`, anything else gives `mixed`. If no agent discovered the board, use `noContact`. The
  `Solo` forms speak about the one agent.

### 3.4 Pacing

The constants live in `script.py`:

- `GAP_MS = 350`: the audio silence between consecutive cues. Voices never overlap because the voice bus is serial
  by construction; a test checks `cue[i].endMs + GAP_MS <= cue[i+1].audioMs` for every run.
- `MIN_ANCHOR_SPACING_MS = 3000` between standalone cues of priority ≥ 2. A later standalone cue that is too close
  is dropped. Follow-ups (`pair` set: handoffs and answers) belong to their beat and are exempt.
- `MAX_CUES_PER_AUDIO_MIN = 12` beat cues over any 60 s window of audio. Broadcast lines are short, so twelve is a
  brisk but natural exchange rate. The fixed intro duet (rows 1 to 3) is not counted: it is one scripted
  conversation, and counting it made the rate check drop the how-to and caveat lines in most short runs.
- `MAX_VOICE_RUN = 3`: at most three consecutive cues in one voice. A longer run is a violation (`W_PACE_VOICE`);
  droppable cues in it go first. Clusters are capped at three calls for the same reason.
- `MAX_HOLD_MS = 9000`: the longest mid-run freeze. A key-moment exchange (call with its SFX lead-in plus a color
  answer) fits, like a replay pause; anything longer is a violation. A freeze that holds a handoff question may
  run `HANDOFF_HOLD_MS = 2500` longer. Holds at `runMs = 0`, inside the intro duet, and at `runMs = tEnd` (outro)
  are exempt.
- **Running intro:** the first intro line starts at run 0; each later intro line moves onto the running clock at
  the moment the previous one ends (run at 1x), as long as it ends before the next beat. A line that does not fit
  stays at the previous line's anchor (a hold), so play order never changes. This keeps the intro duet from adding
  a frozen half-minute to every run, which matters for the size budget (§6).
- Audio length target: the **paced** length (`durationMs` minus the exempt holds) divided by `tEnd` is at most
  **1.4**. Below 1.0 is fine when fast-forward makes it so.
- **Expert slide:** before the drop loop, each non-closing expert line may be delayed by up to `SLIDE_MS = 12000`
  run ms to the moment the previous line's audio ends, if it then fits before the **next cue in play order** (and
  before `tEnd`) while the run keeps playing at 1x. A line that finds no room stays at its anchor. A slide never
  passes a cue, even one at the same anchor, so the order (an intro reply after its line, an answer after its
  question) never changes. The previous line's audio end is measured from where that line is actually spoken: a
  line that stays at anchor `A` is spoken from run time `A` (the run waits there for earlier audio), so it frees
  the audio at `A + d + GAP_MS`. Counting the wait as well would slide the next line too far and leave dead air
  after a handoff question.
- **Script anchors:** the cached script stores each cue's *candidate* anchor (before the running intro and the
  slides). The mix re-runs the whole loop with measured clip durations, so the intro relay and the slides are
  re-timed from real lengths instead of re-sliding lines that estimated durations already moved.
- A constraint that only priority-0 or closing cues violate cannot be fixed by dropping. It is reported as a
  warning code (`W_PACE_HOLD`, `W_PACE_RATE`, `W_PACE_RATIO`, `W_PACE_VOICE`) in the `script` output and the `mix`
  log line, and the build continues.

Selection loop:

1. Spacing prepass, orphan drop, running intro, expert slide.
2. Build the map from estimated durations: `len(text) / charsPerSec * 1000 + 150`.
3. While any constraint is violated, drop the lowest-priority cue among the first violation's droppable cues. Break
   ties by preferring anything but a handoff question, then the one causing the largest hold, then the latest. The
   run's only remaining handoff is not droppable by `W_PACE_HOLD`, `W_PACE_RATE` or `W_PACE_RATIO` (they become
   warnings if nothing else is left). Drop orphans in both directions (follow-ups of a
   dropped cue; handoffs whose answer was dropped) and rebuild the map.

After TTS, the loop runs again with real durations. It can only drop cues, so it never needs new synthesis.

Measured on the committed manifests of the 335 visible runs (measured durations): median 18 cues per run (10 to
37), 78% of adjacent cue pairs switch voice, no run has more than 3 consecutive cues in one voice, 309 runs with at
least one handoff (every handoff answered after exactly `GAP_MS`), no sentence repeated within a run, about 643 min
of audio in total (486 min of run time, 110.5 MiB of mp3), 584 distinct lines totalling about 34,600 characters. Because clips are cached by
`(voice, text, settings)`, that distinct-line total, not the per-run sum, is what synthesis costs. Templates avoid
high-cardinality slots (run times, exact percentages) for this reason.

### 3.5 Time map algorithm (`timemap.py`; the player only consumes the result)

Input: the run end `tEnd`, the selected cues sorted by (anchor, order), each with a duration `D`, and the visible event times (for quiet detection). The walk keeps `a = 0, r = 0` and `points = [[0,0]]`.

For each cue *i* with anchor `R_i` (clamped so that `R_i >= r`):

1. **Approach (run moves, no voice):** `L = R_i - r`.
   - Split `[r, R_i]` at every visible event. Each piece longer than `QUIET_MS (4000)` is a quiet stretch: play its first and last `QUIET_EDGE_MS (400)` at 1x and the middle in `max(ceil(middle / 8), min(middle, 1500))` ms of audio, so the slope never exceeds the contract cap of 8. A 30 s quiet stretch costs at most about 4.5 s of audio.
   - Every other piece plays at 1x.
   - Otherwise play 1x.
   - Append the points.
2. **Speak:** the cue occupies audio `[a, a + D]`. Let `R_next` be the next cue's anchor (or `tEnd`). During the cue the run advances `Δr = min(D, max(0, R_next - r - LEAD_MS))` with `LEAD_MS = 300`. This means:
   - the run plays at 1x under the voice when there is room;
   - it slows when the next beat is close;
   - it **holds** (`Δr = 0`) when the next beat is already due.
   - Append `[a + D, r + Δr]`.
3. **Gap:** `GAP_MS` of audio, with the run advancing `min(GAP_MS, max(0, R_next - r))`.
4. **Tail:** after the last pre-end cue, apply rule 1 from `r` to `tEnd`. The closing cues are then spoken with the run held at `tEnd`. End with `TAIL_MS = 2200` ms of tail so the outro SFX (at most 2000 ms, placed 50 ms after the last cue) plays out instead of being cut.
5. **Normalization:** merge collinear points, round to integer ms, and drop zero-length segments. Then set the final point's `audioMs` to the **measured** mp3 duration, which absorbs encoder padding of 50 ms or less.

The guarantee: every cue starts exactly when the run reaches its anchor, or while the run is held at it.

Map functions, shared with the player:

- `a2r(a)`: binary search on `audioMs`, then linear interpolation. It clamps outside `[0, durationMs]`.
- `r2a(r)`: the **smallest** `a` such that `a2r(a) >= r`. Within a hold this is the start of the hold, so seeking onto a beat replays its cue from the beginning.
- Round trip: `a2r(r2a(r)) == r` within 1 ms, for all r in [0, tEnd].

### 3.6 Template vocabulary and how it is enforced

Cue text is assembled **only** from author-written template strings, formatted numbers, and enum lookups. Enforcement has three layers, each tested:

1. **Projection.** `SafeRun` (§3.1) never contains free text, so there is nothing else to interpolate.
2. **Typed render.** `vocab.TEMPLATES: dict[str, Template]`, where each template is `(voice, pattern)` with typed slots, for example `"{agent:agent} solves the task, in {steps:int} steps."`. `render(tpl_id, **args)` checks each argument against its slot type and raises `VocabError` on any mismatch:

   | Slot | Python type | Rendered as | Regex used by the validator |
   |---|---|---|---|
   | `int` | int, 0..1,000,000 | `"12"` | `\d{1,7}` |
   | `count` | int, 0..999 | "one" for 1, else digits | `one\|\d{1,3}` |
   | `agent` | int, 0..999 | `"Agent 3"` | `Agent \d{1,3}` |
   | `sec` | int ms | `"12 seconds"`, `"1 minute 5 seconds"` | fixed alternation |
   | `pct` | float 0..1 | `"40 percent"` | `\d{1,3} percent` |
   | `pct10` | float 0..1 | `"0 percent"` / `"under 10 percent"` / `"about 40 percent"` / `"over 90 percent"` / `"100 percent"` (exact 0 and 1 only; otherwise nearest ten, edges below 5% / from 95%) | fixed alternation |
   | `enum:<TABLE>` | key in `vocab.ENUMS[TABLE]` | the author-written value | alternation of the table values (escaped) |

   The enum tables (all values author-written):
   - `IFACE` (forum, wiki, cache, irc → spoken phrase)
   - `NOUN` (the board noun per iface)
   - `FAMILY` (read → "board reads", write → "posts", social → "social actions", …)
   - `ENDREASON`
   - `FOUND`, `SOLVED`, `LEAKED` (yes/no phrases for the solo tally)
   - `MODEL` (built from `voices.json.models`; every model in the index has an entry: Qwen 3.8 27B, Mistral Small
     3.2 24B, GLM 5.3 Flash, Gemma 4 31B, and the synthetic test run. A test fails if a run's model is missing.)

   Template ids are `G.*` (guide) or `E.*` (expert), with at most three dot segments after the letter (the player's
   `TPL_ID` check). Variant families (`<base>.b`, `<base>_b`, ...) share a voice; `vocab.variants(base)` lists them
   and `script.py` rotates through them (§3.2).

   Strings of any other type, such as a str passed to an `int` slot, are rejected.
3. **Validation.** Each template compiles to a full-match regex: the literal parts are `re.escape`d and each slot becomes the regex above. `validate_cue(tpl, text)` requires `TEMPLATE_RE[tpl].fullmatch(text)`. A second check tokenizes the text with `[A-Za-z]+|\d+|[^\sA-Za-z\d]` and requires every word token to be in `LEXICON`, which is the set of words from all template literals and enum values. It runs in four places:
   - inside `render`, as defense in depth;
   - in `manifest.py` before writing;
   - in `check_public.py` over every committed manifest (§7);
   - in the tests.

   Validation errors report only the `tpl` id, the cue index, and a fixed error code (`E_TPL_UNKNOWN`, `E_TPL_NOMATCH`, `E_LEXICON`). They never include the text.

Templates live only in `vocab.py`. Adding a template is a code change reviewed like any other, and its text must not quote or paraphrase run content.

Numbers spoken as seconds are rounded to whole seconds of run time. The guide never states timing to better than a second.

## 4. Sound effects (`sfx.py`)

### 4.1 Library generation (`sfx` subcommand, run once)

For each `library` id whose prompt, seconds, or model changed (checked via `promptSha = sha256(model|prompt|seconds|influence)` against `sfx/library.json`), or when `--force <id>` is given:

1. Call `POST /v1/sound-generation` with `{text, duration_seconds, prompt_influence, loop:false, model_id}`. The response is raw mp3 bytes.
2. Save the raw output to `.narration-cache/sfx-raw/`.
3. Post-process with ffmpeg:
   - mono, 44.1 kHz;
   - `silenceremove=start_periods=1:start_threshold=-50dB`;
   - trim to `maxMs`;
   - `afade=t=in:d=0.005`, then `afade=t=out` over the last 40 ms;
   - peak-normalize to −6 dBFS (measure with `volumedetect`, then apply `volume`).
4. Encode to `sfx/<id>.mp3` (128k, `-map_metadata -1`) and update `library.json`.

The library is committed so run builds are reproducible without the API. Each clip is about 2 to 30 KB.

### 4.2 Event → SFX mapping

Candidates come from the **run** timeline and are placed at `audioMs = r2a(t)`.

| Source | SFX id | Per-id min spacing | Prio |
|---|---|---|---|
| Segment start (except the first) | `segment` | — | 1 |
| Chapter `firstContact`, `onset` | `contact` | — | 1 |
| Chapter `solve`, or the first `SUBMIT_OK` event per agent | `solve` | 1000 ms | 1 |
| Chapter `leak` | `leak` | — | 1 |
| Event `crash` | `crash` | 1000 ms | 1 |
| Event `start` (agent boot) | `boot` | 600 ms | 2 |
| Event `join` (irc) | `join` | 600 ms | 2 |
| Event `post`; `delete` (−3 dB) | `post` | 450 ms | 3 |
| Event `upvote`, `dm`, or any event with `LIVE_LIVE` | `social` | 450 ms | 3 |
| Event `end` (agent) | `end` | 800 ms | 3 |
| Event `read`, `list`, `doc` (not `miss`) | `read` | 300 ms | 4 |
| `tEnd` (run end) | `outro` | — | 1 |

Exclusions:

- No SFX for hidden-by-default kinds, `BACKGROUND` events, `cmd`, `board`, `think`, `retry`, `boardStart`, `submit` (other than the solve rule), or `miss`.
- A chapter-driven SFX consumes its causing event `chapter.ev`, so that event does not also produce its plain SFX.

### 4.3 Thinning

Thinning is deterministic and runs on the audio timeline:

1. Sort candidates by `(audioMs, prio)`.
2. **Per-id spacing:** drop a candidate if the last kept SFX of the same id is closer than its min spacing. The kept instance tracks `cluster += 1`. At render it gets `+1 dB` per doubling of `cluster`, capped at `+3 dB`.
3. **Global density:** process the survivors in `(prio, audioMs)` order. Keep one only if:
   - no half-open 1000 ms audio window would then hold more than **3** kept SFX, and
   - no kept SFX is within **120 ms**.
4. **Speech guard:** inside any cue interval `[audioMs − 150, endMs + 100]`:
   - drop prio-4 (`read`) entirely;
   - drop prio-3 if one is already kept in that cue (at most one per cue);
   - prio 1–2 remain, ducked (§6).
5. During a run hold, all events at the held run time map to the same audioMs. Steps 2–3 thin them like any burst.

Output: placements `(audioMs, id, gainDb)` plus `counts[id]` and `dropped`. Only these numbers go into the manifest.

**Key-effect lead-in.** A prio-1 effect (segment, contact, solve, leak, crash) usually coincides with the cue that
announces it, and would be ducked from its first ~15 ms. `sfx.key_leads` gives each such run time a lead-in of
`clamp(maxMs, 300, 700)` ms; the first cue anchored there gets a run-held audio gap of that length before the voice
(`TM.CueSlot.lead`), and the effect is placed at the start of that gap, so its attack plays in the clear and only its
tail is ducked. A test asserts no prio-1 effect starts within `SPEECH_PRE_MS` of a cue start.

### 4.4 Levels

These values are relative to the voice bus before loudness normalization.

- Library clips sit at a −6 dBFS peak. The bus gain is `busGainDb = −20`, and per-id `gainDb` is −8..0. The effective level is about **−26 to −20 dB under voice peaks**.
- Under voice, a sidechain compressor keyed by the voice bus ducks the SFX bus by about a further 6–10 dB.
- The result: SFX are audible in silences and nearly inaudible under speech.

## 5. ElevenLabs client (`eleven.py`, `tts.py`)

### 5.1 Key handling

- `config.api_key()` reads `ELEVENLABS_API_KEY` from `os.environ`. Failing that, it reads the nearest `.env` walking up from `site/` to the git root. The `.env` parser handles `KEY=value` with optional quotes and nothing else.
- The key is passed only as the `xi-api-key` request header.
- It is **never** logged, printed, included in exception messages, written to cache metadata, or put in a URL.
- `eleven.py` catches `urllib.error.HTTPError` and raises `ElevenError(status, code)`:
  - `code` is `detail.status` when that is a short `[a-z_]+` token, otherwise `"unknown"`;
  - the response body is discarded after that one extraction;
  - for 422, `code` is `"validation"`.
- The API key lacks `user_read`, so no `/v1/user*` calls are made.

### 5.2 TTS

- Request: `POST {endpoint}?output_format=mp3_44100_128` with the body `{text, model_id, voice_settings, seed}`.
- Keep the response's `audio_base64` (decoded) and `alignment` (character timings of our own template text). Ignore `normalized_alignment` and `quality_check`.
- **Cache key:** `sha256(canonical_json({v:1, voiceId, model, settings, seed, outputFormat, text}))`. On a cache hit there is no network call.
- Cache meta: `durationMs` (measured from the trimmed PCM, not from alignment), `characterCost` (from the response header), `requestId`, `model`, and `bytes`.
- Pipeline:
  - Concurrency is `voices.json.tts.concurrency` (2) via `concurrent.futures.ThreadPoolExecutor`.
  - Retry 429 (`too_many_concurrent_requests`, `system_busy`) and 5xx with backoff 1, 2, 4, 8, 16 s (plus jitter), up to 5 attempts.
  - 401, `quota_exceeded`, and 422 abort the whole build with `status` and `code`.
  - If the primary model fails with 422 or `model_not_found`, the cue is retried once with `fallbackModel`. The fallback is part of the cache key.
- Decode to the PCM cache: `ffmpeg -i in.mp3 -ac 1 -ar 44100 -af silenceremove=start_periods=1:start_threshold=-50dB,areverse,silenceremove=start_periods=1:start_threshold=-50dB,areverse -f s16le`.
- Logging is one line per call: `tts <tpl> <voice> status=<n> bytes=<n> ms=<n> cost=<n> cached=<bool>`. It never includes text or bodies.

## 6. Mixing (`mix.py`)

1. **Voice bus** (stdlib): an `array('h')` of length `durationMs × 44.1`. Each cue's PCM is copied at `audioMs` with `voices[v].gainDb` applied, clipping to int16. Write it as `voice.wav` with the `wave` module.
2. **SFX bus** (stdlib): the same, summing each placement scaled by `10^((gainDb + clusterBump)/20)` into an `array('i')` accumulator, then clipping to int16. Write `sfx.wav`.
3. **Final** (two passes, so loudness normalization is one static gain and the SFX/voice balance is kept):
   ```
   # pass 1: duck + sum to a float WAV
   ffmpeg -y -i voice.wav -i sfx.wav -filter_complex \
     "[0:a]asplit=2[vk][vm];[1:a]volume=<busGainDb>dB[s];[s][vk]sidechaincompress=threshold=0.03:ratio=6:attack=15:release=250[sd];\
      [vm][sd]amix=inputs=2:normalize=0[out]" -map "[out]" -c:a pcm_f32le mixed.wav
   # measure: ffmpeg -i mixed.wav -af ebur128 -f null -   (only the numeric "I: <x> LUFS" summary line is parsed)
   # pass 2: gain = lufs - I (clamped to +-30 dB), then a latency-compensated peak limiter, then encode
   ffmpeg -y -i mixed.wav -af "volume=<gain>dB,alimiter=limit=<10^((TP-0.5)/20)>:attack=5:release=50:level=0:latency=1,aresample=24000" \
     -ac 1 -ar 24000 -c:a libmp3lame -b:a 24k -map_metadata -1 -id3v2_version 0 -write_xing 1 out.mp3
   ```
   - A single-pass `loudnorm` is not used: without measured values it runs as an AGC that lifts the SFX-only
     stretches relative to speech, which undoes the levels of §4.4.
   - The output goes to a temp path **outside `public/`**, because the gate rejects `*.tmp` files. It is then moved into place with `os.replace`.
   - Final level is about −16 LUFS integrated, peaks limited to about −2 dBFS.
   - **Delivery format and size.** Every run in `data/index.json` is narrated (~335 runs, ~650 min of audio in
     total), and the mp3s are committed and served by Pages, so the encode is sized for the whole set rather than one
     run: **24 kbps CBR mono, MPEG-2 Layer III at 24 kHz** (`mix.OUT_BITRATE_KBPS` / `mix.OUT_SAMPLE_RATE`; these
     constants are authoritative, `voices.json` `mix.bitrate`/`mix.sampleRate` are not used by the encode). That is
     3 KB/s, about 0.18 MB per minute: a 3-minute run is ~0.54 MB, the longest runs (~6 min of audio) ~1.1 MB, and
     the projected total for all runs is ~111 MiB (`batch --dry-run` prints `projected_mp3_bytes`). Why this rate:
     the content is two speaking voices over sparse, ducked SFX, and LAME at 24 kbps / 24 kHz keeps speech
     clearly intelligible, while 32 kbps would put the set at ~150 MB and 80 kbps (the earlier per-run
     choice) at ~390 MB. MPEG-2 frames still pass the gate's `0xFFE0` sync check and play in every browser.
   - Budgets (gate and `manifest.py`, kept equal by a test): ≤ 2 MiB per mp3, ≤ 120 MiB in total under `audio/`.
4. Read the duration of `out.mp3` with `ffprobe -v error -show_entries format=duration -of csv=p=0`.
5. Set the last time-map point to that duration and write the manifest and the narration index.

## 7. CLI (`python3 -m tools.narrate`, run from `site/`)

| Command | Does | Network |
|---|---|---|
| `script --run ID \| --featured \| --all` | Builds `SafeRun`, then cues, then an estimated map. Writes `.narration-cache/runs/<id>/script.json`. Prints `id cues=N chars_guide=N chars_expert=N est_audio_ms=N ratio=X`. | no |
| `sfx [--force ID...]` | Generates and refreshes the committed SFX library. | yes |
| `tts --run ...` | Synthesizes the uncached cues of the script. | yes |
| `mix --run ...` | Rebuilds the final map with real durations, thins SFX, mixes and encodes, then writes the manifest and index. | no |
| `build --run ID \| --featured \| --all [--dry-run]` | Runs `script`, `tts`, then `mix`, with `sfx` first if the library is missing. `--dry-run` runs `script` only and prints the total character estimate per voice. | yes, unless `--dry-run` |
| `batch [--runs all\|visible\|ID...] [--dry-run] [--max-new-chars N] [--jobs K]` | Every target run in one pass with a character budget; resumable (§7.2). | yes, unless `--dry-run` or fully cached |
| `check` | Validates all committed manifests (the same function the gate uses). | no |

- The exit code is non-zero on any validation failure.
- Output is limited to ids, counts, sizes, durations, status codes, and error codes. **Cue text is never printed**, not even in `--dry-run`. To review wording, read the templates in `vocab.py`.
- Runs are selected with `--featured`, which uses `index.featured`, or `--all`, which covers every non-hidden card. Hidden cards need an explicit `--run`.

### 7.1 Invocation and implementation notes (Track A, as built)

Run from `site/`:

```
python3 -m tools.narrate build --featured --dry-run      # no network: script only, prints counts
python3 -m tools.narrate sfx                             # once: generate + commit tools/narrate/sfx/
python3 -m tools.narrate build --featured                # script -> tts -> mix -> public/ artifacts
python3 -m tools.narrate check                           # validate committed manifests/index/mp3s
npm run narrate -- build --all                           # same CLI through npm
python3 -m unittest tools/test_narrate.py                # Python tests (no network)
```

**Repo size.** The mp3s are committed (about 111 MiB for all ~335 runs at 24 kbps, §6). Their names carry a content hash, so an
unchanged rebuild is byte-identical (bitexact encode, no metadata) and adds nothing, but any change that alters the
audio (templates, SFX library, mix, pacing) adds a new blob per changed run that stays in git history. Treat
`batch` as a deliberate, infrequent step: iterate with `build --run ID` or `script`/`batch --dry-run`, and commit
regenerated audio in batches. The gate enforces ≤ 2 MiB per mp3 and ≤ 120 MiB under `audio/` even with `--no-budget`.

### 7.2 `batch`: every run, one character budget, resumable

```
python3 -m tools.narrate batch --dry-run                       # counts only, no network
python3 -m tools.narrate batch --max-new-chars 100000 --jobs 4 # synthesize + mix + index + prune
python3 -m tools.narrate batch --runs all                      # include hidden cards
python3 -m tools.narrate batch --runs ID [ID ...]              # explicit runs
```

- **Targets:** `--runs visible` (default, every non-hidden card), `all` (every card), or explicit ids.
- **Plan (always first):** builds and caches every target script, then prints one numbers-only line: `runs`,
  `cues`, `unique_clips` (distinct voice + text), `cached` / `uncached`, `new_chars` (the exact characters that
  would be sent to ElevenLabs: the sum over uncached unique clips), `est_audio_ms` and `projected_mp3_bytes`.
  `--dry-run` stops here.
- **Budget:** `--max-new-chars N` (default 100000). If `new_chars > N` the command refuses before any request
  (exit 3). During synthesis (run by run, in index order, clips deduplicated across runs) it stops cleanly before a
  run whose clips would take the running total past `N`; the runs before it are still mixed, and the next
  invocation resumes from the cache (exit 1 to flag the partial result).
- **Mix:** `--jobs K` sets the TTS concurrency and the number of mixing processes (default: `voices.json`
  concurrency for TTS, CPU count for mixing). A run is skipped when its recorded build fingerprint (pipeline source,
  `voices.json`, SFX library, encode settings, script, and the TTS cache key of every cue, kept in
  `.narration-cache/runs/<id>/built.json`) still matches and its manifest, index entry and mp3 are in place.
- **Writes:** mp3s, manifests and the narration index. Superseded mp3s are deleted per run, then manifests of runs
  that left `data/index.json` and any mp3 no manifest references are pruned.
- **Output:** one `batch done` line with counts and byte totals only (`mixed`, `skipped`, `spent_chars`,
  `stopped`, `pruned_*`, `mp3_bytes`, `max_mp3_bytes`, ...). Per-clip TTS lines are suppressed; cue text and the
  key are never printed.

Any unexpected exception prints only `error E_INTERNAL <ExceptionType>`; set `NARRATE_DEBUG=1` to get a traceback (it may echo values, so use it only on synthetic data).

Choices made while implementing the broadcast (template ids, rotation, handoffs, intro/outro shape) are documented in §3.2–3.3; all template text lives in `vocab.py`. Remaining implementation notes:

- **Free-time drift:** needs at least 3 free-phase steps up to the anchor.
- **Unsolvable goal:** `bundle.py` keeps the index card's `unsolvable` caveat code; `script.is_unsolvable()` then uses the stuck framing and never mentions a solve.
- **Outro SFX:** placed 50 ms after the last cue ends, so it lands after the closing analysis rather than under it.
- **`json` users:** `eleven.py` also uses `json`, for request and response bodies only; it opens no files. The script cache is read and written through `config.write_cache_json` / `config.read_cache_json`. The isolation test's source scan reflects both.

## 8. Tests

### 8.1 Python (`tools/test_narrate.py`, unittest, no network)

| Area | Tests |
|---|---|
| vocab | Every template renders with sample args and passes `validate_cue`. A wrong slot type raises an error. A foreign word inserted into a rendered string fails with `E_LEXICON` or `E_TPL_NOMATCH`. An unknown enum key raises. Error messages contain no text. |
| isolation | Build a **synthetic** in-memory core with neutral placeholder sentinels (e.g. `SENTINEL_LABEL_1`) in every free-text field: node labels, chapter label and caption, `strings`, agent names, `boardName`, caveat text, card blurb, an unknown model id, and an unknown segment title. Run `script` and `mix --plan-only` (no audio). Assert that no sentinel appears in the script JSON or the manifest JSON. Also assert by source scan that `json` and `open(` occur only in `bundle.py`, `config.py`, `manifest.py`, `tts.py` (cache meta), and `sfx.py` (library lock). |
| real data | If `public/data/index.json` exists: for each listed run, `script` succeeds, every cue validates, and the map invariants hold. Assertions are on counts only, and nothing is printed. |
| timemap | Invariants from §2.1 #4. A cue starts exactly at `r2a(anchor)`. A hold appears when cues are dense. A quiet 30 s stretch costs ≤ 4.5 s of audio. Round trip `a2r(r2a(r))`. The ratio stays ≤ 1.4 after the drop loop. Expert lines slide into room without reordering past the next cue. Priority-0 cues survive. |
| sfx | A synthetic burst of 50 `read` events in 1 s gives ≤ 3 kept SFX and per-id spacing holds. `read` is suppressed inside cues. A chapter consumes its event. Hidden kinds yield nothing. The output is deterministic. |
| eleven | Mocked opener: a 429 then 200 succeeds after a retry. 401 gives `ElevenError(401, …)`. A fake key string never appears in the captured log, the exception `str()`, or the cache meta. |
| mix | Skipped without ffmpeg. Generate sine inputs with `ffmpeg -f lavfi -i sine=f=440:d=1.5` (voice) and `sine=f=1500:d=0.1` (sfx) into a temp dir. Mix 3 cues and 10 SFX. Check that the output duration is within ±60 ms of expected, that `ebur128` integrated loudness is −16 ± 1.5, and that the output has no ID3 text frames. |
| commentary | Every template renders at slot extremes (pct10, count, agent, every enum key) and stays inside the word budget and the player's `TPL_ID`. Rotation is deterministic per run id and varies across ids. Synthetic scenarios (solo/multi; solved, unsolved, unsolvable; dense 13–16 s runs) × model × caveat each give a valid broadcast: valid time map, no overlapping cues, ≤ 3 consecutive cues per voice, intro `G.open` → `E.open`, outro guide tally / expert verdict / guide sign-off / expert sign-off, every handoff immediately followed by its answer, no priority-0 cue dropped. Unsolvable runs never mention a solve. |
| batch | Run selection, dry-run exact new-character count with no network and no output, refusal over the cap, and an ffmpeg end-to-end build with a fake TTS client (resume, prune, stop on overspend). |
| manifest/gate | A non-monotonic map, overlapping cues, a bad `tpl`, a foreign word, an orphan mp3, and a missing mp3 each fail with a fixed code. |

### 8.2 Node (`tools/test/narration.test.mjs`, imports `public/js/...`)

| Area | Tests |
|---|---|
| timemap | `a2r` and `r2a` interpolation and clamping. `r2a` returns the hold start. Round trip. `cueAt(cues, a)` gives the index or −1, including at boundaries. |
| assertNarration | Accepts a minimal synthetic manifest. Rejects a bad schema, a non-monotonic map, overlapping cues, a bad voice, and an `audio` URL outside the allowed pattern. |
| clock driver | Create a clock with a fake model and call `setDriver({t: () => x, done: () => d})`. `advance` sets `t = x`, ignores rate and skip-quiet, emits `chapter` when one is crossed, and emits `ended` once `done()` is true. Clearing the driver restores normal advance. |
| keys | `keyAction('n')` returns `'narration'`. |
| data | If `public/data/narration/index.json` exists: every entry's manifest passes `assertNarration`, the referenced mp3 exists, and its bytes match `audioBytes`. Skipped otherwise. |

## 9. Player integration (Track B)

### 9.1 Types (`src/types.ts`, append-only)

```ts
export type NarrationVoice = 'guide' | 'expert';
export interface NarrationCue { audioMs: number; endMs: number; runMs: number; voice: NarrationVoice; tpl: string; text: string }
export interface NarrationManifest {
  schema: 'swarmpit.narration/1'; generator: string; runId: string; runHash: string; audio: string;
  audioBytes: number; durationMs: number; runEndMs: number;
  timeMap: [number, number][]; cues: NarrationCue[]; sfx: { counts: Record<string, number>; dropped: number };
}
export interface NarrationIndex { schema: 'swarmpit.narration-index/1'; runs: Record<string, { hash: string; manifest: string; durationMs: number }> }
export interface ClockDriver { t(): number; done(): boolean }
// SeekCause gains 'audio'; KeyAction gains 'narration'; TimelineOptions.onToggle union gains 'narration'.
// PlaybackClock gains: setDriver(d: ClockDriver | null): void; readonly driven: boolean; syncDriver(): void;
```

### 9.2 `src/player/timemap.ts` (pure, DOM-free)

`a2r(map, aMs)`, `r2a(map, rMs)`, `cueAt(cues, aMs)`, `assertNarration(x: unknown): NarrationManifest`, and `assertNarrationIndex(x)`. These implement exactly §2.1 and §3.5 and throw `BundleError` on violations.

### 9.3 Clock (`src/player/clock.ts`)

`setDriver(d)` stores the driver. In `advance(dt)`, when `driver && playing && !scrubbing`:

1. `nt = driver.t()`, then `setT(nt, null, dt)`.
2. Emit `chapter` for crossings.
3. Do **not** auto-end at `dur`. Instead, if `driver.done()`, set `playing = false` and emit `state` and then `ended`.

Rate and skip-quiet are ignored while a driver is set. `syncDriver()` runs the same body once, and the controller calls it on the audio `ended` event and on wake. `play()` when `driver.done()` seeks to 0 first, with cause `'api'`. `clock.ts` stays DOM-free.

### 9.4 `src/player/narration.ts` (DOM controller)

```ts
export interface NarrationController {
  readonly available: boolean;   // current run has a valid, hash-matching manifest
  readonly on: boolean;
  setRun(card: RunCard | null, signal: AbortSignal): Promise<void>; // null = unload; fetches index (cached) + manifest
  toggle(): void;                // must be called synchronously from a user gesture
  dispose(): void;
}
export function createNarration(o: { clock: Clock; captions: CaptionLayer; timeline: TimelineBar;
                                     base: URL; onAvailability(avail: boolean): void;
                                     onInteract?(): void; startAtTop?(): boolean }): NarrationController;
```

`createPlayer` creates **one** `HTMLAudioElement` (`preload="none"`) for the player's lifetime and reuses it across runs, so a once-unlocked element stays playable on Safari. No mp3 bytes are fetched until narration is first turned on.

**Turning on (`toggle` from a click or the `n` key):**
1. Set `audio.src` if it changed.
2. `audio.currentTime = r2a(clock.t) / 1000`. If run `t <= INTRO_SNAP_MS` (2000 ms), or `startAtTop()` is true (the
   player passes `() => preview.active` from `src/player/preview.ts`, i.e. turned on over the hero autoplay
   preview), seek to 0 first so the intro duet plays. The preview gate is separate from the player's `interacted`
   flag on purpose: the button's pointerdown and `act()` set `interacted` before `toggle()` runs. Autoplay starts
   the gate; user seeks (`scrub`, `key`, `marker`, `feed`, `url`), loading or picking a run, any other action,
   and turning narration on end it. A seek requested before the mp3's metadata exists (`preload="none"`; older WebKit drops it) is held
   as `pendingA`, used for `t`/`done()`/captions, and re-applied on `loadedmetadata`.
3. `clock.setDriver({t: () => a2r(audio.currentTime*1000), done: () => audio.ended || audio.currentTime*1000 >= durationMs - 20})`.
4. `clock.play()` and `audio.play()`. If the play promise rejects (`NotAllowedError`), call `clock.pause()` and keep narration on. The next user play retries.
5. Mark the user as interacted, which cancels the hero loop-once autoplay.

**Turning off:** `audio.pause()`, `clock.setDriver(null)`, and clear the cue caption. Run `t` and play state are kept, and playback continues at the user's previous rate.

**Sync rules while on:**
- **State:** on clock `state` with `playing`, call `audio.play()`; otherwise `audio.pause()`.
- **Seeking:** on clock `seek` with cause ≠ `'audio'`, set `audio.currentTime = r2a(t)/1000`. This one handler covers scrub, key, marker, feed, tour, end-card replay, and step.
- **Scrubbing:** `beginScrub` and `endScrub` already pause and restore through `state`.
- **Audio events:** on audio `ended`, call `clock.syncDriver()`, which ends the clock and shows the end card. On `waiting`/`stalled`, `t` simply holds because it derives from `currentTime`.
- **Hidden tab:** on `visibilitychange` to hidden, call `clock.pause()`.
- **Scrolled out of view:** when the stage drops below 10% visibility, the audio continues and `t` resyncs on wake via `syncDriver()`.

**Overrides while on:**
- In `act()`, `faster` and `slower` are ignored.
- The speed button gets `aria-disabled="true"` with the title "Speed is set by narration".
- The skip-quiet chip and menu item are disabled.
- `timeline.setNarrating(on)` applies these. The previous rate and skip-quiet values stay in the clock untouched.

**Captions:**
- On every clock `tick` while on, and on audio `timeupdate`, compute `i = cueAt(cues, currentTime*1000)`.
- Call `captions.setCue(i < 0 ? null : {speaker: LABEL[voice], text})` with `LABEL = {guide:'Play-by-play', expert:'Color'}` (`SPEAKER` in `narration.ts`). Each cue also carries its `voice` role token, so the caption element gets `data-voice` and the `.cap-cue` class (Color lines are styled italic with an accent border), and the live region announces `<label>: <text>`.
- A set cue replaces the chapter caption. Render it as `span.cap-spk` plus `span.cap-text`.
- Each new cue is pushed once to the `aria-live="polite"` region, without the 3 s throttle.
- Visibility follows the CC toggle (`c`). Audio and captions are independent.

**Run switching (`api.load`):**
1. Before `clock.pause()`, call `narration.setRun(null)`. This pauses the audio, removes the driver, and clears the cue.
2. After `clock.setModel`, call `narration.setRun(card, ac.signal)` under the existing stale guard.
3. If narration was on and the new run is available, stay on in armed state. `load(...).then(() => clock.play())` from the end card calls `audio.play()`; on rejection, fall back to paused.
4. If the new run is not available, narration turns off and the button is hidden (`onAvailability(false)` → `timeline.setNarrationAvailable(false)`).

**Missing or invalid manifest:** this is a silent no-op. There is one `console.debug` line with a fixed message (no data), and the button is hidden.

**Policies:**
- Narration is never enabled by autoplay, URL, or reduced motion.
- No URL flag is added.
- Save-data only defers the mp3 load until the user turns narration on, which is already the default.

### 9.5 Controls, keys, and CSS

- `ui/icons.ts`: add `headphones`.
- `ui/timeline.ts`:
  - `bNarr = btn(ICON.headphones, 'Narration (n)', 'tl-tog')` goes before `bCC`, after `bNext`, so the mobile `nth-child` rules are unaffected.
  - Add `'narration'` to `ToggleName` and `toggles`.
  - Add the methods `setNarrationAvailable(v)` (sets `hidden`) and `setNarrating(on)`.
- `player/keys.ts`: `case 'n': return 'narration'`.
- `ui/help.ts`: add the row `n  Narration (voice + sound)`.
- `player.ts` `act()`: `case 'narration': narration.toggle(); setToggle('narration', narration.on)`.
- CSS: add `.cap-spk` (small caps, accent colour, a margin) and the disabled speed style, in at most about 400 B.
  - Raise `BUDGET["css"]` in `check_public.py` to `32 * KB` in the same change, because the current CSS sits about 17 B under 30 KB.
- `tools/serve.mjs`: add `'.mp3': 'audio/mpeg'` and single-range `Range: bytes=a-b` support, answering with `206`, `Content-Range`, and `Accept-Ranges: bytes`. This makes local seeking behave like GitHub Pages.

## 10. Gate changes (`tools/check_public.py`)

1. **Binary allowlist:** `BINARY_EXT = {".mp3"}`. Any other file under `public/` whose extension is in neither `TEXT_EXT` nor `BINARY_EXT` is a problem. Existing extensionless files such as `CNAME` and `.nojekyll` stay covered by `""` in `TEXT_EXT`.
2. **mp3 checks** under `audio/narration/`:
   - The name matches `<id>.[0-9a-f]{8}.mp3`, and `sha256(bytes)[:8]` equals the name hash.
   - The file starts with an MPEG frame sync (`0xFFE0` mask). An `ID3` tag fails, because metadata must be stripped.
   - A case-insensitive scan of the raw bytes finds none of `LITERALS` or the denylist words.
   - Budget: ≤ 2 MiB per file and ≤ 120 MiB in total under `audio/` (sized in §6).
3. **Narration JSON:**
   - Each `data/narration/*.json` is ≤ 64 KB and passes the normal redact and literal scan.
   - `index.json` validates as `swarmpit.narration-index/1`.
   - Each manifest validates via `tools.narrate.manifest.validate`, which includes the template full-match of every cue (§3.6). The gate adds `site/tools` to `sys.path`; the import is stdlib only.
   - Every manifest is referenced by the index, and every mp3 is referenced by exactly one manifest.
   - `runId` exists in `data/index.json`.
   - A `runHash` that differs from the card hash is a **warning** (printed, exit 0): the player ignores stale narration, so deploys are not blocked.
4. **Error output** uses fixed codes and paths only, the same as today.
5. Extend `selftest()` with one good manifest, one with a foreign-word cue, and one with a non-monotonic map.

## 11. Work split and order

- **Track A:**
  1. `vocab.py`, `bundle.py`, `script.py`, `timemap.py` and their tests. This needs no network.
  2. `eleven.py` and `sfx` library generation, then commit `sfx/`.
  3. `tts.py`, `mix.py` and the mix test.
  4. The gate changes.
  5. `build --featured`, review by ear, then `build --all`. Commit the mp3s, the manifests, and the index.
- **Track B:**
  1. Types, `timemap.ts`, and the clock driver with their tests.
  2. `narration.ts` and the UI wiring.
  3. `serve.mjs`.
  4. Test against a hand-written synthetic manifest plus a sine-tone mp3 produced locally by ffmpeg. Do not commit that pair under `public/`.
- **Done when:**
  - `npm run build && npm test` and `python3 -m unittest discover -s tools -p 'test_*.py'` pass;
  - `python3 tools/check_public.py public --no-budget` passes;
  - toggling narration on the featured run plays voice with subtle SFX, captions track speakers, and seeking, pausing, run switching, and the end card stay in sync.
