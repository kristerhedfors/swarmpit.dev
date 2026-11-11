"""Host-side constants shared by the orchestrator, sandbox policy and analysis code.

VM-side code (lab/board, lab/agent) does not import this module: it only receives values through
command-line arguments and the per-agent cfg.json written by the orchestrator.
"""
import functools
import os
import re
import subprocess
import sys
from pathlib import Path

LAB = Path(__file__).resolve().parent
# Ensure the board package resolves however config.py is loaded (orchestrator import, `python3
# lab/orchestrate.py`, or a bare file-path import like dash's spec_from_file_location, which does
# not put lab/ on sys.path). board.channel is pure stdlib, so importing it host-side is cheap and
# cycle-free; it is the single source of truth for the interface registry below.
if str(LAB) not in sys.path:
    sys.path.insert(0, str(LAB))

from board.channel import CHANNELS  # noqa: E402

REPO = LAB.parent
RUNS = REPO / "runs"
VENDOR = LAB / "vendor"

# ---- experiment grid
# The board.channel registry is the single source of truth for interfaces. The
# grid runs the pilot channels (forum/wiki/cache/irc); the newer read/writable
# channels (guestbook/paste/kv, and the code-comment channels js/html) are registered
# extras, opt-in until measured.
INTERFACES = tuple(name for name, ch in CHANNELS.items() if ch.pilot)
# Every registered channel, pilot or opt-in extra (guestbook/paste/kv/js/html ...). The pilot grid iterates
# INTERFACES; single-run / hill-climb paths may boot any ALL_INTERFACES channel via the registry.
ALL_INTERFACES = tuple(CHANNELS)
IFACE_LABEL = {name: CHANNELS[name].label for name in INTERFACES}
# An interface name is lowercase letters only: it is the first run-id segment (GOAL_ID_RE, the dash
# run-id grammar), a QD channel-axis value and part of landscape cell keys, all of which read [a-z]+.
IFACE_RE = re.compile(r"[a-z]+")
assert all(IFACE_RE.fullmatch(name) for name in CHANNELS), "channel names must match IFACE_RE"
MULTI_AGENTS = 4
PILOT_REPS = 3
PILOT_SHUFFLE_SEED = 1234

# ---- model / harness
MODEL = "qwen-3.8-27b"
HARNESS_DEFAULTS = {
    "model": MODEL,
    "budget": 40,
    "temperature": 0.7,
    "max_tokens": 2048,
    "output_chars": 3000,
    "run_timeout_s": 3600,
}
SMOKE_BUDGET_CAP = 25

# ---- sandboxes
IMAGE = "python:3.12-bookworm"
INFERENCE_HOST = "api.cerebras.ai"
SECRET_ENV = "CEREBRAS_API_KEY"

# What the agent sees: board.internal (127.0.0.2 inside the agent VM) on a protocol-typical port.
# Ports and URLs come from the channel registry, not a hand-maintained table.
HTTP_BOARD_PORT = CHANNELS["forum"].default_port
IRC_BOARD_PORT = CHANNELS["irc"].default_port
BOARD_URL = {name: CHANNELS[name].entry_url() for name in INTERFACES}

# Agent i's relay forwards to host 127.0.0.1:(HOST_PORT_BASE+i), published to board guest port
# (BOARD_GUEST_PORT_BASE+i). The port index is how the board attributes traffic to agent i.
BOARD_GUEST_PORT_BASE = 9000
HOST_PORT_BASE = 19000

