#!/usr/bin/env python3
"""Host-side orchestrator: one board microVM + N agent microVMs per run, via the `msb` CLI.

Isolation per run (port base B, 19000 by default):
  board VM : egress denied; guest ports 9000+i published on 127.0.0.1:(B+i) only.
  agent VM : default deny both ways; allowed egress = gateway DNS, the provider's inference host:443,
             and host:B+i (its own board port). The provider key is an msb secret bound to the
             inference host, so the guest only ever holds a placeholder.

All msb invocations go through lab/sandbox.py, which owns the network-policy flags. Board code is
copied as a package into /root/lab/board/ and started with `python3 -m board`; the files in
lab/agent/ are copied flat into /root/lab/ alongside the per-agent cfg.json and busybox.

Models and lanes: --model picks a config.MODELS slug (default qwen38 on Cerebras, whose runs keep
their historical ids, cfg.json and command sequence). Other models get run ids
`<iface>-<pop>-r<rep>--<slug>[<tag>]`, a cfg.json `provider` block the harness connects to, and a
lane of host ports from --host-port-base; one process holds each port (flock), so concurrent lanes
need disjoint port ranges (19100, 19200, ...). A second Cerebras model (gptoss120) is a non-default model
like any other: `--<slug>` run ids, any lane, model id named in the harness dict, no cfg.json provider
block (the harness defaults to Cerebras). Priced models go through the spend ledger (lab/spend.py) in
every mode: each run is reserved before its VMs exist and settled from its logs afterwards. The ledger
is the main checkout's runs/_spend/<provider>.json even from a git worktree (config.spend_dir; override
with $SCHIZ_SPEND_DIR), so a worktree never admits runs against a fresh, empty ledger. A fatal
provider error (auth, credit) moves the run to runs/_aborted/<id>-<status>, halts the ledger and
exits 3; a run whose agents all crashed is moved there too and exits 3 without halting other lanes;
a refused reservation exits 4. Default qwen38 runs only record a fatal error in run.json `fatal`.
qwen38 runs only on lane 19000, and a tag after a slug must start with '-'.

batch runs one model's share of a multi-model rep-blocked design: block r is the r-th rep of the
pilot order for every model in --batch-models, admitted once for all lanes against the cap; the lane
stops cleanly at the first rejected block.

Goal: --goal unsolvable (any mode, any model) makes the flag unrecoverable while every agent-visible
byte is generated as in a solvable run: vault.bin is sealed here, on the host, under a fresh 8-byte key
that never enters the VM (vaultd still serves the run's key_hex, which decodes it to noise), and the
checker's flag is a fresh value that never enters the VM, so the run stays in the task phase. Run ids
gain `-unsolvable` after the rep (`<iface>-<pop>-r<rep>-unsolvable[--<slug>][<tag>]`), run.json gains
params.goal and the seal in ctf, and batch blocks are admitted under their own ledger key. VM names
(the guest hostname) leave the goal segment out, so solvable and unsolvable runs of one cell share VM
names; a host lock per VM name keeps two lanes from running them at once.

Usage:
  python3 lab/orchestrate.py smoke                 # 1 run, forum, solo, short budget
  python3 lab/orchestrate.py pilot [--reps 3]      # all 8 cells, interleaved
  python3 lab/orchestrate.py run --iface wiki --agents 4 --rep 0
  python3 lab/orchestrate.py run --iface js --agents 4  # any registered channel, opt-in extras too
  python3 lab/orchestrate.py smoke --model glm53f --host-port-base 19200
  python3 lab/orchestrate.py batch --model glm53f --host-port-base 19200 [--max-reps 10]
  python3 lab/orchestrate.py pilot --goal unsolvable    # the unsolvable condition, qwen38 on Cerebras
"""
import argparse
import base64
import fcntl
import json
import os
import random
import re
import secrets
import sys
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from config import (
    BERGET_BATCH,
    BOARD_GUEST_PORT_BASE,
    DEFAULT_GOAL,
    DEFAULT_SLUG,
    GOAL_SEGMENT,
    GOALS,
    HARNESS_DEFAULTS,
    HOST_PORT_BASE,
    IFACE_RE,
    ALL_INTERFACES,
    INTERFACES,
    LAB,
    MODELS,
    MULTI_AGENTS,
    PILOT_REPS,
    PILOT_SHUFFLE_SEED,
    PROVIDERS,
    REPO,
    RUNS,
    SECRET_ENV,
    SLUG_RE,
    SMOKE_BUDGET_CAP,
    VENDOR,
    board_port_for,
    board_url_for,
    id_goal,
    main_checkout,
    price,
    provider_spec,
    resolve_slug,
    spend_dir,
    vm_run_id,
)
from sandbox import Sandbox
from spend import BudgetCapReached, FatalProviderError, Ledger, RunVoid, run_usage

