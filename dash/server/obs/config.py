"""Environment / path resolution (CONTRACT section 1.1). CLI flags override env via Config.override()."""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # obs/config.py -> obs -> server -> dash -> <repo>
DASH = REPO / "dash"

# EUR per million tokens (in, out) by harness.model, mirrored from lab/config.MODELS (tests check they agree).
# None = not priced (Cerebras free tier). Written to the `models` table by derive; feeds the cost_eur metric.
MODEL_PRICES: dict[str, tuple[float | None, float | None]] = {
    "qwen-3.8-27b": (None, None),
    "mistralai/Mistral-Small-3.2-24B-Instruct-2506": (0.30, 0.30),
    "zai-org/GLM-5.3-Flash": (0.25, 0.50),
    "google/gemma-4-31B-it": (0.25, 0.50),
    "gpt-oss-120b": (None, None),
}


def _main_checkout() -> Path | None:
    try:
        out = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--git-common-dir"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    common = Path(out.stdout.strip())
    if not common.is_absolute():
        common = (REPO / common).resolve()
    return common.parent


def default_runs_dir() -> Path:
    if (REPO / "runs").is_dir():
        return REPO / "runs"
    main = _main_checkout()
    if main and (main / "runs").is_dir():
        return main / "runs"
    return REPO / "runs"


def default_msb() -> str | None:
    p = Path.home() / ".local/bin/msb"
    if p.exists():
        return str(p)
    return shutil.which("msb")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Config:
    runs_dir: Path = field(default_factory=lambda: Path(os.environ["OBS_RUNS_DIR"]) if os.environ.get("OBS_RUNS_DIR")
                           else default_runs_dir())
    db_path: Path = field(default_factory=lambda: Path(os.environ.get("OBS_DB", DASH / "data/observatory.duckdb")))
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("OBS_DATA_DIR", DASH / "data")))
    # None = resolve from the repo that owns runs_dir (see seed_file); OBS_SEED_PATH / --seed overrides.
    seed_path: Path | None = field(default_factory=lambda: Path(os.environ["OBS_SEED_PATH"])
                                   if os.environ.get("OBS_SEED_PATH") else None)
    msb: str | None = field(default_factory=lambda: os.environ.get("OBS_MSB") or default_msb())
    live_tap: bool | None = None  # resolved in __post_init__
    poll_s: float = field(default_factory=lambda: _env_float("OBS_POLL_S", 3))
    tap_discover_s: float = field(default_factory=lambda: _env_float("OBS_TAP_DISCOVER_S", 5))
    tap_tail_s: float = field(default_factory=lambda: _env_float("OBS_TAP_TAIL_S", 5))
    experiment: str = field(default_factory=lambda: os.environ.get("OBS_EXPERIMENT", "board-stickiness"))
    control_enabled: bool = field(default_factory=lambda: os.environ.get("OBS_CONTROL_ENABLED") == "1")
    web_dist: Path = field(default_factory=lambda: Path(os.environ.get("OBS_WEB_DIST", DASH / "web/dist")))

    def __post_init__(self):
        if self.live_tap is None:
            # Off unless explicitly enabled: the tap execs into agent VMs, which the observed agents can see in
            # /proc (observer effect; CONTRACT 3.4). Enable with OBS_LIVE_TAP=1 / --live-tap when acceptable.
            self.live_tap = os.environ.get("OBS_LIVE_TAP") == "1"
        self.runs_dir = Path(self.runs_dir).expanduser()
        self.db_path = Path(self.db_path).expanduser()
        self.data_dir = Path(self.data_dir).expanduser()

    @property
    def seed_file(self) -> Path:
        """Seed used for runs whose dir has no seed.json: the lab checkout that owns runs_dir (the runs were
        produced by that lab), falling back to this repo's lab."""
        if self.seed_path:
            return Path(self.seed_path).expanduser()
        own = self.runs_dir.resolve().parent / "lab/seeds/seed.json"
        return own if own.is_file() else REPO / "lab/seeds/seed.json"

    @property
    def live_dir(self) -> Path:
        return self.data_dir / "live"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    def override(self, **kw) -> "Config":
        return replace(self, **{k: v for k, v in kw.items() if v is not None})
