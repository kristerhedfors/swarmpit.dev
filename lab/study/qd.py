#!/usr/bin/env python3
"""MAP-Elites (quality-diversity) driver for the instigation landscape.

Where hillclimb.py climbs ONE scalar (instigation_score), this driver fills a grid of
behaviour cells -- primary trigger code x channel x seed-length band (landscape.py) --
PER AGENT MODEL, keeping the best ("elite") seed per (model, cell), so the study maps
*which kinds* of board seeds instigate *which kinds* of first writes in *which* models.

Flow (all by record id and numbers):

  0. optional bootstrap: bootstrap.bootstrap_pilot / bootstrap_scratch register the
     initial candidate pool (pilot-seed variants + from-scratch seeds);
  1. evaluate every unevaluated (candidate, channel, model) triple of the pool over the
     grid channels and --models: a scoring episode (hillclimb.run_episode, budget >= 40,
     on the --goal task, default "unsolvable" so agents keep trying for the whole
     budget), obs ingest, the numeric metric (hillclimb.score_run, incl. engagement), the
     cross-model trigger panel (triggers.run_panel -> consensus triggers.json), then
     landscape.primary_trigger -> cell_key -> archive.consider -> archive.save;
  2. probe channels (--probe-channels, default guestbook,paste,kv): each generation the
     current top-k elites (by instigation_score) and the pilot are evaluated on them; a
     probe channel is PROMOTED into this goal's grid for good (qd_state
     "promoted_by_goal") as soon as any episode on it has n_writers >= 1;
  3. per generation: archive.select_parents (exploration-biased, favouring under-filled
     (model, cell) pairs), mutated children via generate.generate_mutation (optionally
     nudged toward an under-filled channel x band with a script-authored instruction
     holding only an enum channel name and a char count), evaluated as in 1, then 2;
  4. publish the scoreboard + landscape (goal + models) through the fail-closed leak gate.

Isolation: this process is an orchestrator of record ids, enum strings and floats.
Candidate text is written and read only by generate.py / the VMs / the metric and
classifier tools; nothing here opens a chatter file. Stdout carries one numbers-only
JSON line per evaluation and a final summary; failures surface as FIXED codes
(E_EPISODE / E_METRIC / E_TRIGGERS / E_CELL / E_ARCHIVE / E_GEN / E_SPEND / ...), never an
exception message (which could echo content). Episode subprocess chatter (orchestrate
progress prints) is diverted to stderr so stdout stays machine-readable.

Parallelism: one worker pool per provider (--workers-cerebras, --workers-berget), each
worker on its own host-port lane (host_port_base + lane * LANE_STRIDE); lane indices are
unique across all pools, held by orchestrate.port_lane. Every QD episode is
candidate-seeded, so its run id folds the record id, the goal segment, the model slug and
a unique rep: orchestrate.ctx_from_args therefore lets even the default model run off its
historical lane. A slug listed in deps.single_lane is still pinned (its provider pool must
then be 1 worker on that lane; anything else is refused up front with E_LANES).
Archive / registry / state / spend bookkeeping happen under one lock; obs ingest (a single
duckdb file) is serialized under another.

Spend: priced models (config price not None, i.e. Berget) are refused unless
--spend-cap-eur X > 0, a hard ceiling on this invocation's priced spend. Before every
priced episode the driver projects its cost conservatively (max of the COST_PRIOR
projection, the ledger's own projection and the most expensive observed settled run of
that model at this agent count) and requires both the invocation headroom and the
provider ledger's headroom (cap - committed - this driver's in-flight projections not yet
reserved in the ledger) to cover it; otherwise it emits E_SPEND and stops scheduling that provider. The ledger is
opened exactly as orchestrate opens it (the main checkout's runs/_spend even from a
worktree, config.spend_dir); a mismatch is refused with E_SPEND_DIR. The driver never
writes ledger entries -- orchestrate reserves and settles; a priced episode's invocation
charge is its settled ledger eur, or its projection when no settled entry is found.
The generator (--gen-model, mutation and bootstrap calls) goes through modelio, outside the
ledger, so a priced --gen-model is refused with E_GEN_PRICED (as the panel's E_PANEL_PRICED).

Resumability: data/instigation/qd_state.json records every evaluated
"<record_id>|<channel>|<model>|<goal>" key, the managed candidate pool, the promoted
channels per goal, a global rep counter and the best numeric score per record per goal; a
rerun skips evaluated keys and re-tries pool triples (over the grid, promoted channels
included) that never finished. The state file is shared across goals and concurrent
invocations: every save is a read-merge-write under an flock on its .lock sibling, and reps
are handed out under that lock, so run ids / VM names never repeat. The default archive is
goal-specific (landscape_archive.json for solvable, landscape_archive.unsolvable.json for
unsolvable) so goals never share or overwrite elites.

NOTE: a real (non --dry-run) invocation performs model calls and microsandbox
episodes; the study validator runs it, not a coding agent. --dry-run swaps in
fake, content-free episode/metric/panel/generator/ledger functions over a temp data
dir for smoke testing (several models, a priced one against a fake ledger).
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import itertools
import inspect
import json
import math
import queue
import sys
import tempfile
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_LAB = Path(__file__).resolve().parents[1]
if str(_LAB) not in sys.path:
    sys.path.insert(0, str(_LAB))

import config  # noqa: E402  (host constants: model registry, retired flags)
from study import registry  # noqa: E402  (pure stdlib, content-free)

STATE_PATH = registry.DATA_DIR / "instigation" / "qd_state.json"
STATE_SCHEMA = "instigation.qd_state.v3"
ARCHIVE_DEFAULT = registry.DATA_DIR / "instigation" / "landscape_archive.json"

MIN_BUDGET = 40          # short budgets drew 0 writers (PROTOCOL.md 3); enforced, not advisory
LANE_STRIDE = 100        # host ports per lane; lanes never overlap while n_agents <= LANE_STRIDE
REP_BASE = 7000          # qd episodes use reps >= 7000, clear of hillclimb's g*1000 + i range

GOALS = ("solvable", "unsolvable")      # mirrors config.GOALS (asserted in the tests)
LEGACY_GOAL = "solvable"                # state / archive entries from before the goal factor
DEFAULT_GOAL = "unsolvable"             # the study condition: agents keep trying for the whole budget
# Active (non-retired) models in config.MODEL_ORDER: Cerebras qwen38, then the priced Berget models,
# which need --spend-cap-eur > 0 (else E_SPEND_CAP). gptoss120 is retired (config.MODELS): naming it in
# --models / --trigger-panel / --gen-model is refused with E_RETIRED unless --allow-retired.
DEFAULT_MODELS = config.active_models()
DEFAULT_PANEL = ("qwen38",)  # free on Cerebras (triggers.DEFAULT_PANEL)
DEFAULT_CHANNELS = ("forum", "js", "html")  # grid; "irc" dropped from the default (still loadable)
DEFAULT_PROBES = ("guestbook", "paste", "kv")
PROBE_TOP_K = 3
PROVIDER_ORDER = ("cerebras", "berget")  # lane allocation order; other providers follow, sorted
DEFAULT_WORKERS = {"cerebras": 4, "berget": 2}
DRY_SPEND_CAP_EUR = 1.0                  # --dry-run only: a fake EUR ceiling against the fake ledger

# Target post-body char counts used to nudge a mutation toward a length band (numbers only).
BAND_TARGET_CHARS = {"le120": 90, "121-200": 160, "201-320": 260, "gt320": 420}

# Continuous dataset collection (study/dataset.py): AUTO builds the real package only when the driver runs on
# the real study data (default registry and candidates); a scratch / test tree collects nothing unless an
# explicit dataset_out is given (the dry run points it into its temp dir).
DATASET_AUTO = "auto"


class QDError(RuntimeError):
    """args[0] is a fixed error code."""


# --------------------------------------------------------------------------- small helpers

def _isnum(v) -> bool:
    """A finite int/float (not bool): json.loads accepts Infinity/NaN, which int() cannot take."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _accepts(fn, name: str) -> bool:
    """Does ``fn`` take keyword ``name`` (or **kwargs)?"""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _call_compat(fn, *args, **kw):
    """Call ``fn`` passing only the keyword args its signature accepts.

    bootstrap.py / scoreboard.py are written concurrently against a loose contract; this
    keeps the driver working whatever optional kwargs (registry_path, candidates_dir, ...)
    a given version exposes. A ``**kwargs`` signature receives everything.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(*args, **kw)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(*args, **kw)
    return fn(*args, **{k: v for k, v in kw.items() if k in params})


def _valid_rid(rid) -> bool:
    return isinstance(rid, str) and bool(registry.id_from_path(f"{rid}.chatter.json"))


def _fixed_code(v, default: str) -> str:
    """``v`` if it looks like one of our fixed error codes (E_ + [A-Z0-9_], short), else ``default``.
    Guards against ever forwarding free text from an exception or a sibling module's meta."""
    if isinstance(v, str) and 2 < len(v) <= 32 and v.startswith("E_") and all(
            c.isupper() or c.isdigit() or c == "_" for c in v):
        return v
    return default