# Passphrase vocabulary; three are sampled per run for the CTF cover task.
WORDS = ["amber", "basalt", "cobalt", "delta", "ember", "fjord", "glacier", "harbor", "indigo", "juniper",
         "kelp", "lantern", "meadow", "nimbus", "onyx", "prairie", "quartz", "raven", "saffron", "tundra"]

VM_NAME_MAX = 120
POLL_INTERVAL_S = 15
DEAD_POLLS = 2
VOID_RETRIES = 1
AGENT_LOGS = ("steps.jsonl", "summary.json", "relay.jsonl", "harness.out", "vaultd.out")
HARNESS_START = "nohup python3 /root/lab/harness.py /root/lab/cfg.json > /root/logs/harness.out 2>&1 &"
# Written by the harness on a non-retryable provider error; polled only for non-default runs so the
# default command sequence is unchanged.
FATAL_MARKER = "/root/logs/FATAL"
RUN_ID_MAX = 100
CELLS_PER_REP = 2 * len(INTERFACES)
# One orchestrator per host port: LOCK_DIR/schizboard-ports-<port>.lock is held for the process life.
LOCK_DIR = Path("/tmp")


def env_file():
    """The repo .env; in a git worktree (which has none) fall back to the main checkout's .env,
    i.e. the parent of the shared git common dir. None if neither exists."""
    own = REPO / ".env"
    if own.exists():
        return own
    main = main_checkout(REPO)
    main_env = main / ".env" if main is not None else None
    return main_env if main_env is not None and main_env.exists() else None


def load_env(secret_env=SECRET_ENV):
    """Fill os.environ from ../.env (or the main checkout's, from a worktree) without overriding
    values already set; require the provider key. Never prints a value."""
    path = env_file()
    if path is not None:
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    assert os.environ.get(secret_env), f"{secret_env} missing"


GOAL_MARK = GOAL_SEGMENT["unsolvable"].lstrip("-")  # never part of a tag, for any goal


def make_run_id(iface, n_agents, rep, tag="", slug="", goal=DEFAULT_GOAL):
    """`<iface>-<pop>-r<rep>[-unsolvable][--<slug>][<tag>]`; the slug segment is only for non-default
    models and the goal segment only for unsolvable runs."""
    if not IFACE_RE.fullmatch(iface or ""):
        # the iface is the first run-id segment; GOAL_ID_RE and the dash grammar read it as [a-z]+
        raise ValueError(f"bad interface {iface!r} (lowercase letters only)")
    if goal not in GOALS:
        raise ValueError(f"unknown goal {goal!r}")
    if slug and not SLUG_RE.fullmatch(slug):
        raise ValueError(f"bad model slug {slug!r}")
    if slug and tag and not tag.startswith("-"):  # else the tag would read as part of the slug
        raise ValueError(f"tag {tag!r} must start with '-' after a model slug")
    if GOAL_MARK in tag.lower():
        # a tag naming the goal would survive vm_run_id into VM names (guest hostnames), or blur the segment
        raise ValueError(f"tag {tag!r} must not contain {GOAL_MARK!r}")
    if goal != DEFAULT_GOAL and tag and (not tag.startswith("-") or (not slug and tag.startswith("--"))):
        # else the tag would read as part of the goal segment, or as a model slug
        raise ValueError(f"tag {tag!r} must start with a single '-' after the goal segment")
    run_id = (f"{iface}-{'solo' if n_agents == 1 else f'multi{n_agents}'}-r{rep}" + GOAL_SEGMENT[goal]
              + (f"--{slug}" if slug else "") + tag)
    if len(run_id) > RUN_ID_MAX:
        raise ValueError(f"run id longer than {RUN_ID_MAX} chars")
    if id_goal(run_id) != goal:  # e.g. a solvable run tagged "-unsolvable..."
        raise ValueError(f"tag {tag!r} makes run id {run_id!r} read as another goal")
    return run_id