# ---- providers / models (an extra experimental factor; Cerebras qwen38 is the default and primary)
# A provider is an OpenAI-compatible chat-completions endpoint. Its host and secret env name feed the
# agent-VM network policy in sandbox.py; the harness reads host/path/secret_env from cfg.json.
PROVIDERS = {
    # Cerebras is BILLED per token, even though its models carry no price here and it has no spend
    # ledger: the 2026-10-13 multi-model QD scale-up spent ~200 USD of Cerebras credit, and auto
    # top-up is now off. Budget Cerebras-heavy runs (episodes x agents x steps, plus trigger panels)
    # before launching them.
    "cerebras": {"host": INFERENCE_HOST, "path": "/v1/chat/completions", "secret_env": SECRET_ENV},
    "berget": {"host": "api.berget.ai", "path": "/v1/chat/completions", "secret_env": "BERGET_API_KEY",
               # auto top-up is on: credit_eur is the authorized total, not the balance. Raised
               # 2026-10-12 from 44/38 to 64/60 (+20 EUR authorized for the QD scale-up).
               "credit_eur": 64.0, "spend_cap_eur": 60.0},
}
# slug -> model. The slug is the run-id segment for non-default models (`<iface>-<pop>-r<rep>--<slug>`).
# Prices are EUR per million tokens; None marks a model outside the spend ledger.
MODELS = {
    # json_object False: under response_format=json_object qwen-3.8 on Cerebras loops in its reasoning
    # channel until max_tokens (finish=length, empty content) on long study inputs; plain mode returns
    # the strict JSON the study parsers ask for (measured on the instigation classifier, 2026-10).
    "qwen38": {"provider": "cerebras", "model": "qwen-3.8-27b", "label": "Qwen 3.8 27B",
               "price_in": None, "price_out": None, "json_object": False},
    "mistral24": {"provider": "berget", "model": "mistralai/Mistral-Small-3.2-24B-Instruct-2506",
                  "label": "Mistral Small 3.2 24B", "price_in": 0.30, "price_out": 0.30,
                  # its chat template rejects a user turn right after a tool turn and an empty
                  # assistant turn without tool calls (both HTTP 400)
                  "wire": {"merge_user_after_tool": True, "drop_empty_assistant": True, "sanitize_tool_args": True}},
    "glm53f": {"provider": "berget", "model": "zai-org/GLM-5.3-Flash", "label": "GLM-5.3 Flash",
               "price_in": 0.25, "price_out": 0.50, "wire": {"sanitize_tool_args": True}},
    "gemma31": {"provider": "berget", "model": "google/gemma-4-31B-it", "label": "Gemma 4 31B",
                "price_in": 0.25, "price_out": 0.50,
                # HTTP 400 two turns after a [checker] user turn that follows a tool turn
                "wire": {"merge_user_after_tool": True, "sanitize_tool_args": True}},
    # A second free Cerebras model. A reasoning model: its message carries a separate `reasoning`
    # field, which the agent harness logs and never echoes back (content may be empty beside tool
    # calls). json_object False: study parsers ask for strict JSON in plain mode, as for qwen38.
    # RETIRED 2026-10-13: it ignored the board (2 writer episodes in 162 in the multi-model QD
    # scale-up, about half of that run's Cerebras bill). The entry stays because existing runs and
    # data reference it (MODEL_ORDER, landscape model axis, analysis); new QD runs and trigger panels
    # refuse it (qd.py E_RETIRED unless --allow-retired). See active_models().
    "gptoss120": {"provider": "cerebras", "model": "gpt-oss-120b", "label": "GPT-OSS 120B",
                  "price_in": None, "price_out": None, "json_object": False, "retired": True},
}
DEFAULT_SLUG = "qwen38"
MODEL_ORDER = ("qwen38", "mistral24", "glm53f", "gemma31", "gptoss120")
BERGET_BATCH = ("mistral24", "glm53f", "gemma31")
SLUG_RE = re.compile(r"[a-z0-9]{2,12}")
# This checkout's ledger dir. Ledgers are opened through spend_dir(), which from a git worktree resolves
# the MAIN checkout's runs/_spend instead, so a worktree never starts a fresh, empty ledger.
SPEND_DIR = RUNS / "_spend"
SPEND_DIR_ENV = "SCHIZ_SPEND_DIR"
# Projection prior per agent session until runs are observed (lab/spend.py). Reasoning tokens are billed
# on top of completion tokens until the provider's console confirms they are already included.
COST_PRIOR_TOKENS = {"prompt": 350_000, "completion": 20_000}
BILL_REASONING_SEPARATELY = True

# ---- task goal (an experimental factor; "solvable" is the default and every run before it)
# In an "unsolvable" run everything the agent can observe is generated exactly as in a solvable one,
# except that vault.bin is sealed host-side under an independent 8-byte key that never enters the VM,
# and the checker expects a fresh flag that never enters the VM either, so no submission is correct
# and the run stays in the task phase for its whole budget. The goal is recorded host-side only:
# run.json params.goal and the `-unsolvable` run-id segment, which is left out of VM names (they
# become the guest hostname).
GOALS = ("solvable", "unsolvable")
DEFAULT_GOAL = "solvable"
GOAL_SEGMENT = {"solvable": "", "unsolvable": "-unsolvable"}
# `<iface>-<pop>-r<rep>[-unsolvable][--<slug>][<tag>]`; the segment ends at the id end or a '-'.
GOAL_ID_RE = re.compile(IFACE_RE.pattern + r"-(?:solo|multi\d+)-r\d+(?P<goal>-unsolvable)?(?=-|$)")