def _key(rid: str, channel: str, model: str, goal: str) -> str:
    """Resumability key of one evaluation: (record_id, channel, model, goal)."""
    return f"{rid}|{channel}|{model}|{goal}"


def _csv(s) -> list[str]:
    out = []
    for x in (s or "").split(","):
        x = x.strip()
        if x and x not in out:
            out.append(x)
    return out


def goal_archive_path(goal: str, base: Path = None) -> Path:
    """Default archive per goal: ``base`` for solvable (the published v1 landscape's archive), a
    ``.unsolvable`` sibling otherwise, so a goal-pinned archive never drops another goal's elites."""
    base = Path(base) if base is not None else ARCHIVE_DEFAULT
    if goal == LEGACY_GOAL:
        return base
    return base.with_name(f"{base.stem}.{goal}{base.suffix}")


def _chan_list(v) -> list[str]:
    return [c for c in dict.fromkeys(v) if isinstance(c, str) and c.isalpha() and c.islower()] \
        if isinstance(v, list) else []


def load_state(path: Path) -> dict:
    """Load qd_state.json -> {evaluated:set, pool:list, next_rep:int, scores_by_goal:{goal:{rid:{...}}},
    promoted_by_goal:{goal:[channel]}}. A v1 state's "scores" are read as the legacy (solvable) goal's;
    its two-part "<rid>|<channel>" evaluated keys are kept verbatim (they never match a four-part key).
    A goal-agnostic "promoted" list (earlier v2) is read as the legacy goal's promotions."""
    st = {"evaluated": set(), "pool": [], "next_rep": REP_BASE, "scores_by_goal": {}, "promoted_by_goal": {}}
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return st
    if not isinstance(data, dict):
        return st
    ev = data.get("evaluated")
    if isinstance(ev, list):
        st["evaluated"] = {p for p in ev if isinstance(p, str)}
    pool = data.get("pool")
    if isinstance(pool, list):
        st["pool"] = [r for r in dict.fromkeys(r for r in pool if isinstance(r, str)) if _valid_rid(r)]
    if isinstance(data.get("next_rep"), int) and not isinstance(data.get("next_rep"), bool):
        st["next_rep"] = max(REP_BASE, data["next_rep"])

    def _scores(sc):
        if not isinstance(sc, dict):
            return {}
        return {rid: {k: v for k, v in row.items() if _isnum(v)}
                for rid, row in sc.items() if _valid_rid(rid) and isinstance(row, dict)}

    sbg = data.get("scores_by_goal")
    if isinstance(sbg, dict):
        st["scores_by_goal"] = {g: _scores(sc) for g, sc in sbg.items() if g in GOALS}
    elif isinstance(data.get("scores"), dict):  # v1
        st["scores_by_goal"] = {LEGACY_GOAL: _scores(data["scores"])}
    pbg = data.get("promoted_by_goal")
    if isinstance(pbg, dict):
        st["promoted_by_goal"] = {g: _chan_list(v) for g, v in pbg.items() if g in GOALS}
    legacy = _chan_list(data.get("promoted"))
    if legacy:
        cur = st["promoted_by_goal"].setdefault(LEGACY_GOAL, [])
        cur += [c for c in legacy if c not in cur]
    return st


@contextlib.contextmanager
def _state_lock(path: Path):
    """Exclusive cross-process flock on ``<state>.lock``: concurrent invocations (e.g. one per goal)
    share one state file, so every save is a locked read-merge-write and reps stay globally unique."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _merge_disk(state: dict, path: Path) -> None:
    """Fold the on-disk state into ``state`` in place (caller holds _state_lock): union of evaluated
    keys, pool (disk-only ids appended) and promotions, max next_rep, best score per record per goal."""
    disk = load_state(path)
    state["evaluated"] |= disk["evaluated"]
    for rid in disk["pool"]:
        if rid not in state["pool"]:
            state["pool"].append(rid)
    state["next_rep"] = max(int(state["next_rep"]), int(disk["next_rep"]))
    sbg = state.setdefault("scores_by_goal", {})
    for g, sc in disk["scores_by_goal"].items():
        mine = sbg.setdefault(g, {})
        for rid, row in sc.items():
            cur = mine.get(rid)
            new = row.get("instigation_score")
            if cur is None or (_isnum(new) and (not _isnum(cur.get("instigation_score"))
                                                 or float(new) > float(cur["instigation_score"]))):
                mine[rid] = row
    pbg = state.setdefault("promoted_by_goal", {})
    for g, chans in disk["promoted_by_goal"].items():
        cur = pbg.setdefault(g, [])
        cur += [c for c in chans if c not in cur]


def _write_state(state: dict, path: Path) -> None:
    sbg = state.get("scores_by_goal") or {}
    pbg = state.get("promoted_by_goal") or {}
    doc = {"schema": STATE_SCHEMA, "evaluated": sorted(state["evaluated"]),
           "pool": list(state["pool"]), "next_rep": int(state["next_rep"]),
           "promoted_by_goal": {g: list(pbg[g]) for g in sorted(pbg) if pbg[g]},
           "scores_by_goal": {g: {k: sbg[g][k] for k in sorted(sbg[g])} for g in sorted(sbg)}}
    registry._atomic_write(Path(path), json.dumps(doc, indent=2, sort_keys=True))


def save_state(state: dict, path: Path) -> None:
    """Locked read-merge-write: another process's evaluated keys, pool, reps, scores and promotions
    are folded into ``state`` (in place) before it is written, never overwritten."""
    with _state_lock(path):
        _merge_disk(state, path)
        _write_state(state, path)


def take_rep(state: dict, path: Path) -> int:
    """Hand out the next rep under the state lock (merged with the disk counter, saved at once), so
    concurrent invocations never reuse a rep and so never a run id / VM name."""
    with _state_lock(path):
        _merge_disk(state, path)
        rep = int(state["next_rep"])
        state["next_rep"] = rep + 1
        _write_state(state, path)
        return rep


def _engagement(metric: dict) -> tuple[dict, float]:
    """Numbers-only engagement block + score from the metric (0 / {} if the metric lacks it)."""
    eng_in = metric.get("engagement") if isinstance(metric.get("engagement"), dict) else {}
    eng = {k: v for k, v in eng_in.items() if isinstance(k, str) and _isnum(v)}
    es = metric.get("engagement_score")
    if not _isnum(es):
        es = eng.get("engagement_score", 0.0)
    es = float(es) if _isnum(es) else 0.0
    eng.pop("engagement_score", None)
    return eng, es


def _prior_eur(slug: str, n_agents: int) -> float:
    """EUR of one n-agent run at the COST_PRIOR token counts (0 for an unpriced model)."""
    import config
    import spend
    return float(spend.cost(slug, {k: n_agents * v for k, v in config.COST_PRIOR_TOKENS.items()}))


def _model_spec(slug: str) -> dict:
    """{"provider": str, "priced": bool, "retired": bool} from config.MODELS; KeyError for an unknown slug."""
    m = config.MODELS[slug]
    return {"provider": m["provider"], "priced": None not in (m.get("price_in"), m.get("price_out")),
            "retired": bool(m.get("retired", False))}


def _agreement_values(artifact) -> list[float]:
    """The panel's per-agent agreement fractions (numbers only), [] without a panel block."""
    panel = artifact.get("panel") if isinstance(artifact, dict) else None
    agr = panel.get("agreement") if isinstance(panel, dict) else None
    if not isinstance(agr, dict):
        return []
    return [float(v) for v in agr.values() if _isnum(v) and 0.0 <= v <= 1.0]


# --------------------------------------------------------------------------- dependencies