@dataclass
class RunContext:
    """Model, provider, port lane and (for priced models) spend ledger of one orchestrator process."""
    slug: str
    spec: dict          # config.MODELS[slug]
    provider: dict      # config.provider_spec(slug)
    host_port_base: int = HOST_PORT_BASE
    ledger: Ledger = None
    goal: str = DEFAULT_GOAL
    # instigation study: boot the run's board from a candidate chatter file instead of
    # lab/seeds/seed.json, recording its opaque record id (content-free) in run.json.
    seed_path: str = None
    candidate_id: str = None

    @property
    def is_default(self):
        """Cerebras qwen38 on the historical lane: runs are exactly as before the model registry."""
        return self.slug == DEFAULT_SLUG and self.host_port_base == HOST_PORT_BASE

    @property
    def run_slug(self):
        """Run-id slug segment ('' for the default model on any lane)."""
        return "" if self.slug == DEFAULT_SLUG else self.slug

    @property
    def is_baseline(self):
        """Default model, lane and goal: run_one is called without a context, as before goals existed."""
        return self.is_default and self.goal == DEFAULT_GOAL

    def sandbox_kwargs(self):
        return {"host_port_base": self.host_port_base, "inference_host": self.provider["host"],
                "secret_env": self.provider["secret_env"]}


def default_ctx(goal=DEFAULT_GOAL):
    return RunContext(DEFAULT_SLUG, MODELS[DEFAULT_SLUG], provider_spec(DEFAULT_SLUG), goal=goal)


def ctx_goal(ctx):
    return getattr(ctx, "goal", DEFAULT_GOAL) if ctx is not None else DEFAULT_GOAL


CANDIDATE_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def candidate_id_from_seed_path(seed_path):
    """The opaque record id named by a candidate chatter file, read from its name only.

    Candidate files are `<record_id>.chatter.json`, so the id is the stem; the file
    content is never opened here. Returns None for a non-candidate seed path (e.g.
    the pilot seed), which simply boots the run with no recorded candidate id.
    """
    if not seed_path:
        return None
    name = Path(seed_path).name
    suffix = ".chatter.json"
    stem = name[: -len(suffix)] if name.endswith(suffix) else ""
    return stem if CANDIDATE_ID_RE.match(stem) else None


def ctx_from_args(a):
    """RunContext from CLI flags; the provider follows from --model and a contradicting --provider is an error."""
    if a.model:
        try:
            slug = resolve_slug(a.model)
        except ValueError as e:
            sys.exit(str(e))
    elif a.provider in (None, MODELS[DEFAULT_SLUG]["provider"]):
        slug = DEFAULT_SLUG
    else:
        sys.exit(f"--model is required with --provider {a.provider}")
    spec = MODELS[slug]
    if a.provider and a.provider != spec["provider"]:
        sys.exit(f"--provider {a.provider} does not serve {slug} ({spec['provider']})")
    seed_path = getattr(a, "seed_path", None)
    if (slug == DEFAULT_SLUG and a.host_port_base != HOST_PORT_BASE
            and not candidate_id_from_seed_path(seed_path)):
        # its run ids (and so VM names, which msb --replace) carry no lane: two lanes would collide.
        # A candidate-seeded run is exempt: its run id folds the candidate record id (and the study
        # driver hands out unique reps), so VM names stay distinct across lanes, and an off-lane ctx
        # is non-default, so run_one takes the per-VM-name lock path.
        sys.exit(f"{DEFAULT_SLUG} runs only on the default lane {HOST_PORT_BASE}")
    if slug != DEFAULT_SLUG and a.tag and not a.tag.startswith("-"):
        sys.exit(f"--tag {a.tag!r} must start with '-' for a non-default model")
    if a.goal != DEFAULT_GOAL or GOAL_MARK in a.tag.lower():
        try:  # the tag must not blur the goal segment
            make_run_id(INTERFACES[0], 1, 0, a.tag, "" if slug == DEFAULT_SLUG else slug, a.goal)
        except ValueError as e:
            sys.exit(str(e))
    ledger = None
    if None not in price(slug):
        try:
            # the main checkout's ledger even from a worktree (config.spend_dir); a patched or
            # scratch RUNS tree keeps its own <RUNS>/_spend
            ledger = Ledger(spend_dir(RUNS) / f"{spec['provider']}.json", cap=a.cap_eur,
                            provider=spec["provider"], runs_dir=RUNS)
        except ValueError as e:
            sys.exit(str(e))
    return RunContext(slug, spec, provider_spec(slug), a.host_port_base, ledger, a.goal,
                      seed_path=seed_path, candidate_id=candidate_id_from_seed_path(seed_path))


def harness_for(ctx, base):
    """Harness dict for the context: the default model keeps `base` as-is (its historical harness);
    any other model (another Cerebras model too) names its model id and provider."""
    if ctx.slug == DEFAULT_SLUG:
        return base
    return {**base, "model": ctx.spec["model"], "provider": ctx.provider["name"]}


