import os
import shutil
import time
from pathlib import Path

import pytest

from obs.config import REPO, Config
from obs.db import Database
from obs.ingest import Ingester

FIX = Path(__file__).parent / "fixtures" / "runs"
OLD = time.time() - 7200  # make fixture files look settled (older than the collect/pending grace periods)


def copy_runs(dst: Path, names=None) -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    for p in sorted(FIX.iterdir()):
        if p.is_dir() and (names is None or p.name in names):
            shutil.copytree(p, dst / p.name)
        elif p.is_file() and p.name.endswith(".log"):
            shutil.copy(p, dst / p.name)
    for p in dst.rglob("*"):
        os.utime(p, (OLD, OLD))
    return dst


@pytest.fixture
def cfg(tmp_path):
    runs = copy_runs(tmp_path / "runs")
    return Config(runs_dir=runs, db_path=tmp_path / "data" / "obs.duckdb", data_dir=tmp_path / "data",
                  seed_path=REPO / "lab" / "seeds" / "seed.json", live_tap=False, msb=None,
                  web_dist=tmp_path / "no-dist")


@pytest.fixture
def db(cfg):
    d = Database(cfg.db_path, tmp_dir=cfg.tmp_dir)
    d.setup()
    yield d
    d.close()


@pytest.fixture
def ingested(cfg, db):
    ing = Ingester(db, cfg)
    ing.quiet = True
    ing.scan()
    ing.derive_dirty()
    return ing


def counts(db, tables=("agent_records", "model_calls", "tool_calls", "steps", "board_events", "relay_events",
                       "submissions", "posts", "threads", "interactions", "agents", "runs")):
    return {t: db.con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
