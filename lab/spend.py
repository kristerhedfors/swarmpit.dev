"""Spend ledger for priced providers (Berget): admission, reservation and settlement of runs.

One JSON file per provider (config.spend_dir()/<provider>.json) is shared by every orchestrator lane
and every git worktree: spend_dir() resolves the main checkout's runs/_spend (or $SCHIZ_SPEND_DIR), so
a run started from a worktree is admitted against the real spent total, never against a fresh ledger.
Each operation is one read-modify-write under an flock on the sibling .lock file, and the file is
replaced atomically. It holds numbers, statuses, pids and runs-dir paths only, never secrets or log text.

A run entry (keyed by run id) moves through
  planned   its batch block was admitted; the projection is committed
  reserved  run_one is about to create VMs (owner pid recorded)
  settled   logs collected; `eur` is the cost of the token usage they report
  aborted   a fatal provider error voided the run; re-keyed `<run_id>@<ts>` so a rerun starts fresh
and committed = eur of settled/aborted entries + projected_eur of planned/reserved ones. Nothing is
admitted or reserved that would take committed over the cap. Unsolvable runs are ordinary entries under
their own `-unsolvable` ids (and feed projections like solvable ones); their batch blocks are keyed
`unsolvable:<models>` so the two goals are admitted separately.
"""
import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

from config import BILL_REASONING_SEPARATELY, COST_PRIOR_TOKENS, MODELS, PROVIDERS, RUNS, price, spend_dir

LEDGER_VERSION = 1
PROJECTION_MARGIN = 1.2
# usage sources that are measured rather than projected; only these feed later projections
OBSERVED = ("steps", "summary", "mixed")
# A run whose prompt tokens are below this fraction of the prior (agents crashed or stalled at once)
# says nothing about what a full run costs, so it never lowers a projection.
DEGENERATE_FRACTION = 0.1
COMMITTED_EUR = ("settled", "aborted")
COMMITTED_PROJECTION = ("planned", "reserved")


class FatalProviderError(RuntimeError):
    """A run hit a non-retryable provider error (auth, credit): every lane must stop (RunVoid: this one)."""


class RunVoid(FatalProviderError):
    """Every agent of a run crashed (e.g. a rejected model id or request shape): this lane stops."""


class BudgetCapReached(RuntimeError):
    """The next run would take committed spend over the cap, or the ledger is halted."""


def cost(slug, u, bill_reasoning=BILL_REASONING_SEPARATELY):
    """EUR for token usage {prompt, completion, reasoning} at the model's price; 0 when unpriced.

    Reasoning is billed as output on top of completion unless the provider is known to include it
    there; a reasoning count above the completion count cannot be included, so it is always billed.
    """
    p_in, p_out = price(slug)
    if p_in is None or p_out is None:
        return 0.0
    prompt, completion, reasoning = (u.get(k) or 0 for k in ("prompt", "completion", "reasoning"))
    extra = reasoning if bill_reasoning or reasoning > completion else 0
    return (prompt * p_in + (completion + extra) * p_out) / 1e6


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _steps_usage(path):
    """Summed usage of the `model` records in one steps.jsonl; None if the file is missing.

    Parses numbers only: no record content is returned or printed.
    """
    try:
        fh = open(path, errors="replace")
    except OSError:
        return None
    tot = {"prompt": 0, "completion": 0, "reasoning": 0}
    with fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("kind") != "model" or not isinstance(rec.get("usage"), dict):
                continue
            u = rec["usage"]
            details = u.get("completion_tokens_details")
            tot["prompt"] += _num(u.get("prompt_tokens"))
            tot["completion"] += _num(u.get("completion_tokens"))
            tot["reasoning"] += _num(u.get("reasoning_tokens")) or (
                _num(details.get("reasoning_tokens")) if isinstance(details, dict) else 0)
    return tot


def _summary_usage(path):
    """Token counts from a summary.json (normal or crash summary); None if absent."""
    try:
        t = json.loads(Path(path).read_text()).get("tokens")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(t, dict):
        return None
    return {"prompt": _num(t.get("prompt")), "completion": _num(t.get("completion")), "reasoning": 0}


def run_usage(out):
    """Token usage of a collected run dir: per agent, steps.jsonl model records, else summary.tokens.

    `measured` counts agents with either source; settle() charges the rest their projection.
    """
    tot = {"prompt": 0, "completion": 0, "reasoning": 0}
    sources, measured = set(), 0
    for d in sorted(p for p in Path(out).glob("a*") if p.is_dir()):
        u, src = _steps_usage(d / "steps.jsonl"), "steps"
        if u is None:
            u, src = _summary_usage(d / "summary.json"), "summary"
        if u is None:
            continue
        measured += 1
        sources.add(src)
        for k in tot:
            tot[k] += u[k]
    source = "none" if not sources else "mixed" if len(sources) > 1 else sources.pop()
    return {**tot, "measured": measured, "usage_source": source}