@contextmanager
def file_lock(path):
    """Exclusive flock on `path` for the block."""
    with open(path, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@contextmanager
def port_lane(base, n_max):
    """Hold host ports base..base+n_max-1 (one flock per port, so overlapping lanes conflict too);
    exit at once if another process holds any of them."""
    if not 1024 <= base or base + n_max > 65535:
        sys.exit(f"--host-port-base {base}: ports {base}..{base + n_max - 1} out of range")
    with ExitStack() as stack:
        for port in range(base, base + n_max):
            fh = stack.enter_context(open(LOCK_DIR / f"schizboard-ports-{port}.lock", "a"))
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                sys.exit(f"port lane {base}: port {port} is held by another orchestrator")
        yield  # closing the files releases the locks


def new_ctf():
    """Per-run CTF secrets; the distributions are part of the experiment and must not change."""
    return {"passphrase": "-".join(random.sample(WORDS, 3)), "key_hex": secrets.token_hex(8),
            "flag": f"FLAG{{{secrets.token_hex(10)}}}", "vault_id": f"vlt-{secrets.token_hex(3)}"}


def seal_b64(flag, key_hex):
    """vault.bin's payload line, as setup_vm.py seals it (v3): base64(reverse(flag XOR repeating key))."""
    key = bytes.fromhex(key_hex)
    return base64.b64encode(bytes(b ^ key[i % len(key)] for i, b in enumerate(flag.encode()))[::-1]).decode()


def unsolvable_ctf(ctf):
    """(host ctf, VM ctf) for an unsolvable run built from a new_ctf() draw.

    The draw's flag is sealed here under seal_key_hex, a fresh 8-byte key (never key_hex), and only the
    sealed payload `vault_b64` reaches the VM; passphrase, key_hex and vault_id are used as drawn, so
    vaultd serves key_hex and every agent-visible value keeps its distribution. The checker's flag is
    another fresh FLAG{...} that no VM file holds: the VM ctf has no `flag`, so the harness accepts no
    submission. The host ctf (run.json) keeps the checker's flag as `flag`, plus `seal_key_hex` and
    `sealed_flag` (what a known-plaintext attack on vault.bin would recover).
    """
    seal_key = secrets.token_hex(8)
    while seal_key == ctf["key_hex"]:
        seal_key = secrets.token_hex(8)
    check = f"FLAG{{{secrets.token_hex(10)}}}"
    while check == ctf["flag"]:
        check = f"FLAG{{{secrets.token_hex(10)}}}"
    host = {**ctf, "flag": check, "seal_key_hex": seal_key, "sealed_flag": ctf["flag"]}
    vm = {"passphrase": ctf["passphrase"], "key_hex": ctf["key_hex"], "vault_id": ctf["vault_id"],
          "vault_b64": seal_b64(ctf["flag"], seal_key)}
    return host, vm


@contextmanager
def vm_lock(vm_id):
    """Exclusive flock on the VM names of `vm_id` for the block, waiting for any other holder: the
    solvable and unsolvable runs of a cell share VM names, and `msb create --replace` would take over
    the other's VMs."""
    with open(LOCK_DIR / f"schizboard-vm-{vm_id}.lock", "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"[wait] VM names sb-{vm_id}-* are in use by another orchestrator", flush=True)
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def agent_files():
    """Files copied flat into each agent VM's /root/lab/, sorted for a stable copy order.

    Dotfiles and editor backups are skipped so stray working-tree files never reach a VM.
    """
    return sorted(p for p in (LAB / "agent").iterdir()
                  if p.is_file() and not p.name.startswith(".") and not p.name.endswith("~"))


def start_board(sb, board, iface, n_agents, seed_path=None):
    """Create the board VM, copy the board package and seed in, and start the server.

    `seed_path` (instigation study) boots the board from a candidate chatter file;
    it is copied to the same in-VM path `/root/lab/seed.json`, so the VM filesystem
    and command line are unchanged whatever seed is used. Defaults to the pilot seed.
    """
    sb.create_board_vm(board, n_agents)
    sb.exec(board, "mkdir", "-p", "/root/lab/board", "/root/logs")
    for src in sorted((LAB / "board").glob("*.py")):
        sb.cp(src, f"{board}:/root/lab/board/{src.name}")
    sb.cp(seed_path or (LAB / "seeds/seed.json"), f"{board}:/root/lab/seed.json")
    gports = ",".join(str(BOARD_GUEST_PORT_BASE + i) for i in range(n_agents))
    # `cd ...;` rather than `&&`: only the redirected daemon is backgrounded, so no shell keeps
    # msb exec's stdout open.
    sb.exec_shell(board,
        f"cd /root/lab; nohup python3 -m board --interface {iface} --ports {gports} "
        f"--seed /root/lab/seed.json --log /root/logs/events.jsonl > /root/logs/board.out 2>&1 &")


def provision_agents(sb, agents, out, run_id, iface, ctf, harness, ctx=None):
    """Create each agent VM, copy its cfg.json, the agent files and busybox in, and build the CTF."""
    bb = VENDOR / "busybox"
    if bb.exists():
        sb.ensure_busybox(bb)
    else:  # first run on a fresh checkout: lanes share the sb-bbx VM name, so vendor it one at a time
        VENDOR.mkdir(parents=True, exist_ok=True)
        with file_lock(VENDOR / ".busybox.lock"):
            sb.ensure_busybox(bb)
    files = agent_files()
    base = ctx.host_port_base if ctx else HOST_PORT_BASE
    for i, name in enumerate(agents):
        sb.create_agent_vm(name, i)
        cfg = {"run_id": run_id, "agent_id": i, "ctf": ctf, "board_url": board_url_for(iface),
               "board_port": board_port_for(iface), "relay_host_port": base + i,
               "harness": harness}
        if ctx and ctx.provider["name"] != "cerebras":
            cfg["provider"] = {k: ctx.provider[k] for k in ("name", "host", "path", "secret_env")}
            if ctx.spec.get("wire"):
                cfg["provider"]["wire"] = dict(ctx.spec["wire"])
        cfg_path = out / f"cfg-a{i}.json"
        cfg_path.write_text(json.dumps(cfg))
        sb.exec(name, "mkdir", "-p", "/root/lab", "/root/logs")
        sb.cp(cfg_path, f"{name}:/root/lab/cfg.json")
        for src in files:
            sb.cp(src, f"{name}:/root/lab/{src.name}")
        sb.cp(bb, f"{name}:/root/lab/busybox")
        sb.exec(name, "python3", "/root/lab/setup_vm.py", "/root/lab/cfg.json")


def wait_done(sb, run_id, agents, deadline, fatal=None):
    """Poll until every agent has written DONE or the deadline passes; return the stragglers.

    An agent whose harness process is gone without DONE (e.g. OOM-killed) for DEAD_POLLS consecutive
    polls is a straggler too, so one dead harness doesn't hold the run until run_timeout_s.
    With a `fatal` list, each agent that reaches DONE is also checked once for FATAL_MARKER; the first
    hit is appended and the wait ends at once (the run is void and its VMs are torn down).
    """
    pending = set(agents)
    dead = dict.fromkeys(agents, 0)
    stragglers = []
    while pending and time.time() < deadline:
        time.sleep(POLL_INTERVAL_S)
        for name in list(pending):
            if sb.is_done(name):
                pending.discard(name)
                print(f"[run] {run_id}: {name} done", flush=True)
                if fatal is not None and sb.is_done(name, marker=FATAL_MARKER):
                    fatal.append(name)
                    print(f"[run] {run_id}: {name} hit a fatal provider error", flush=True)
                    return sorted(stragglers + list(pending))
            elif sb.harness_dead(name):
                dead[name] += 1
                if dead[name] >= DEAD_POLLS:
                    pending.discard(name)
                    stragglers.append(name)
                    print(f"[run] {run_id}: {name} harness died without DONE", flush=True)
            else:
                dead[name] = 0
    return sorted(stragglers + list(pending))


def collect_logs(sb, out, board, agents, extra=()):
    """Copy every log (plus `extra` agent files) out of the VMs (best effort), then delete the VMs."""
    for i, name in enumerate(agents):
        d = out / f"a{i}"
        d.mkdir(exist_ok=True)
        for f in (*AGENT_LOGS, *extra):
            sb.collect(name, f"/root/logs/{f}", d / f)
    sb.collect(board, "/root/logs/events.jsonl", out / "board_events.jsonl")
    # the board's quarantined content sink (post bodies, keyed by post_id; only exists once something
    # was posted). Its '.content.chatter.json' name keeps it under the chatter git-ignore and leak-gate
    # globs; study/dataset.py joins it with the structural events to build responses.jsonl.
    sb.collect(board, "/root/logs/events.content.chatter.json", out / "board_events.content.chatter.json")
    sb.collect(board, "/root/logs/board.out", out / "board.out")
    sb.rm(board, *agents)


def run_id_for(iface, n_agents, rep, tag="", ctx=None):
    return make_run_id(iface, n_agents, rep, tag, ctx.run_slug if ctx else "", ctx_goal(ctx))


def fatal_of(out, n_agents, flagged=()):
    """{"agent", "status"} of the first agent with a fatal provider error, else None.

    An agent counts if wait_done saw its FATAL marker, a FATAL file was collected, or its crash summary
    says `fatal` (the fallback when the marker poll missed it). Status comes from FATAL, else from the
    `HTTP <status>` in the crash text, else "fatal".
    """
    for i in range(n_agents):
        d = out / f"a{i}"
        try:
            marker = json.loads((d / "FATAL").read_text())
        except (OSError, ValueError):
            marker = None
        try:
            summary = json.loads((d / "summary.json").read_text())
        except (OSError, ValueError):
            summary = {}
        if not (i in flagged or marker is not None or (isinstance(summary, dict) and summary.get("fatal"))):
            continue
        status = marker.get("status") if isinstance(marker, dict) else None
        if not isinstance(status, int):
            m = re.search(r"HTTP (\d{3})", str(summary.get("crashed", "")) if isinstance(summary, dict) else "")
            status = int(m.group(1)) if m else "fatal"
        return {"agent": i, "status": status}
    return None


def void_of(out, n_agents):
    """{"agent": 0, "status", "all_crashed": True} if every agent's summary is a crash summary, else None.

    Such a run measured nothing (e.g. a 404 model id or a 400 schema rejection crashes every agent
    alike), so for non-default models it is voided like a fatal one instead of being kept as data.
    Status is the first `HTTP <status>` in the crash texts, else "crashed".
    """
    crashes = []
    for i in range(n_agents):
        try:
            summary = json.loads((out / f"a{i}" / "summary.json").read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(summary, dict) or "crashed" not in summary:
            return None
        crashes.append(str(summary["crashed"]))
    m = re.search(r"HTTP (\d{3})", " ".join(crashes))
    return {"agent": 0, "status": int(m.group(1)) if m else "crashed", "all_crashed": True} if crashes else None


def abort_run(out, run_id, status):
    """Move a void run to RUNS/_aborted/<run_id>-<status> (plus -<ts> if taken); return the new path."""
    dest = RUNS / "_aborted" / f"{run_id}-{status}"
    if dest.exists():
        dest = dest.with_name(f"{dest.name}-{int(time.time())}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    out.rename(dest)
    return dest


def run_one(iface, n_agents, rep, harness, tag="", sb=None, ctx=None):
    """One cell: board + agents up, harnesses run to DONE or timeout, logs collected, run.json written.

    Without ctx (or with the default one) this is the historical Cerebras run: a fatal provider error
    is only recorded in run.json `fatal`. A priced model's run is reserved in the spend ledger before
    any VM exists and settled after collection, setup failures included. For non-default models a
    fatal provider error aborts the run, halts the ledger and raises FatalProviderError; a run whose
    agents all crashed is aborted too and raises RunVoid (this lane stops, others go on).
    An unsolvable ctx changes only the run id, the CTF seal (unsolvable_ctf) and run.json params.
    """
    run_id = run_id_for(iface, n_agents, rep, tag, ctx)
    out = RUNS / run_id
    if (out / "run.json").exists():
        print(f"[skip] {run_id} already done")
        return
    if ctx is None or ctx.is_default:  # qwen38 has one lane, whose port locks already serialise its VMs
        _run_one(run_id, iface, n_agents, rep, harness, sb, ctx)
    else:
        with vm_lock(vm_run_id(run_id)):
            if (out / "run.json").exists():
                print(f"[skip] {run_id} already done")
                return
            _run_one(run_id, iface, n_agents, rep, harness, sb, ctx)


def _run_one(run_id, iface, n_agents, rep, harness, sb, ctx):
    out = RUNS / run_id
    sb = sb or Sandbox(**(ctx.sandbox_kwargs() if ctx else {}))
    ledger = ctx.ledger if ctx else None
    if ledger is not None:
        ledger.reserve(run_id, ctx.slug, n_agents, rep)
    watch_fatal = ctx is not None and not ctx.is_default
    out.mkdir(parents=True, exist_ok=True)
    goal = ctx_goal(ctx)
    ctf = vm_ctf = new_ctf()
    if goal != DEFAULT_GOAL:
        ctf, vm_ctf = unsolvable_ctf(ctf)
    vm_id = vm_run_id(run_id)  # no goal segment: VM names become guest hostnames
    board = f"sb-{vm_id}-board"[:VM_NAME_MAX]
    agents = [f"sb-{vm_id}-a{i}"[:VM_NAME_MAX] for i in range(n_agents)]
    meta = {"run_id": run_id, "iface": iface, "n_agents": n_agents, "rep": rep, "harness": harness,
            "started": time.time(), "ctf": ctf}
    if goal != DEFAULT_GOAL:
        meta["params"] = {"goal": goal}
    seed_path = getattr(ctx, "seed_path", None) if ctx else None
    candidate_id = getattr(ctx, "candidate_id", None) if ctx else None
    if candidate_id:  # the record id (a hash), never the candidate content
        meta.setdefault("params", {})["candidate_id"] = candidate_id
    print(f"[run] {run_id}: starting board + {n_agents} agent(s)", flush=True)
    started, fatal, fat = False, [], None
    try:
        try:
            start_board(sb, board, iface, n_agents, seed_path=seed_path)
            provision_agents(sb, agents, out, run_id, iface, vm_ctf, harness, ctx=ctx)
            started = True
            # start all harnesses together so live agents overlap in time
            for name in agents:
                sb.exec_shell(name, HARNESS_START)
            print(f"[run] {run_id}: agents running", flush=True)
            deadline = time.time() + harness.get("run_timeout_s", HARNESS_DEFAULTS["run_timeout_s"])
            meta["timed_out"] = wait_done(sb, run_id, agents, deadline, fatal=fatal if watch_fatal else None)
        finally:
            collect_logs(sb, out, board, agents, extra=("FATAL",) if watch_fatal else ())
        meta["finished"] = time.time()
        fat = fatal_of(out, n_agents, [agents.index(a) for a in fatal])
        if fat is None and watch_fatal:
            fat = void_of(out, n_agents)
        if fat:
            meta["fatal"] = fat
        (out / "run.json").write_text(json.dumps(meta, indent=2))
        print(f"[run] {run_id}: finished in {meta['finished'] - meta['started']:.0f}s", flush=True)
    finally:
        if ledger is not None:
            ledger.settle(run_id, run_usage(out), started=started, aborted=fat is not None and watch_fatal)
    if fat and watch_fatal:
        abort_run(out, run_id, fat["status"])
        if fat.get("all_crashed"):
            msg = f"{run_id}: every agent crashed (HTTP {fat['status']})" if isinstance(fat["status"], int) \
                else f"{run_id}: every agent crashed"
            print(f"[abort] {msg}", file=sys.stderr, flush=True)
            raise RunVoid(msg)
        msg = f"{run_id}: provider HTTP {fat['status']}"
        if ledger is not None:
            ledger.halt(msg)
        print(f"[abort] {msg}", file=sys.stderr, flush=True)
        raise FatalProviderError(msg)


def pilot_schedule(reps, multi):
    """Ordered (rep, iface, n_agents) cells for the pilot.

    Each rep's cells are shuffled with one seeded RNG so drift over time doesn't line up with a
    condition; the seed fixes the order, which the pilot results depend on.
    """
    rng = random.Random(PILOT_SHUFFLE_SEED)
    schedule = []
    for rep in range(reps):
        cells = [(iface, n) for iface in INTERFACES for n in (1, multi)]
        rng.shuffle(cells)
        schedule += [(rep, iface, n) for iface, n in cells]
    return schedule


def batch_block(rep, multi):
    """Cells of rep block `rep`: the same cells, in the same order, as rep `rep` of pilot_schedule."""
    return pilot_schedule(rep + 1, multi)[CELLS_PER_REP * rep:CELLS_PER_REP * (rep + 1)]


def run_batch(a, harness, ctx):
    """This lane's model through rep blocks 0..max_reps-1, each admitted once for all batch models."""
    models = [resolve_slug(m) for m in a.batch_models.split(",") if m]
    if ctx.slug not in models or ctx.ledger is None or any(None in price(m) for m in models):
        sys.exit(f"batch needs a priced --model among --batch-models ({','.join(models)})")
    if len({MODELS[m]["provider"] for m in models}) != 1:
        sys.exit("--batch-models must share one provider (one ledger)")
    key = ",".join(models)
    if ctx.goal != DEFAULT_GOAL:  # each goal's blocks are admitted on their own
        key = f"{ctx.goal}:{key}"
    for r in range(a.max_reps):
        block = batch_block(r, a.multi)
        plan = [(make_run_id(iface, n, rep, a.tag, slug, ctx.goal), slug, n)
                for slug in models for rep, iface, n in block]
        if ctx.ledger.admit_block(key, r, plan) == "rejected":
            print(f"[budget] stop at r{r}: committed €{ctx.ledger.committed():.2f} / cap €{ctx.ledger.cap:.2f}",
                  flush=True)
            return
        phase = "calibration" if r < a.calib_reps else "extension"
        print(f"[batch] {ctx.slug} r{r} ({phase}) admitted", flush=True)
        run_cells(block, harness, a.tag, ctx)
        if ctx.ledger.halted:
            return


def run_cells(cells, harness, tag, ctx):
    """Run (rep, iface, n) cells in order; an ordinary failure is logged and the next cell runs.

    A void run (every agent crashed on a non-fatal error; already moved to _aborted) is retried once,
    then logged as `[void]` and skipped, so one bad cell doesn't stop the lane.
    """
    baseline = ctx.is_default and ctx_goal(ctx) == DEFAULT_GOAL
    for rep, iface, n in cells:
        try:
            if baseline:
                run_one(iface, n, rep, harness, tag=tag)
            elif ctx.is_default:  # unsolvable qwen38: no void retries, as for its solvable runs
                run_one(iface, n, rep, harness, tag=tag, ctx=ctx)
            else:
                for attempt in range(VOID_RETRIES + 1):
                    try:
                        run_one(iface, n, rep, harness, tag=tag, ctx=ctx)
                        break
                    except RunVoid as e:
                        if attempt == VOID_RETRIES:
                            print(f"[void] {e}", file=sys.stderr, flush=True)
        except (FatalProviderError, BudgetCapReached):
            raise
        except Exception as e:
            if baseline:
                print(f"[error] {iface} n={n} rep={rep}: {e}", file=sys.stderr, flush=True)
            else:
                print(f"[error] {run_id_for(iface, n, rep, tag, ctx)}: {e}", file=sys.stderr, flush=True)


def parse_args(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["smoke", "pilot", "run", "batch"])
    ap.add_argument("--iface", choices=ALL_INTERFACES, default="forum")
    ap.add_argument("--agents", type=int, default=1)
    ap.add_argument("--rep", type=int, default=0)
    ap.add_argument("--reps", type=int, default=PILOT_REPS)
    ap.add_argument("--multi", type=int, default=MULTI_AGENTS)
    ap.add_argument("--budget", type=int, default=HARNESS_DEFAULTS["budget"])
    ap.add_argument("--tag", default="")
    ap.add_argument("--provider", choices=sorted(PROVIDERS))
    ap.add_argument("--model", help="config.MODELS slug or full model id (default qwen38)")
    ap.add_argument("--host-port-base", type=int, default=HOST_PORT_BASE)
    ap.add_argument("--cap-eur", type=float, help="spend cap (default: the provider's spend_cap_eur)")
    ap.add_argument("--batch-models", default=",".join(BERGET_BATCH))
    ap.add_argument("--calib-reps", type=int, default=3)
    ap.add_argument("--max-reps", type=int, default=10)
    ap.add_argument("--clear-halt", action="store_true", help="clear a halted spend ledger first")
    ap.add_argument("--goal", choices=GOALS, default=DEFAULT_GOAL,
                    help="unsolvable: the flag cannot be recovered (run ids gain -unsolvable)")
    ap.add_argument("--seed-path", default=None,
                    help="boot the board from this candidate chatter file (instigation study) "
                         "instead of lab/seeds/seed.json; run.json records its record id")
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    ctx = ctx_from_args(a)
    if ctx.is_default:
        load_env()
    else:
        load_env(ctx.provider["secret_env"])
    harness = harness_for(ctx, {**HARNESS_DEFAULTS, "budget": a.budget})
    if ctx.goal == "unsolvable":
        harness = {**harness, "stall_exit": False}  # agents never finish early: idle turns spend budget
    RUNS.mkdir(exist_ok=True)
    # a candidate seed must carry its ctx through even on the default (baseline) lane,
    # or start_board would fall back to the pilot seed and run.json lose candidate_id.
    kw = {} if (ctx.is_baseline and not ctx.seed_path) else {"ctx": ctx}
    try:
        with port_lane(ctx.host_port_base, max(a.agents, a.multi)):
            if a.clear_halt and ctx.ledger is not None:
                ctx.ledger.clear_halt()
            if a.mode == "smoke":
                run_one(a.iface, a.agents, 0, {**harness, "budget": min(a.budget, SMOKE_BUDGET_CAP)},
                        tag=a.tag or "-smoke", **kw)
            elif a.mode == "run":
                run_one(a.iface, a.agents, a.rep, harness, tag=a.tag, **kw)
            elif a.mode == "batch":
                run_batch(a, harness, ctx)
            else:
                run_cells(pilot_schedule(a.reps, a.multi), harness, a.tag, ctx)
    except FatalProviderError:  # run_one already printed the [abort] line
        sys.exit(3)
    except BudgetCapReached as e:
        print(f"[budget] stop {e}", flush=True)
        sys.exit(4)


if __name__ == "__main__":
    main()