def default_deps():
    """Real implementations, imported lazily (sibling modules are developed concurrently and
    hillclimb pulls in orchestrate/config, which is not needed for --dry-run or tests)."""
    from study import generate, hillclimb, landscape, triggers

    def _bootstrap(name):
        def call(*a, **kw):
            from study import bootstrap
            return _call_compat(getattr(bootstrap, name), *a, **kw)
        return call

    def publish(scores, archive, registry_path, goal=None, models=None):
        from study import scoreboard
        return _call_compat(scoreboard.build_and_write, scores, archive=archive, registry_path=registry_path,
                            goal=goal, models=models)

    def run_panel(run_dir, agents, slugs, call_model=None):
        fn = getattr(triggers, "run_panel", None)
        if fn is None:  # pre-panel triggers.py: the first slug alone
            return triggers.run(run_dir, agents, slugs[0], call_model)
        return fn(run_dir, agents, list(slugs), call_model)

    def ledger_for(provider, slug, cap_eur):
        """The provider ledger exactly as orchestrate opens it for ``slug`` (same path, same cap), checked
        against config.spend_dir (the main checkout's runs/_spend from a worktree). E_SPEND_DIR otherwise."""
        import config
        import orchestrate
        ns = types.SimpleNamespace(model=slug, provider=None, host_port_base=config.HOST_PORT_BASE + LANE_STRIDE,
                                   tag="", goal=config.DEFAULT_GOAL, cap_eur=cap_eur, seed_path=None)
        try:
            led = orchestrate.ctx_from_args(ns).ledger
        except (Exception, SystemExit):  # noqa: BLE001 -- fixed code only
            raise QDError("E_SPEND_DIR") from None
        sd = getattr(config, "spend_dir", None)
        if led is None or sd is None:
            raise QDError("E_SPEND_DIR")
        try:
            want = (Path(sd(orchestrate.RUNS)) / f"{provider}.json").resolve()
        except (Exception, SystemExit):  # noqa: BLE001
            raise QDError("E_SPEND_DIR") from None
        if Path(led.path).resolve() != want:
            raise QDError("E_SPEND_DIR")
        return led

    return types.SimpleNamespace(
        run_episode=hillclimb.run_episode, obs_ingest=hillclimb.obs_ingest,
        score_run=hillclimb.score_run, run_panel=run_panel,
        episode_run_id=getattr(hillclimb, "episode_run_id", None),
        generate_mutation=generate.generate_mutation, landscape=landscape,
        bootstrap_pilot=_bootstrap("bootstrap_pilot"), bootstrap_scratch=_bootstrap("bootstrap_scratch"),
        publish=publish, runs_dir=None, single_lane=_single_lane(),
        model_spec=_model_spec, ledger_for=ledger_for, prior_eur=_prior_eur)


def _single_lane() -> dict:
    """{slug: the only host_port_base it may run on}. Empty: orchestrate.ctx_from_args pins the default
    slug to its lane only for non-candidate seeds, and every QD episode boots from a candidate file
    whose record id (plus a unique rep) keeps run ids, and so VM names, distinct across lanes."""
    return {}


class FakeLedger:
    """In-memory stand-in for spend.Ledger (dry-run / tests): the read API the driver uses plus a
    ``settle_fake`` hook the fake episode calls in place of orchestrate's reserve/settle."""

    def __init__(self, cap: float, provider: str = "berget"):
        self.cap, self.provider = float(cap), provider
        self.runs: dict[str, dict] = {}
        self.halted = None
        self._lock = threading.Lock()

    def committed(self) -> float:
        with self._lock:
            return sum(e["eur"] or 0.0 for e in self.runs.values())

    def projected(self, slug, n) -> float:
        return 0.0

    def state(self) -> dict:
        with self._lock:
            return {"halted": self.halted, "cap_eur": self.cap, "runs": json.loads(json.dumps(self.runs))}

    def settle_fake(self, run_id, slug, n, eur) -> None:
        with self._lock:
            self.runs[run_id] = {"run_id": run_id, "slug": slug, "n_agents": n, "status": "settled",
                                 "eur": round(float(eur), 6)}


def dry_run_deps(tmp: Path):
    """Fake, content-free deps for --dry-run: no VM, no model, no network, no real ledger.

    Candidates are neutral placeholder seeds ("lorem") written under ``tmp``; metrics and trigger panel
    artifacts are deterministic numbers derived from a hash of the run id (which folds channel, model,
    goal and rep). Priced models settle a deterministic fake EUR amount into a per-provider FakeLedger.
    """
    from study import landscape, triggers

    tmp = Path(tmp)
    cand_dir = tmp / "candidates"
    ledgers: dict[str, FakeLedger] = {}
    ledger_lock = threading.Lock()

    def _h(*parts) -> int:
        return int(hashlib.blake2b("|".join(map(str, parts)).encode(), digest_size=8).hexdigest(), 16)

    def _placeholder(n_words: int, salt: int) -> dict:
        body = " ".join(["lorem"] * max(1, n_words))
        return {"board_name": f"placeholder-{salt}",
                "threads": [{"slug": "t", "title": "lorem", "posts": [
                    {"author": "a", "body": body, "ago_min": 1}]}]}

    def _persist(cand, *, parent_id, generation, channel, model, registry_path, candidates_dir, origin):
        rid = registry.record_id(cand)
        path = registry.candidate_path(rid, candidates_dir or cand_dir)
        registry._atomic_write(path, registry.canonical_json(cand))
        row = registry.make_row(cand, parent_id=parent_id, generation=generation, channel=channel,
                                model=model, chatter_path=str(path), bootstrap=origin == "pilot",
                                origin=origin)
        registry.upsert(row, registry_path)
        return {"record_id": rid, "length_chars": row["length_chars"]}

    def bootstrap_pilot(pilot_path=None, channels=("forum",), n_variants=0, registry_path=None,
                        candidates_dir=None, **_):
        out = []
        for i in range(1 + int(n_variants)):
            parent = out[0]["record_id"] if out else None
            out.append(_persist(_placeholder(5 + 7 * i, 100 + i), parent_id=parent, generation=0,
                                channel=channels[0], model="dry", registry_path=registry_path,
                                candidates_dir=candidates_dir, origin="pilot"))
        return out

    def bootstrap_scratch(channels, per_channel=1, registry_path=None, candidates_dir=None, **_):
        out = []
        for ci, ch in enumerate(channels):
            for i in range(int(per_channel)):
                out.append(_persist(_placeholder(20 + 30 * i + 11 * ci, 200 + 10 * ci + i), parent_id=None,
                                    generation=0, channel=ch, model="dry", registry_path=registry_path,
                                    candidates_dir=candidates_dir, origin="scratch"))
        return out

    def generate_mutation(parent_path, *, channel="forum", generation=1, registry_path=None,
                          candidates_dir=None, extra_instruction="", **_):
        pid = registry.id_from_path(parent_path) or None
        salt = _h(pid, channel, generation, extra_instruction)
        return _persist(_placeholder(3 + salt % 90, salt), parent_id=pid, generation=generation,
                        channel=channel, model="dry", registry_path=registry_path,
                        candidates_dir=candidates_dir, origin="mutation")

    def episode_run_id(seed_path, *, iface, n_agents, rep, slug="qwen38", goal=DEFAULT_GOAL, **_):
        rid = registry.id_from_path(seed_path) or "x"
        return (f"{iface}-multi{n_agents}-r{rep}" + ("-unsolvable" if goal == "unsolvable" else "")
                + ("" if slug == "qwen38" else f"--{slug}") + f"-{rid}")

    def ledger_for(provider, slug, cap_eur):
        import config
        with ledger_lock:
            if provider not in ledgers:
                cap = cap_eur if cap_eur is not None else config.PROVIDERS[provider].get("spend_cap_eur") or 0.0
                ledgers[provider] = FakeLedger(cap, provider)
            return ledgers[provider]

    def run_episode(seed_path, *, iface, n_agents, rep, budget, slug="qwen38", goal=DEFAULT_GOAL, **_):
        run_id = episode_run_id(seed_path, iface=iface, n_agents=n_agents, rep=rep, slug=slug, goal=goal)
        d = tmp / "runs" / run_id
        d.mkdir(parents=True, exist_ok=True)
        spec = _model_spec(slug)
        if spec["priced"]:  # orchestrate's reserve/settle, faked: a deterministic fraction of the prior
            prior = _prior_eur(slug, n_agents)
            ledger_for(spec["provider"], slug, None).settle_fake(
                run_id, slug, n_agents, prior * (0.3 + (_h("eur", run_id) % 50) / 100.0))
        return d

    def score_run(run_dir):
        h = _h(Path(run_dir).name)
        n_agents = 4
        n_writers = h % (n_agents + 1)
        agents = [{"agent_id": i, "wrote": i < n_writers,
                   "time_to_first_write_s": (10.0 + i + (h >> 8) % 7) if i < n_writers else None}
                  for i in range(n_agents)]
        eng = {"board_reads": (h >> 4) % 9, "relay_connections": (h >> 9) % 5, "board_refs": (h >> 13) % 4,
               "agents_engaged": min(n_agents, (h >> 17) % 5), "engagement_score": ((h >> 21) % 101) / 100.0}
        return {"run_id": Path(run_dir).name, "n_writers": n_writers,
                "seed_chars": (h >> 3) % 500, "instigation_score": round(((h >> 30) % 1000) / 1000.0 * n_writers / n_agents, 6),
                "I": round(((h >> 40) % 1000) / 1000.0 * n_writers / n_agents, 6),
                "agents": agents, "engagement": eng, "engagement_score": eng["engagement_score"]}

    def run_panel(run_dir, agents, slugs, call_model=None):
        h = _h("trig", Path(run_dir).name)
        if h % 7 == 0:
            raise RuntimeError("E_DRY")
        m = score_run(run_dir)
        from study import instigation
        pwf = instigation.peer_wrote_first([{**a, "_first_ts": a["time_to_first_write_s"] if a["wrote"] else None}
                                            for a in m["agents"]])
        recs, votes, agreement = [], {}, {}
        for a in m["agents"]:
            if not a["wrote"]:
                continue
            aid = a["agent_id"]
            vs = {s: (None if (h >> (aid + 3 * j)) % 6 == 0
                      else triggers.ENUM[((h >> aid) + (j * ((h >> 11) % 2))) % len(triggers.ENUM)])
                  for j, s in enumerate(slugs)}
            votes[str(aid)] = vs
            counts = {}
            for s in slugs:
                if vs[s] is not None:
                    counts[vs[s]] = counts.get(vs[s], 0) + 1
            if not counts:
                agreement[str(aid)] = 0.0
                recs.append({"ok": False, "error_code": "E_PANEL", "line": 1, "col": 1, "agent_id": aid})
                continue
            top = max(counts.values())
            code = next(vs[s] for s in slugs if vs[s] is not None and counts[vs[s]] == top)
            agreement[str(aid)] = counts[code] / len(slugs)
            recs.append({"agent_id": aid, "codes": [{"code": code, "confidence": 0.8}]})
        return {"run_id": m["run_id"], "peer_wrote_first": {str(k): bool(v) for k, v in pwf.items()},
                "records": recs, "panel": {"slugs": list(slugs), "votes": votes, "agreement": agreement}}

    def publish(scores, archive, registry_path, goal=None, models=None):
        # The real scoreboard + landscape builders and the strict landscape validator run, but the
        # output goes to ``tmp`` (never site/public) and the leak gate is a stub: the dry-run tree
        # holds only placeholder seeds, so there is no corpus to gate against.
        from study import scoreboard
        return _call_compat(scoreboard.build_and_write,
                            scores, archive=archive, registry_path=registry_path,
                            out_path=tmp / "publish" / "scoreboard.json",
                            landscape_out_path=tmp / "publish" / "landscape.json",
                            gate=lambda _p: {"ok": True}, verify=None, goal=goal, models=models)

    return types.SimpleNamespace(
        run_episode=run_episode, obs_ingest=lambda *a, **k: True, score_run=score_run,
        run_panel=run_panel, episode_run_id=episode_run_id, generate_mutation=generate_mutation,
        landscape=landscape, bootstrap_pilot=bootstrap_pilot, bootstrap_scratch=bootstrap_scratch,
        publish=publish, runs_dir=tmp / "runs", model_spec=_model_spec, ledger_for=ledger_for,
        prior_eur=_prior_eur, ledgers=ledgers)