def _alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Ledger:
    """The provider's spend ledger; `cap` defaults to the provider's spend_cap_eur and `path` to
    config.spend_dir(runs_dir)/<provider>.json (the main checkout's ledger for the repo's runs dir)."""

    def __init__(self, path=None, cap=None, provider="berget", runs_dir=None):
        spec = PROVIDERS[provider]
        self.provider = provider
        self.path = Path(path or spend_dir(runs_dir) / f"{provider}.json")
        self.lock_path = self.path.with_suffix(".lock")
        self.cap = float(spec["spend_cap_eur"] if cap is None else cap)
        self.runs_dir = Path(runs_dir) if runs_dir is not None else RUNS
        if spec.get("credit_eur") is not None and self.cap > spec["credit_eur"]:
            raise ValueError(f"cap €{self.cap:.2f} exceeds the {provider} credit €{spec['credit_eur']:.2f}")

    # ---- storage -------------------------------------------------------------------------------

    def _new_state(self):
        return {"version": LEDGER_VERSION, "provider": self.provider,
                "credit_eur": PROVIDERS[self.provider].get("credit_eur"), "cap_eur": self.cap,
                "prices": {s: list(price(s)) for s, m in MODELS.items() if m["provider"] == self.provider},
                "halted": None, "blocks": {}, "runs": {}}

    @contextmanager
    def _txn(self):
        """Locked read-modify-write; the state is saved only if the body does not raise."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                st = json.loads(self.path.read_text()) if self.path.exists() else self._new_state()
                if st.get("version") != LEDGER_VERSION:
                    raise RuntimeError(f"{self.path.name}: unknown ledger version {st.get('version')!r}")
                self._recover(st)
                yield st
                st["cap_eur"] = self.cap
                tmp = self.path.with_suffix(".json.tmp")
                with open(tmp, "w") as fh:
                    json.dump(st, fh, indent=1)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)

    def _run_dir(self, run_id, runs_dir=None):
        """The run's dir, or its archived copy under _aborted/, or None. `runs_dir` (the reserving
        process's, recorded in its entry: a worktree's runs tree) is searched instead of this one's."""
        runs = Path(runs_dir) if runs_dir else self.runs_dir
        d = runs / run_id
        if d.is_dir():
            return d
        pat = re.compile(re.escape(run_id) + r"-(\d{3}|fatal|crashed)(-\d+)?")
        arch = runs / "_aborted"
        hits = sorted(p for p in arch.iterdir() if pat.fullmatch(p.name)) if arch.is_dir() else []
        return hits[-1] if hits else None

    def _recover(self, st):
        """Settle reservations whose owner died: from whatever logs reached the run dir, else at projection."""
        for e in st["runs"].values():
            if e["status"] == "reserved" and not _alive(e.get("pid")):
                d = self._run_dir(e["run_id"], e.get("runs_dir"))
                self._settle_entry(e, run_usage(d) if d else {"measured": 0, "usage_source": "none"}, True)
                e["recovered"] = True

    # ---- accounting ----------------------------------------------------------------------------

    @staticmethod
    def _committed(st):
        return sum((e["eur"] or 0) if e["status"] in COMMITTED_EUR else
                   (e["projected_eur"] or 0) if e["status"] in COMMITTED_PROJECTION else 0
                   for e in st["runs"].values())

    @staticmethod
    def _projected(st, slug, n):
        """Projected EUR of one (slug, n)-agent run.

        With >= 2 observed runs of this model at n: margin x their max. Else the larger of margin x
        a single own run and the fallback: margin x the max of other models' observed runs at n
        (their tokens at this model's price), else the per-agent prior. Tagged runs (smoke) and
        degenerate runs (prompt tokens < DEGENERATE_FRACTION of the prior) never feed a projection.
        """
        floor = DEGENERATE_FRACTION * n * COST_PRIOR_TOKENS["prompt"]
        obs = [e for e in st["runs"].values()
               if e["status"] == "settled" and e["n_agents"] == n and e.get("usage_source") in OBSERVED
               and e["run_id"].endswith(f"--{e['slug']}") and (e.get("prompt") or 0) >= floor]
        own = [e["eur"] for e in obs if e["slug"] == slug]
        if len(own) >= 2:
            return round(PROJECTION_MARGIN * max(own), 6)
        if any(e["slug"] != slug for e in obs):
            base = PROJECTION_MARGIN * max(cost(slug, e) for e in obs if e["slug"] != slug)
        else:
            base = cost(slug, {k: n * v for k, v in COST_PRIOR_TOKENS.items()})
        return round(max([base, *(PROJECTION_MARGIN * x for x in own)]), 6)

    @staticmethod
    def _settle_entry(e, usage, started):
        """Price the usage; agents without any usage record (logs lost) are charged their projection."""
        n = e["n_agents"]
        missing = max(n - usage.get("measured", 0), 0) if started else 0
        src = usage.get("usage_source", "none") if started else "not-started"
        if missing:
            src = "projection" if src == "none" else f"{src}+projection"
        e.update(status="settled", usage_source=src,
                 eur=round(cost(e["slug"], usage) + missing * (e["projected_eur"] or 0) / n, 6),
                 **{k: usage.get(k, 0) for k in ("prompt", "completion", "reasoning")})

    @staticmethod
    def _entry(run_id, slug, n, rep, status, projected, block=None):
        return {"run_id": run_id, "slug": slug, "n_agents": n, "rep": rep, "status": status,
                "projected_eur": projected, "eur": None, "prompt": None, "completion": None, "reasoning": None,
                "usage_source": None, "pid": None, "ts": time.time(), **({"block": block} if block else {})}

    # ---- public API ----------------------------------------------------------------------------

    def committed(self):
        with self._txn() as st:
            return self._committed(st)

    def projected(self, slug, n):
        with self._txn() as st:
            return self._projected(st, slug, n)

    @property
    def halted(self):
        with self._txn() as st:
            return st["halted"]

    def state(self):
        """A snapshot of the ledger (numbers and statuses only)."""
        with self._txn() as st:
            return json.loads(json.dumps(st))

    def admit_block(self, key, rep, plan):
        """Decide block `rep` of the batch `key` once, for every lane: "admitted" or "rejected".

        `plan` lists (run_id, slug, n_agents) for every cell of every model in the block. The block is
        admitted iff committed + the projections of its not-yet-ledgered runs fit under the cap; the
        runs then become `planned`. A decided block returns its first decision ("truncated" if a
        reservation in it was refused later).
        """
        err = None
        with self._txn() as st:
            blk = st["blocks"].setdefault(key, {})
            r = str(rep)
            if r in blk:
                return blk[r]
            if st["halted"]:
                err = BudgetCapReached(f"at block r{rep}: ledger halted ({st['halted']['reason']})")
            else:
                new = {rid: (slug, n) for rid, slug, n in plan if rid not in st["runs"]}
                proj = {rid: self._projected(st, slug, n) for rid, (slug, n) in new.items()}
                ok = self._committed(st) + sum(proj.values()) <= self.cap + 1e-9
                blk[r] = "admitted" if ok else "rejected"
                if ok:
                    for rid, (slug, n) in new.items():
                        st["runs"][rid] = self._entry(rid, slug, n, rep, "planned", proj[rid], block=key)
                decision = blk[r]
        if err:
            raise err
        return decision

    def reserve(self, run_id, slug, n, rep):
        """Mark the run reserved by this process, or raise BudgetCapReached.

        Refused when the ledger is halted or committed (minus this run's own planned projection) plus
        its current projection exceeds the cap; a refused planned run is dropped and its block marked
        truncated. A settled or aborted entry of the same id (a rerun) is kept under `<run_id>@<ts>`.
        """
        err = None
        with self._txn() as st:
            e = st["runs"].get(run_id)
            if st["halted"]:
                err = BudgetCapReached(f"at {run_id}: ledger halted ({st['halted']['reason']})")
            elif e and e["status"] == "reserved":
                err = RuntimeError(f"{run_id} is already reserved by pid {e['pid']}")
            else:
                if e and e["status"] != "planned":
                    st["runs"][f"{run_id}@{e['ts']}"] = st["runs"].pop(run_id)
                    e = None
                planned = e["projected_eur"] if e else 0.0
                proj = self._projected(st, slug, n)
                rest = self._committed(st) - planned
                if rest + proj > self.cap + 1e-9:
                    if e:
                        del st["runs"][run_id]
                        if e.get("block"):
                            st["blocks"].setdefault(e["block"], {})[str(e["rep"])] = "truncated"
                    err = BudgetCapReached(f"at {run_id}: committed €{rest:.2f} + projected €{proj:.2f} "
                                           f"> cap €{self.cap:.2f}")
                else:
                    st["runs"][run_id] = {**(e or self._entry(run_id, slug, n, rep, "reserved", proj)),
                                          "status": "reserved", "projected_eur": proj,
                                          "pid": os.getpid(), "ts": time.time(),
                                          "runs_dir": str(self.runs_dir.resolve())}
        if err:
            raise err
        return proj

    def settle(self, run_id, usage, started=True, aborted=False):
        """Settle this process's reservation from run_usage(); `started=False` (no harness ever ran)
        charges only the usage found. An aborted run is re-keyed `<run_id>@<ts>`. Returns eur."""
        with self._txn() as st:
            e = st["runs"].get(run_id)
            if not e or e["status"] != "reserved" or e.get("pid") != os.getpid():
                return None
            self._settle_entry(e, usage, started)
            if aborted:
                e["status"] = "aborted"
                st["runs"][f"{run_id}@{e['ts']}"] = st["runs"].pop(run_id)
            return e["eur"]

    def halt(self, reason):
        """Stop every lane: reserve and admit_block refuse until clear_halt()."""
        with self._txn() as st:
            st["halted"] = {"reason": reason, "pid": os.getpid(), "ts": time.time()}

    def clear_halt(self):
        with self._txn() as st:
            st["halted"] = None