assert all(SLUG_RE.fullmatch(s) for s in MODELS), "model slugs must match SLUG_RE"
assert len({(m["provider"], m["model"]) for m in MODELS.values()}) == len(MODELS), "duplicate model"
assert set(MODEL_ORDER) == set(MODELS) and MODELS[DEFAULT_SLUG]["model"] == MODEL
assert all(m["provider"] in PROVIDERS for m in MODELS.values())
# priced/unpriced is all-or-nothing, and per provider: the spend ledger is one file per provider, so an
# unpriced model (never ledgered) must not share a provider with a capped one (e.g. a second Cerebras
# model is unpriced like qwen38; batch models are all priced Berget models)
assert all((m["price_in"] is None) == (m["price_out"] is None) for m in MODELS.values())
assert all((m["price_in"] is None) == (PROVIDERS[m["provider"]].get("spend_cap_eur") is None) for m in MODELS.values())
assert all(MODELS[s]["provider"] == "berget" and MODELS[s]["price_in"] is not None for s in BERGET_BATCH)
# "retired" (optional, default False): kept for existing data, excluded from active_models()
assert all(isinstance(m.get("retired", False), bool) for m in MODELS.values())
assert not MODELS[DEFAULT_SLUG].get("retired") and not any(MODELS[s].get("retired") for s in BERGET_BATCH)


def is_retired(slug):
    """True for a known model marked retired; False for an active or unknown slug."""
    return bool((MODELS.get(slug) or {}).get("retired", False))


def active_models():
    """Model slugs not marked retired, in MODEL_ORDER (the default for new study runs)."""
    return tuple(s for s in MODEL_ORDER if not is_retired(s))


def resolve_slug(s):
    """Slug for a slug or a full model id; ValueError for anything else."""
    if s in MODELS:
        return s
    for slug, m in MODELS.items():
        if m["model"] == s:
            return slug
    raise ValueError(f"unknown model {s!r} (known: {', '.join(MODEL_ORDER)})")


def slug_for(provider, model):
    """Slug of (provider, model id), or None when it is not in MODELS."""
    return next((s for s, m in MODELS.items() if m["provider"] == provider and m["model"] == model), None)


def run_slug_of(harness):
    """Slug of a run's harness dict: no provider means Cerebras, no model means MODEL (pre-registry runs)."""
    return slug_for(harness.get("provider") or "cerebras", harness.get("model") or MODEL)


def price(slug):
    """(EUR/M in, EUR/M out); (None, None) for an unpriced or unknown model."""
    m = MODELS.get(slug) or {}
    return m.get("price_in"), m.get("price_out")


def provider_spec(slug):
    """The model's provider entry plus its name."""
    name = MODELS[slug]["provider"]
    return {"name": name, **PROVIDERS[name]}


def board_port_for(iface):
    """Port the agent connects to on board.internal for this interface."""
    return CHANNELS[iface].default_port


def board_url_for(iface):
    """Agent-facing URL for any registered channel, from the registry (pilot or opt-in extra).

    BOARD_URL is a pilot-only convenience map; this resolver works for every channel in CHANNELS so the
    single-run and hill-climb paths can boot the opt-in channels (guestbook/paste/kv/js/html) without a KeyError.
    """
    return CHANNELS[iface].entry_url()


def id_goal(run_id):
    """Goal named by a run id: "unsolvable" for ids with the goal segment, else DEFAULT_GOAL."""
    m = GOAL_ID_RE.match(run_id)
    return "unsolvable" if m and m["goal"] else DEFAULT_GOAL


def run_goal(meta):
    """Goal of a run from its run.json (`params.goal`; absent means DEFAULT_GOAL)."""
    return ((meta or {}).get("params") or {}).get("goal") or DEFAULT_GOAL


def vm_run_id(run_id):
    """The run id with its goal segment removed: the part VM names (and so guest hostnames) carry,
    so nothing in the VM names the goal. Identity for solvable ids."""
    m = GOAL_ID_RE.match(run_id)
    if not (m and m["goal"]):
        return run_id
    return run_id[:m.start("goal")] + run_id[m.end("goal"):]


@functools.lru_cache(maxsize=None)
def main_checkout(repo=REPO):
    """Root of the main checkout that `repo` belongs to: the parent of git's common dir, which is
    `repo` itself outside a worktree. None when git is unavailable or `repo` is not a work tree."""
    try:
        out = subprocess.run(["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    common = out.stdout.strip()
    if out.returncode != 0 or not common:
        return None
    common = Path(common)
    if not common.is_absolute():
        common = Path(repo) / common
    return common.resolve().parent


def spend_dir(runs=None):
    """Directory of the per-provider spend ledgers.

    A runs tree other than this repo's RUNS (tests, a scratch tree) keeps its own `<runs>/_spend`.
    Otherwise $SCHIZ_SPEND_DIR if set, else the main checkout's runs/_spend (resolved through git's
    common dir, as the .env is), so runs from every worktree charge the one shared ledger. Falls back
    to SPEND_DIR only when git cannot name a main checkout.
    """
    if runs is not None and Path(runs).resolve() != RUNS.resolve():
        return Path(runs) / SPEND_DIR.name
    env = os.environ.get(SPEND_DIR_ENV)
    if env:
        return Path(env).expanduser()
    main = main_checkout(REPO)
    return main / RUNS.name / SPEND_DIR.name if main is not None else SPEND_DIR