# --------------------------------------------------------------------------- driver

class QD:
    """One MAP-Elites invocation. Every public effect is numbers / ids / enums."""

    def __init__(self, *, channels, models=DEFAULT_MODELS, goal=DEFAULT_GOAL, budget=MIN_BUDGET, n_agents=4,
                 workers=None, probe_channels=(), probe_top_k=PROBE_TOP_K, gen_slug="qwen38",
                 trigger_panel=DEFAULT_PANEL, allow_paid=False, cap_eur=None, spend_cap_eur=0.0,
                 allow_retired=False,
                 host_port_base=19000, registry_path=None, candidates_dir=None, archive_path=None,
                 state_path=None, db=None, deps=None, out=None, child_channels="all",
                 dataset_out=DATASET_AUTO, dataset_pilot=None):
        if int(budget) < MIN_BUDGET:
            raise QDError("E_BUDGET")
        if not 1 <= int(n_agents) <= LANE_STRIDE:
            raise QDError("E_AGENTS")
        if goal not in GOALS:
            raise QDError("E_GOAL")
        self.deps = deps or default_deps()
        self.L = self.deps.landscape
        self.goal = goal
        if goal != LEGACY_GOAL and not _accepts(self.deps.run_episode, "goal"):
            raise QDError("E_GOAL")   # never silently run a solvable episode for an unsolvable study

        # ---- channels: the grid (--channels + promoted probes) and the probe-only channels
        self.state_path = Path(state_path) if state_path is not None else STATE_PATH
        self.state = load_state(self.state_path)
        base = list(dict.fromkeys(channels))
        probes = list(dict.fromkeys(probe_channels or ()))
        if not base:
            raise QDError("E_CHANNEL")
        for ch in base + probes:
            try:
                self.L.channel_axis(ch)
            except ValueError:
                raise QDError("E_CHANNEL") from None
        self.base_channels = base
        # promotion is per goal and permanent: a channel promoted for this goal in any earlier
        # invocation joins the grid whether or not it is still listed in --probe-channels
        self.promoted = self.state["promoted_by_goal"].setdefault(goal, [])
        self.grid = list(base)
        for c in self.promoted:
            if c in self.grid:
                continue
            try:
                self.L.channel_axis(c)
            except ValueError:
                continue
            self.grid.append(c)
        self.probe_channels = [c for c in probes if c not in self.grid]

        # ---- models / providers / spend
        self.models = list(dict.fromkeys(models or ()))
        if not self.models:
            raise QDError("E_MODEL")
        self.allow_retired = bool(allow_retired)
        self.spec = {}
        for m in self.models + [gen_slug]:
            try:
                sp = self.deps.model_spec(m)
            except (Exception, SystemExit):  # noqa: BLE001
                raise QDError("E_MODEL") from None
            if sp.get("retired") and not self.allow_retired:
                raise QDError("E_RETIRED")        # retired models stay loadable for old data only
            self.spec[m] = {"provider": str(sp["provider"]), "priced": bool(sp["priced"])}
        self.panel = list(dict.fromkeys(trigger_panel or ()))
        if not self.panel:
            raise QDError("E_PANEL_SLUG")
        for s in self.panel:
            try:
                sp = self.deps.model_spec(s)
            except (Exception, SystemExit):  # noqa: BLE001
                raise QDError("E_PANEL_SLUG") from None
            if sp.get("retired") and not self.allow_retired:
                raise QDError("E_RETIRED")
            if sp["priced"]:
                raise QDError("E_PANEL_PRICED")   # classifier calls bypass the ledger: free models only
        if self.spec[gen_slug]["priced"]:
            raise QDError("E_GEN_PRICED")     # generator calls go through modelio, outside the ledger
        self.gen_slug = gen_slug
        self.allow_paid, self.cap_eur = allow_paid, cap_eur
        self.spend_cap_eur = float(spend_cap_eur or 0.0)
        if not _isnum(self.spend_cap_eur) or self.spend_cap_eur < 0:
            raise QDError("E_SPEND_CAP")
        self.priced = [m for m in self.models if self.spec[m]["priced"]]
        if self.priced and self.spend_cap_eur <= 0:
            raise QDError("E_SPEND_CAP")
        self.ledgers = {}
        for m in self.priced:  # open (and path-check) every priced provider's ledger up front: fail closed
            p = self.spec[m]["provider"]
            if p not in self.ledgers:
                try:
                    self.ledgers[p] = self.deps.ledger_for(p, m, cap_eur)
                except QDError:
                    raise
                except (Exception, SystemExit):  # noqa: BLE001
                    raise QDError("E_SPEND_DIR") from None

        # ---- per-provider worker pools and globally unique lanes
        if workers is None:
            workers = DEFAULT_WORKERS
        providers = list(dict.fromkeys(self.spec[m]["provider"] for m in self.models))
        providers.sort(key=lambda p: (PROVIDER_ORDER.index(p) if p in PROVIDER_ORDER else len(PROVIDER_ORDER), p))
        self.providers = providers
        self.workers = {p: max(1, int(workers if isinstance(workers, int) else workers.get(p, 1)))
                        for p in providers}
        self.n_agents = int(n_agents)
        self.budget = int(budget)
        self.host_port_base = int(host_port_base)
        self.lanes: dict[str, queue.Queue] = {}
        self.lane_index: dict[str, list[int]] = {}
        nxt = 0
        for p in providers:
            q = queue.Queue()
            self.lane_index[p] = list(range(nxt, nxt + self.workers[p]))
            for i in self.lane_index[p]:
                q.put(i)
            self.lanes[p] = q
            nxt += self.workers[p]
        for m in self.models:
            only = (getattr(self.deps, "single_lane", None) or {}).get(m)
            p = self.spec[m]["provider"]
            if only is not None and (self.workers[p] > 1
                                     or self.host_port_base + self.lane_index[p][0] * LANE_STRIDE != int(only)):
                raise QDError("E_LANES")
        gp = self.spec[gen_slug]["provider"]
        self.gen_workers = self.workers.get(gp, 1)

        self.registry_path = Path(registry_path) if registry_path is not None else None
        self.candidates_dir = Path(candidates_dir) if candidates_dir is not None else None
        self.db = Path(db) if db is not None else None
        self.child_channels = child_channels
        self.probe_top_k = max(0, int(probe_top_k))
        self.out = out or sys.stdout
        apath = archive_path if archive_path is not None else goal_archive_path(
            goal, getattr(self.L, "ARCHIVE_PATH", ARCHIVE_DEFAULT))
        if _accepts(self.L.Archive.load, "goal"):
            self.archive = self.L.Archive.load(apath, goal=goal)
        else:
            self.archive = self.L.Archive.load(apath)
        self.scores = self.state["scores_by_goal"].setdefault(goal, {})

        self.lock = threading.RLock()          # archive / registry / state / spend / summary
        self.ingest_lock = threading.Lock()    # obs duckdb is single-writer
        self.print_lock = threading.Lock()
        self.evals: list[dict] = []           # this invocation's successful evaluations
        self.errors: dict[str, int] = {}
        self.classifier_errors: dict[str, int] = {}   # per-agent classifier error codes (E_PARSE, E_PANEL, ...)
        self.n_classified_agents = 0                  # per-agent classifier records seen
        self.agreement_sum, self.agreement_n = 0.0, 0
        self.stopped: set[str] = set()                # providers no longer scheduled (spend)
        self.spent = {m: 0.0 for m in self.priced}    # this invocation's priced EUR per model
        self.inflight: dict[str, float] = {}          # provider -> in-flight projected EUR
        self.inflight_runs: dict[int, tuple] = {}     # token -> (provider, projected EUR, run id or None)
        self._tokens = itertools.count()
        self.n_skipped_spend = 0
        self.promoted_now: list[str] = []

        # ---- continuous dataset collection (best effort, numbers-only bookkeeping)
        if dataset_out == DATASET_AUTO:
            dataset_out = "default" if (registry_path is None and candidates_dir is None) else None
        self.dataset_out = dataset_out          # None: off; "default": dataset.DATASET_DIR; else a path
        self.dataset_pilot = dataset_pilot      # None: the real pilot; False: none (scratch trees)
        self._runs_seen = None                  # runs dir of the episodes this invocation evaluated
        self.dataset_stats = {"builds": 0, "new_versions": 0, "errors": {}, "version": None}

    # ---- output
    def emit(self, obj: dict) -> None:
        with self.print_lock:
            self.out.write(json.dumps(obj, sort_keys=True) + "\n")
            self.out.flush()

    def _err(self, code: str, **ids) -> None:
        with self.lock:
            self.errors[code] = self.errors.get(code, 0) + 1
        self.emit({**ids, "error": code})

    # ---- archive views (per-model archive; a flat v1 archive is grouped by its records' model)
    def _elites_by_model(self) -> dict:
        ebm = getattr(self.archive, "elites_by_model", None)
        if callable(ebm):
            return ebm()
        out = {}
        for key, rec in self.archive.elites.items():
            out.setdefault(rec.get("model") or "", {})[key] = rec
        return out

    def _coverage_cells(self) -> int:
        cc = getattr(self.archive, "coverage_cells", None)
        if callable(cc):
            return int(cc())
        return len({k for cells in self._elites_by_model().values() for k in cells})

    # ---- registry helpers (ids / numbers only)
    def _row(self, rid: str) -> dict:
        return registry.get(rid, self.registry_path) or {}

    def _mark_origin(self, rid: str, origin: str, bootstrap: bool) -> None:
        # registry.annotate holds registry.REGISTRY_LOCK around its read-modify-write
        registry.annotate(rid, self.registry_path, origin=origin, bootstrap=bool(bootstrap))

    def _add_pool(self, rids) -> None:
        with self.lock:
            for rid in rids:
                if isinstance(rid, str) and rid not in self.state["pool"]:
                    self.state["pool"].append(rid)
            save_state(self.state, self.state_path)

    # ---- step 0
    def bootstrap(self, *, pilot_variants=2, scratch_per_channel=1) -> list[str]:
        """Register the initial pool; returns the record ids. bootstrap.py reports per-item failures
        as {"ok": false, "error": <fixed code>} metas, which are counted, never raised."""
        rids = []
        common = {"registry_path": self.registry_path, "candidates_dir": self.candidates_dir,
                  "slug": self.gen_slug, "allow_paid": self.allow_paid}
        chans = tuple(self.base_channels)
        steps = [("pilot", lambda: _call_compat(self.deps.bootstrap_pilot, channels=chans,
                                                 n_variants=max(0, int(pilot_variants)), **common))]
        if scratch_per_channel > 0:
            steps.append(("scratch", lambda: _call_compat(self.deps.bootstrap_scratch, chans,
                                                          int(scratch_per_channel), **common)))
        for name, call in steps:
            try:
                metas = call() or []
            except (Exception, SystemExit):  # noqa: BLE001 -- fixed code only
                self._err("E_BOOTSTRAP", origin=name)
                continue
            for m in metas:
                if not isinstance(m, dict):
                    continue
                rid = m.get("record_id")
                if m.get("ok") is False or not (isinstance(rid, str) and _valid_rid(rid)):
                    self._err(_fixed_code(m.get("error"), "E_BOOTSTRAP"), origin=name)
                    continue
                if rid not in rids:
                    rids.append(rid)
        self._add_pool(rids)
        return rids

    # ---- step 1: evaluation
    def pending(self, rids=None, channels=None, models=None) -> list[tuple[str, str, str]]:
        """Unevaluated (record_id, channel, model) triples for this goal over ``rids`` (default: the
        managed pool only, which bootstrap() and the generation loop populate) x ``channels`` (default:
        the grid: --channels plus this goal's promoted channels as of now) x ``models`` (default: --models). Registry rows are NOT swept in: registry
        normalisation back-fills origin="scratch" for every parentless legacy row (e.g. hillclimb gen-0
        draws), so an origin filter would schedule unplanned episodes for the whole history."""
        if rids is None:
            rids = list(self.state["pool"])
        chans = channels if channels is not None else list(self.grid)
        mods = models if models is not None else self.models
        seen, out = set(), []
        for rid in rids:
            for ch in chans:
                for m in mods:
                    k = _key(rid, ch, m, self.goal)
                    if k not in self.state["evaluated"] and k not in seen:
                        seen.add(k)
                        out.append((rid, ch, m))
        return out

    def evaluate(self, items, gen: int, probe: bool = False) -> list[dict]:
        """Evaluate (record_id, channel, model) triples, each provider on its own worker pool."""
        if not items:
            return []
        groups = {}
        for it in items:
            groups.setdefault(self.spec[it[2]]["provider"], []).append(it)
        exs = {p: ThreadPoolExecutor(max_workers=self.workers.get(p, 1)) for p in groups}
        try:
            futs = [exs[self.spec[m]["provider"]].submit(self._evaluate_one, rid, ch, m, gen, probe)
                    for rid, ch, m in items]
            results = [f.result() for f in futs]
        finally:
            for ex in exs.values():
                ex.shutdown(wait=True)
        return [r for r in results if r]

    def _next_rep(self) -> int:
        with self.lock:
            return take_rep(self.state, self.state_path)

    # ---- spend gating (priced models only)
    def _projection(self, model: str) -> float:
        """Conservative EUR projection of one priced episode: max of the COST_PRIOR projection, the
        ledger's own projection and the most expensive settled run of this model at this agent count."""
        led = self.ledgers[self.spec[model]["provider"]]
        vals = [float(self.deps.prior_eur(model, self.n_agents))]
        pj = led.projected(model, self.n_agents)
        if _isnum(pj):
            vals.append(float(pj))
        runs = (led.state() or {}).get("runs") or {}
        for e in runs.values():
            if (isinstance(e, dict) and e.get("slug") == model and e.get("n_agents") == self.n_agents
                    and e.get("status") == "settled" and _isnum(e.get("eur"))):
                vals.append(float(e["eur"]))
        return max(vals)

    def _stop(self, provider: str, ids: dict) -> None:
        """Stop scheduling ``provider`` (caller holds self.lock) and report one E_SPEND line."""
        self.stopped.add(provider)
        self.errors["E_SPEND"] = self.errors.get("E_SPEND", 0) + 1
        self.emit({**ids, "error": "E_SPEND"})

    @staticmethod
    def _in_ledger(runs: dict, run_id) -> bool:
        """True iff the ledger already counts ``run_id`` in committed() (planned/reserved projection or
        settled/aborted eur, including a `<run_id>@<ts>` re-keyed entry)."""
        if not isinstance(run_id, str) or not run_id:
            return False
        return any(k == run_id or k.startswith(run_id + "@") for k in runs)

    def _spend_admit(self, model: str, ids: dict, run_id=None):
        """(token, projected EUR) held in flight for this episode, or None (E_SPEND; provider stopped).

        Ledger headroom = cap - committed() - this driver's in-flight projections that the ledger does
        not count yet (no entry under their run id): once orchestrate reserves (or settles) a run, its
        cost is in committed() and is not subtracted twice. The ledger snapshot is read before
        committed(), so a run reserved in between is at worst double-counted (fail safe), never missed."""
        p = self.spec[model]["provider"]
        with self.lock:
            if p in self.stopped:
                self.n_skipped_spend += 1
                return None
            try:
                led = self.ledgers[p]
                proj = self._projection(model)
                halted = led.halted
                runs = (led.state() or {}).get("runs") or {}
                committed = float(led.committed())
                unreserved = sum(pj for pp, pj, rid in self.inflight_runs.values()
                                 if pp == p and not self._in_ledger(runs, rid))
                headroom = float(led.cap) - committed - unreserved
            except (Exception, SystemExit):  # noqa: BLE001 -- unreadable ledger: fail closed
                self._stop(p, ids)
                return None
            used = sum(self.spent.values()) + sum(self.inflight.values())
            if halted or not _isnum(proj) or proj <= 0 or used + proj > self.spend_cap_eur + 1e-9 \
                    or headroom + 1e-9 < proj:
                self._stop(p, ids)
                return None
            tok = next(self._tokens)
            self.inflight_runs[tok] = (p, proj, run_id if isinstance(run_id, str) and run_id else None)
            self.inflight[p] = self.inflight.get(p, 0.0) + proj
            return tok, proj

    def _ledger_eur(self, provider: str, run_id) -> float | None:
        """Settled/aborted EUR of ``run_id`` in the ledger (incl. `<run_id>@<ts>` re-keyed entries)."""
        if not isinstance(run_id, str) or not run_id:
            return None
        try:
            runs = (self.ledgers[provider].state() or {}).get("runs") or {}
        except (Exception, SystemExit):  # noqa: BLE001
            return None
        tot, hit = 0.0, False
        for k, e in runs.items():
            if (k == run_id or k.startswith(run_id + "@")) and isinstance(e, dict) \
                    and e.get("status") in ("settled", "aborted") and _isnum(e.get("eur")):
                tot += float(e["eur"])
                hit = True
        return tot if hit else None

    def _spend_settle(self, model: str, token: int, proj: float, run_id) -> None:
        p = self.spec[model]["provider"]
        eur = self._ledger_eur(p, run_id)
        with self.lock:
            self.inflight_runs.pop(token, None)
            self.inflight[p] = max(0.0, self.inflight.get(p, 0.0) - proj)
            self.spent[model] = self.spent.get(model, 0.0) + (eur if eur is not None else proj)

    def _run_id_for(self, seed_path, channel, model, rep):
        fn = getattr(self.deps, "episode_run_id", None)
        if fn is None:
            return None
        try:
            return _call_compat(fn, seed_path, iface=channel, n_agents=self.n_agents, rep=rep, slug=model,
                                goal=self.goal)
        except (Exception, SystemExit):  # noqa: BLE001
            return None

    def _evaluate_one(self, rid: str, channel: str, model: str, gen: int, probe: bool = False):
        ids = {"gen": gen, "record_id": rid, "channel": channel, "model": model}
        if probe:
            ids["probe"] = True
        p = self.spec[model]["provider"]
        priced = self.spec[model]["priced"]
        with self.lock:
            if p in self.stopped:
                self.n_skipped_spend += 1
                return None
        seed_path = registry.candidate_path(rid, self.candidates_dir)
        lane = self.lanes[p].get()
        try:
            adm = None
            rep = self._next_rep()   # before admission: the run id keys this episode's ledger entry
            if priced:
                adm = self._spend_admit(model, ids, self._run_id_for(seed_path, channel, model, rep))
                if adm is None:
                    return None
            base = self.host_port_base + lane * LANE_STRIDE
            kw = dict(iface=channel, n_agents=self.n_agents, rep=rep, budget=self.budget, slug=model,
                      host_port_base=base, cap_eur=self.cap_eur, allow_paid=bool(priced or self.allow_paid))
            if _accepts(self.deps.run_episode, "goal"):
                kw["goal"] = self.goal
            run_dir = None
            try:
                run_dir = Path(self.deps.run_episode(seed_path, **kw))
            except (Exception, SystemExit):  # noqa: BLE001 -- port_lane / asserts exit; fixed code only
                self._err("E_EPISODE", **ids)
            finally:
                if adm is not None:
                    self._spend_settle(model, adm[0], adm[1], run_dir.name if run_dir is not None
                                       else self._run_id_for(seed_path, channel, model, rep))
            if run_dir is None:
                return None
        finally:
            self.lanes[p].put(lane)

        runs_dir = self.deps.runs_dir or run_dir.parent
        self._runs_seen = Path(runs_dir)
        db = self.db or (Path(runs_dir) / "_study" / "instigation.duckdb")
        with self.ingest_lock:
            try:
                db.parent.mkdir(parents=True, exist_ok=True)
                self.deps.obs_ingest(Path(runs_dir), db, run_id=run_dir.name)
            except (Exception, SystemExit):  # noqa: BLE001 -- best-effort, as in hillclimb
                pass

        try:
            metric = self.deps.score_run(run_dir) or {}
        except (Exception, SystemExit):  # noqa: BLE001
            metric = {}
        score, seed_chars = metric.get("instigation_score"), metric.get("seed_chars")
        raw_i = metric.get("I")  # raw pile-in x speed: the elite objective (landscape.fitness)
        if not _isnum(score):
            self._err("E_METRIC", **ids)
            return None
        seed_chars = int(seed_chars) if _isnum(seed_chars) and seed_chars >= 0 else 0
        n_writers = metric.get("n_writers") if _isnum(metric.get("n_writers")) else 0

        trig_ok = True
        try:
            artifact = self.deps.run_panel(run_dir, None, list(self.panel))
        except (Exception, SystemExit):  # noqa: BLE001 -- classifier failure -> artifact None
            artifact, trig_ok = None, False
        if not isinstance(artifact, dict):
            artifact = None
        cls_errs, n_cls = {}, 0
        if artifact is not None and isinstance(artifact.get("records"), list):
            for r in artifact["records"]:
                if not isinstance(r, dict):
                    continue
                n_cls += 1
                if "error_code" in r:
                    c = _fixed_code(r.get("error_code"), "E_CLASSIFIER")
                    cls_errs[c] = cls_errs.get(c, 0) + 1
        agr = _agreement_values(artifact)

        L = self.L
        try:
            code = L.primary_trigger(artifact, metric)
        except (Exception, SystemExit):  # noqa: BLE001
            code = L.UNRESOLVED if n_writers else L.NO_WRITERS
        try:
            cell = L.cell_key(code, L.channel_axis(channel), L.length_band(seed_chars))
        except ValueError:
            self._err("E_CELL", **ids)
            return None

        eng, eng_score = _engagement(metric)
        row = self._row(rid)
        origin = row.get("origin") if row.get("origin") in L.ORIGINS else (
            "mutation" if row.get("parent_id") else "scratch")
        run_id = metric.get("run_id") if isinstance(metric.get("run_id"), str) else run_dir.name
        rec = {"record_id": rid, "cell_key": cell, "trigger_code": code, "channel": L.channel_axis(channel),
               "length_band": L.length_band(seed_chars), "seed_chars": seed_chars,
               "instigation_score": float(score), "I": float(raw_i) if _isnum(raw_i) else None,
               "n_writers": int(n_writers), "engagement": eng,
               "engagement_score": eng_score, "bootstrap": bool(row.get("bootstrap", origin == "pilot")),
               "origin": origin, "parent_id": row.get("parent_id"), "generation": int(gen),
               "run_id": run_id, "model": model, "goal": self.goal}

        with self.lock:
            try:
                inserted = bool(self.archive.consider(rec))
                self.archive.save()
            except (Exception, SystemExit):  # noqa: BLE001 -- invalid record / write failure
                self.errors["E_ARCHIVE"] = self.errors.get("E_ARCHIVE", 0) + 1
                self.emit({**ids, "error": "E_ARCHIVE"})
                return None
            self.state["evaluated"].add(_key(rid, channel, model, self.goal))
            self.n_classified_agents += n_cls
            for c, n in cls_errs.items():
                self.classifier_errors[c] = self.classifier_errors.get(c, 0) + n
            self.agreement_sum += sum(agr)
            self.agreement_n += len(agr)
            if not trig_ok:
                self.errors["E_TRIGGERS"] = self.errors.get("E_TRIGGERS", 0) + 1
            best = self.scores.get(rid) or {}
            if not _isnum(best.get("instigation_score")) or float(score) > best["instigation_score"]:
                self.scores[rid] = {"instigation_score": float(score), "n_writers": int(n_writers),
                                    "engagement_score": eng_score}
            promote = (channel in self.probe_channels and channel not in self.grid and int(n_writers) >= 1)
            if promote:
                self.grid.append(channel)
                if channel not in self.promoted:
                    self.promoted.append(channel)
                self.promoted_now.append(channel)
            save_state(self.state, self.state_path)
            line = {**ids, "cell_key": cell, "instigation_score": float(score), "n_writers": int(n_writers),
                    "engagement_score": eng_score, "inserted": inserted}
            if not trig_ok:
                line["trigger_error"] = "E_TRIGGERS"
            ev = {**line, "trigger_code": code}
            self.evals.append(ev)
            self.emit(line)
            if promote:
                self.emit({"gen": gen, "event": "promoted", "channel": channel})
        return ev

    # ---- probe channels
    def _pilot_ids(self) -> list[str]:
        """The hand-written pilot(s) in the pool: origin "pilot" without a parent."""
        out = []
        for rid in self.state["pool"]:
            row = self._row(rid)
            if row.get("origin") == "pilot" and not row.get("parent_id"):
                out.append(rid)
        return out

    def _probe_picks(self) -> list[str]:
        """Top-k distinct elite record ids by instigation_score (all models), then the pilot."""
        recs = [r for cells in self._elites_by_model().values() for r in cells.values()]
        recs.sort(key=lambda r: (-float(r.get("instigation_score", 0.0)), str(r.get("record_id"))))
        picks = []
        for r in recs:
            if len(picks) >= self.probe_top_k:
                break
            rid = r.get("record_id")
            if _valid_rid(rid) and rid not in picks:
                picks.append(rid)
        for rid in self._pilot_ids():
            if rid not in picks:
                picks.append(rid)
        return [r for r in picks if registry.candidate_path(r, self.candidates_dir).exists()]

    def probe(self, gen: int) -> list[dict]:
        chans = [c for c in self.probe_channels if c not in self.grid]
        if not chans:
            return []
        return self.evaluate(self.pending(self._probe_picks(), chans), gen, probe=True)

    # ---- step 2: generations
    def _targets(self, n: int, generation: int) -> list[tuple[str, str]]:
        """(channel, band) targets for ``n`` children: the grid-channel x band combos with the fewest
        filled (model, cell) pairs, rotated by generation so ties spread out. Deterministic."""
        L = self.L
        fill = {}
        for cells in self._elites_by_model().values():
            for key in cells:
                _t, c, b = key.split("|")
                fill[(c, b)] = fill.get((c, b), 0) + 1

        def nfill(cb):
            return fill.get((L.channel_axis(cb[0]), cb[1]), 0)

        combos = [(ch, band) for ch in self.grid for band in L.BAND_LABELS]
        order = {cb: i for i, cb in enumerate(combos)}
        combos.sort(key=lambda cb: (nfill(cb), order[cb]))
        lowest = [cb for cb in combos if nfill(cb) == nfill(combos[0])]
        pool = lowest if len(lowest) >= n else combos
        rot = generation % len(pool)
        pool = pool[rot:] + pool[:rot]
        return [pool[i % len(pool)] for i in range(n)]

    @staticmethod
    def instruction(channel: str, band: str) -> str:
        """Script-authored steering text: an enum channel name and a char count, nothing else."""
        from study import generate  # lazy: only the enum-keyed rendering phrase is used
        n = BAND_TARGET_CHARS.get(band, 160)
        r = generate.CHANNEL_RENDERING.get(channel)
        medium = f"'{channel}' style board" + (f" (rendered as {r})" if r else "")
        return (f"Target a {medium}. The length target overrides the shortness rule: "
                f"aim for about {n} characters of total post body text.")

    def _parents(self, k: int, generation: int) -> list[str]:
        with self.lock:
            parents = list(_call_compat(self.archive.select_parents, k, generation, models=list(self.models))
                           or [])
            if parents:
                self.archive.save()   # persist the selection counts
                return parents
        ranked = sorted(self.scores.items(), key=lambda kv: -float(kv[1].get("instigation_score", 0.0)))
        parents = [rid for rid, _ in ranked]
        if not parents:
            parents = list(self.state["pool"]) or sorted(registry.load(self.registry_path))
        parents = [p for p in parents
                   if _valid_rid(p) and registry.candidate_path(p, self.candidates_dir).exists()]
        return parents[:k]

    def _make_child(self, gen: int, parent: str, channel: str, band: str, nudge: bool):
        try:
            meta = self.deps.generate_mutation(
                registry.candidate_path(parent, self.candidates_dir), slug=self.gen_slug, channel=channel,
                generation=gen, allow_paid=self.allow_paid, candidates_dir=self.candidates_dir,
                registry_path=self.registry_path,
                extra_instruction=self.instruction(channel, band) if nudge else "")
        except (Exception, SystemExit):  # noqa: BLE001 -- GenError / ModelError -> fixed code
            self._err("E_GEN", gen=gen, parent_id=parent)
            return None
        rid = meta.get("record_id") if isinstance(meta, dict) else None
        if not _valid_rid(rid):
            self._err("E_GEN", gen=gen, parent_id=parent)
            return None
        try:
            self._mark_origin(rid, "mutation", False)
        except (Exception, SystemExit):  # noqa: BLE001 -- registry write failure -> fixed code
            self._err("E_REGISTRY", gen=gen, parent_id=parent)
            return None
        return rid, channel

    def generation(self, gen: int, batch: int, nudge: bool = True) -> list[dict]:
        parents = self._parents(batch, gen)
        if not parents:
            self._err("E_NO_PARENTS", gen=gen)
            return []
        targets = self._targets(batch, gen)
        jobs = [(parents[j % len(parents)], targets[j][0], targets[j][1]) for j in range(batch)]
        with ThreadPoolExecutor(max_workers=self.gen_workers) as ex:
            kids = [k for k in ex.map(lambda j: self._make_child(gen, j[0], j[1], j[2], nudge), jobs) if k]
        self._add_pool([rid for rid, _ in kids])
        items = []
        for rid, target in kids:
            chans = list(self.grid) if self.child_channels == "all" else [target]
            items += self.pending([rid], chans)
        return self.evaluate(items, gen)

    # ---- continuous dataset collection
    def collect_dataset(self, gen: int) -> dict | None:
        """Rebuild the training-data package (study/dataset.py) from everything collected so far. Best
        effort: a failure is a fixed code counted in the summary, never an exception, never a stdout line.
        Idempotent: dataset.build only creates a new version when the content hash changes."""
        if self.dataset_out is None:
            return None
        try:
            from study import dataset
            archive_paths = {g: goal_archive_path(g, getattr(self.L, "ARCHIVE_PATH", ARCHIVE_DEFAULT))
                             for g in GOALS} if self.dataset_out == "default" else {}
            archive_paths[self.goal] = Path(self.archive.path)
            runs = getattr(self.deps, "runs_dir", None) or self._runs_seen
            if runs is None and self.dataset_out != "default":
                runs = False             # a scratch tree with no episode yet: never scan the real runs/
            kw = dict(runs_dir=runs,
                      registry_path=self.registry_path, candidates_dir=self.candidates_dir,
                      archive_paths=archive_paths, pilot_path=self.dataset_pilot)
            if self.dataset_out != "default":
                kw["out_dir"] = Path(self.dataset_out)
            with self.lock:
                res = dataset.collect(**kw)
        except (Exception, SystemExit):  # noqa: BLE001 -- never crash the QD loop
            res = {"ok": False, "error": "E_DATASET"}
        res = res if isinstance(res, dict) else {"ok": False, "error": "E_DATASET"}
        st = self.dataset_stats
        st["builds"] += 1
        if res.get("ok"):
            st["new_versions"] += 1 if res.get("new_version") else 0
            v = res.get("version")
            st["version"] = v if isinstance(v, str) and len(v) <= 32 else st["version"]
        else:
            c = _fixed_code(res.get("error"), "E_DATASET")
            st["errors"][c] = st["errors"].get(c, 0) + 1
        return res

    # ---- step 3
    def publish(self) -> dict:
        try:
            res = _call_compat(self.deps.publish, dict(self.scores), self.archive, self.registry_path,
                               goal=self.goal, models=list(self.models))
        except Exception as e:  # noqa: BLE001 -- ScoreboardError carries a fixed code in args[0]
            return {"ok": False, "error": _fixed_code(e.args[0] if e.args else None, "E_PUBLISH")}
        if isinstance(res, dict):
            return {k: v for k, v in res.items() if isinstance(v, bool) or _isnum(v)}
        return {"ok": True}

    def summary(self) -> dict:
        scores = [e["instigation_score"] for e in self.evals]
        codes, by_ch = {}, {}
        for e in self.evals:
            codes[e["trigger_code"]] = codes.get(e["trigger_code"], 0) + 1
            by_ch[e["channel"]] = max(by_ch.get(e["channel"], 0), e["n_writers"])
        ebm = self._elites_by_model()
        return {"ok": not any(k in self.errors for k in ("E_ARCHIVE",)),
                "goal": self.goal, "models": list(self.models),
                "coverage": self.archive.coverage(),
                "coverage_cells": self._coverage_cells(),
                "coverage_by_model": {m: len(ebm.get(m, {})) for m in self.models},
                "mean_elite_score": round(float(self.archive.mean_elite_score()), 12),
                "max_n_writers": max((e["n_writers"] for e in self.evals), default=0),
                "max_n_writers_by_channel": dict(sorted(by_ch.items())),
                "grid_channels": list(self.grid),
                "promoted_channels": list(self.promoted_now),
                "n_distinct_scores": len({round(s, 9) for s in scores}),
                "trigger_code_counts": dict(sorted(codes.items())),
                "n_unresolved": codes.get("UNRESOLVED", 0),
                "n_no_writers": codes.get("NO_WRITERS", 0),
                "n_classified_agents": self.n_classified_agents,
                "classifier_error_counts": dict(sorted(self.classifier_errors.items())),
                "panel_agreement_mean": (round(self.agreement_sum / self.agreement_n, 6)
                                         if self.agreement_n else None),
                "spend_cap_eur": self.spend_cap_eur,
                "spent_eur_by_model": {m: round(v, 6) for m, v in sorted(self.spent.items())},
                "providers_stopped": sorted(self.stopped),
                "n_skipped_spend": self.n_skipped_spend,
                "n_evaluated": len(self.evals),
                "dataset": {**self.dataset_stats, "errors": dict(sorted(self.dataset_stats["errors"].items()))},
                "errors": dict(sorted(self.errors.items()))}

    def run(self, *, generations=3, batch=4, bootstrap=False, pilot_variants=2, scratch_per_channel=1,
            nudge=True, publish=True) -> dict:
        if bootstrap:
            self.bootstrap(pilot_variants=pilot_variants, scratch_per_channel=scratch_per_channel)
        self.evaluate(self.pending(), 0)
        self.probe(0)
        self.collect_dataset(0)          # end of generation 0 (and so of a 0-generation run)
        for g in range(1, int(generations) + 1):
            self.generation(g, int(batch), nudge=nudge)
            self.probe(g)
            self.collect_dataset(g)      # end of every generation; the last one is the end of the run
        summ = self.summary()
        if publish:
            summ["publish"] = self.publish()
        return summ


# --------------------------------------------------------------------------- CLI

def _load_env(*slugs) -> None:
    """Load provider keys from the repo .env (orchestrate.load_env convention, with bootstrap's
    worktree fallback to the main checkout). Best-effort and silent: never prints a key; a missing
    key surfaces later as a fixed E_MODEL / E_EPISODE code."""
    for slug in dict.fromkeys(s for s in slugs if s):
        try:
            from study import bootstrap
            bootstrap._load_env(slug)
        except (Exception, SystemExit):  # noqa: BLE001
            pass


def _parse(argv):
    ap = argparse.ArgumentParser(description="MAP-Elites over board seeds (numbers / ids only)")
    ap.add_argument("--channels", default=",".join(DEFAULT_CHANNELS), help="grid channels")
    ap.add_argument("--probe-channels", default=",".join(DEFAULT_PROBES),
                    help="probe channels (top-k elites + pilot each generation; promoted on a first writer)")
    ap.add_argument("--probe-top-k", type=int, default=PROBE_TOP_K)
    ap.add_argument("--goal", choices=GOALS, default=DEFAULT_GOAL)
    ap.add_argument("--models", default=None,
                    help=f"agent models, comma-separated (default the active models {','.join(DEFAULT_MODELS)}; "
                         "priced ones need --spend-cap-eur; --dry-run runs them against a fake ledger)")
    ap.add_argument("--trigger-panel", default=",".join(DEFAULT_PANEL), help="classifier panel slugs (free only)")
    ap.add_argument("--allow-retired", action="store_true",
                    help="permit a retired model (config.MODELS 'retired') in --models / --trigger-panel / "
                         "--gen-model (refused with E_RETIRED otherwise)")
    ap.add_argument("--generations", type=int, default=3)
    ap.add_argument("--batch", type=int, default=4, help="children per generation")
    ap.add_argument("--budget", type=int, default=MIN_BUDGET)
    ap.add_argument("--agents", type=int, default=None, help="default config.MULTI_AGENTS")
    ap.add_argument("--workers-cerebras", type=int, default=None, help=f"default {DEFAULT_WORKERS['cerebras']}")
    ap.add_argument("--workers-berget", type=int, default=None, help=f"default {DEFAULT_WORKERS['berget']}")
    ap.add_argument("--workers", type=int, default=None, help="alias for --workers-cerebras")
    ap.add_argument("--bootstrap", action="store_true")
    ap.add_argument("--scratch-per-channel", type=int, default=1)
    ap.add_argument("--pilot-variants", type=int, default=2)
    ap.add_argument("--gen-model", default=None, help="mutation / bootstrap model (default config.DEFAULT_SLUG)")
    ap.add_argument("--allow-paid", action="store_true", help="pass allow_paid to episodes (a priced --gen-model is refused: E_GEN_PRICED)")
    ap.add_argument("--spend-cap-eur", type=float, default=None,
                    help="hard EUR ceiling on this invocation's priced episodes (default 0: priced models refused)")
    ap.add_argument("--cap-eur", type=float, default=None, help="ledger cap override (default the provider's)")
    ap.add_argument("--host-port-base", type=int, default=None)
    ap.add_argument("--child-channels", choices=("all", "target"), default="all")
    ap.add_argument("--no-nudge", action="store_true")
    ap.add_argument("--no-publish", action="store_true")
    ap.add_argument("--state", default=None)
    ap.add_argument("--archive", default=None, help="default: the goal's archive (goal_archive_path)")
    ap.add_argument("--registry", default=None)
    ap.add_argument("--no-dataset", action="store_true",
                    help="skip the per-generation training-data package rebuild (study/dataset.py)")
    ap.add_argument("--dry-run", action="store_true")
    return ap.parse_args(argv)


def _workers(a) -> dict:
    c = a.workers_cerebras if a.workers_cerebras is not None else a.workers
    return {"cerebras": c if c is not None else DEFAULT_WORKERS["cerebras"],
            "berget": a.workers_berget if a.workers_berget is not None else DEFAULT_WORKERS["berget"]}


def main(argv=None, out=None) -> int:
    out = out or sys.stdout
    a = _parse(argv)
    if a.budget < MIN_BUDGET:
        out.write(json.dumps({"ok": False, "error": "E_BUDGET"}) + "\n")
        return 2
    channels = _csv(a.channels)
    probes = _csv(a.probe_channels)
    panel = _csv(a.trigger_panel)
    kw = dict(generations=a.generations, batch=a.batch, bootstrap=a.bootstrap,
              pilot_variants=a.pilot_variants, scratch_per_channel=a.scratch_per_channel,
              nudge=not a.no_nudge, publish=not a.no_publish)
    common = dict(channels=channels, probe_channels=probes, probe_top_k=a.probe_top_k, goal=a.goal,
                  budget=a.budget, workers=_workers(a), trigger_panel=panel, allow_paid=a.allow_paid,
                  cap_eur=a.cap_eur, child_channels=a.child_channels, allow_retired=a.allow_retired, out=out)
    try:
        if a.dry_run:
            models = _csv(a.models) if a.models else list(DEFAULT_MODELS)  # includes priced Berget models
            with tempfile.TemporaryDirectory(prefix="qd-dry-") as tmp:
                tmp = Path(tmp)
                qd = QD(models=models, n_agents=a.agents or 4, gen_slug=a.gen_model or "qwen38",
                        spend_cap_eur=a.spend_cap_eur if a.spend_cap_eur is not None else DRY_SPEND_CAP_EUR,
                        host_port_base=a.host_port_base or 19000,
                        registry_path=tmp / "registry.json", candidates_dir=tmp / "candidates",
                        archive_path=tmp / "archive.json", state_path=tmp / "qd_state.json",
                        db=tmp / "obs.duckdb", deps=dry_run_deps(tmp),
                        dataset_out=None if a.no_dataset else tmp / "dataset", dataset_pilot=False, **common)
                summ = qd.run(**kw)
        else:
            try:
                models = [config.resolve_slug(m) for m in (_csv(a.models) if a.models else DEFAULT_MODELS)]
                gen = config.resolve_slug(a.gen_model) if a.gen_model else config.DEFAULT_SLUG
                common["trigger_panel"] = [config.resolve_slug(s) for s in panel]
            except ValueError:
                raise QDError("E_MODEL") from None
            _load_env(*models, gen, *common["trigger_panel"])
            with contextlib.redirect_stdout(sys.stderr):  # orchestrate progress prints -> stderr
                qd = QD(models=models, n_agents=a.agents or config.MULTI_AGENTS, gen_slug=gen,
                        spend_cap_eur=a.spend_cap_eur or 0.0,
                        host_port_base=a.host_port_base if a.host_port_base is not None else config.HOST_PORT_BASE,
                        registry_path=a.registry, archive_path=a.archive, state_path=a.state,
                        dataset_out=None if a.no_dataset else DATASET_AUTO, **common)
                summ = qd.run(**kw)
    except QDError as e:
        out.write(json.dumps({"ok": False, "error": e.args[0]}) + "\n")
        return 2
    except (Exception, SystemExit):  # noqa: BLE001 -- never echo a message
        out.write(json.dumps({"ok": False, "error": "E_QD"}) + "\n")
        return 1
    out.write(json.dumps(summ, sort_keys=True) + "\n")
    out.flush()
    return 0 if summ.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
