import json
import os
import shutil
import time

from obs.db import Database
from obs.ingest import Ingester, LiveChunk

from conftest import FIX, OLD, copy_runs, counts


def _fresh(cfg, tmp_path, name):
    d = Database(tmp_path / name, tmp_dir=cfg.tmp_dir)
    d.setup()
    ing = Ingester(d, cfg)
    ing.quiet = True
    return d, ing


def test_full_ingest_counts(db, ingested):
    c = counts(db)
    assert c["runs"] == 4 and c["agents"] == 13
    assert c["steps"] > 400 and c["model_calls"] > 300 and c["board_events"] > 50
    st = dict(db.con.execute("SELECT run_id, status FROM runs").fetchall())
    assert set(st.values()) == {"done"}
    r = db.con.execute("SELECT code_version, t_origin_final, condition_id, seed_hash FROM runs "
                       "WHERE run_id='forum-solo-r0-smoke'").fetchone()
    assert r[0] == "pre-987afa9-inferred" and r[1] is True and r[2].startswith("c_") and len(r[3]) == 16
    # seed instance posts are materialized per run, with negative t
    assert db.con.execute("SELECT count(*), max(t) FROM posts WHERE run_id='forum-multi4-r0' AND is_seed").fetchone()[1] < 0
    # every executed tool call is linked to exactly one step
    assert db.con.execute("SELECT count(*) FROM steps WHERE NOT idle AND call IS NULL").fetchone()[0] == 0
    assert db.con.execute("SELECT count(*) FROM tool_calls WHERE step IS NULL").fetchone()[0] == 0
    # pilot.log markers
    assert db.con.execute("SELECT count(*) FROM run_markers WHERE source='pilot.log'").fetchone()[0] > 5


def test_rescan_is_idempotent(db, ingested):
    before = counts(db)
    dv = dict(db.con.execute("SELECT run_id, data_version FROM runs").fetchall())
    res = ingested.scan()
    assert res.changed == set()
    assert ingested.derive_dirty() == []
    assert counts(db) == before
    assert dict(db.con.execute("SELECT run_id, data_version FROM runs").fetchall()) == dv


def test_rederive_is_deterministic(db, ingested):
    before = db.con.execute("SELECT * FROM mat_agent_metrics ORDER BY run_id, agent_id").fetchall()
    edges = db.con.execute("SELECT * FROM interactions ORDER BY run_id, edge_uid").fetchall()
    db.con.execute("UPDATE runs SET dirty = TRUE")
    ingested.derive_dirty()
    assert db.con.execute("SELECT * FROM mat_agent_metrics ORDER BY run_id, agent_id").fetchall() == before
    assert db.con.execute("SELECT * FROM interactions ORDER BY run_id, edge_uid").fetchall() == edges


def test_append_safe_incremental(cfg, tmp_path):
    """Ingest a file cut mid-line, then append the rest: result equals a one-shot ingest."""
    runs2 = tmp_path / "runs2"
    copy_runs(runs2, {"forum-multi4-r0"})
    p = runs2 / "forum-multi4-r0" / "a1" / "steps.jsonl"
    full = p.read_bytes()
    cut = len(full) // 2 + 7
    p.write_bytes(full[:cut])
    os.utime(p, (OLD, OLD))
    c2 = cfg.override(runs_dir=runs2)
    d, ing = _fresh(c2, tmp_path, "a.duckdb")
    ing.cfg = c2
    ing.scan()
    n_half = d.con.execute("SELECT count(*) FROM steps WHERE agent_id=1").fetchone()[0]
    with open(p, "ab") as f:
        f.write(full[cut:])
    os.utime(p, (OLD + 10, OLD + 10))
    ing.scan()
    ing.derive_dirty()
    d2, ing2 = _fresh(cfg.override(runs_dir=runs2), tmp_path, "b.duckdb")
    ing2.scan()
    ing2.derive_dirty()
    q = "SELECT * EXCLUDE (src) FROM steps ORDER BY run_id, agent_id, line_off"
    assert 0 < n_half < d.con.execute("SELECT count(*) FROM steps WHERE agent_id=1").fetchone()[0]
    assert d.con.execute(q).fetchall() == d2.con.execute(q).fetchall()
    q = "SELECT * FROM mat_agent_metrics ORDER BY run_id, agent_id"
    assert d.con.execute(q).fetchall() == d2.con.execute(q).fetchall()


def test_truncated_file_resets(cfg, db, ingested):
    p = cfg.runs_dir / "forum-multi4-r0" / "a0" / "relay.jsonl"
    lines = p.read_bytes().splitlines(keepends=True)
    p.write_bytes(b"".join(lines[:3]))
    os.utime(p, (OLD + 20, OLD + 20))
    ingested.scan()
    ingested.derive_dirty()
    assert db.con.execute("SELECT count(*) FROM relay_events WHERE run_id='forum-multi4-r0' AND agent_id=0"
                          ).fetchone()[0] == 3
    notes = json.loads(db.con.execute("SELECT notes FROM runs WHERE run_id='forum-multi4-r0'").fetchone()[0])
    assert any("truncated" in n for n in notes)


def test_status_lifecycle(cfg, db):
    rd = cfg.runs_dir
    (rd / "wiki-multi4-r9").mkdir()
    shutil.copy(rd / "forum-multi4-r0" / "cfg-a0.json", rd / "wiki-multi4-r9" / "cfg-a0.json")
    (rd / "cache-solo-r9").mkdir()
    shutil.copy(rd / "forum-multi4-r0" / "cfg-a0.json", rd / "cache-solo-r9" / "cfg-a0.json")
    os.utime(rd / "cache-solo-r9" / "cfg-a0.json", (OLD, OLD))
    shutil.copytree(rd / "forum-multi4-r0", rd / "forum-multi4-r8")
    (rd / "forum-multi4-r8" / "run.json").unlink()
    for p in (rd / "forum-multi4-r8").rglob("*"):
        os.utime(p, (OLD, OLD))
    arch = rd / "_old_batch"
    arch.mkdir()
    shutil.copytree(rd / "forum-solo-r0-smoke", arch / "forum-solo-r0")
    ing = Ingester(db, cfg)
    ing.quiet = True
    ing.scan()
    ing.derive_dirty()
    st = dict(db.con.execute("SELECT run_id, status FROM runs").fetchall())
    assert st["wiki-multi4-r9"] == "pending"
    assert st["cache-solo-r9"] == "abandoned"
    assert st["forum-multi4-r8"] == "errored"
    assert st["forum-solo-r0-old_batch"] == "done"
    assert "_old_batch" not in st
    tag, arch_p = db.con.execute("SELECT tag, params->>'archive' FROM runs WHERE run_id='forum-solo-r0-old_batch'").fetchone()
    assert (tag, arch_p) == ("old_batch", "_old_batch")
    notes = db.con.execute("SELECT notes FROM runs WHERE run_id='forum-multi4-r8'").fetchone()[0]
    assert "no run.json" in notes
    # errored runs still get all their files
    assert db.con.execute("SELECT count(*) FROM steps WHERE run_id='forum-multi4-r8'").fetchone()[0] > 100
    shutil.rmtree(rd / "wiki-multi4-r9")
    ing.scan()
    assert db.con.execute("SELECT status FROM runs WHERE run_id='wiki-multi4-r9'").fetchone()[0] == "missing"


def test_runs_dir_never_written(cfg, ingested):
    snap = {p: (p.stat().st_size, p.stat().st_mtime) for p in cfg.runs_dir.rglob("*")}
    ingested.scan()
    ingested.derive_dirty()
    assert {p: (p.stat().st_size, p.stat().st_mtime) for p in cfg.runs_dir.rglob("*")} == snap


def test_live_chunks_then_reconcile(cfg, tmp_path):
    """Feed a run through the live path in uneven chunks (no run.json yet), then let run.json appear:
    the host copy is a byte-exact superset, so live rows are adopted (src -> final) with no duplicates."""
    runs3 = tmp_path / "runs3"
    copy_runs(runs3, {"forum-multi4-r0"})
    rd = runs3 / "forum-multi4-r0"
    run_json = (rd / "run.json").read_bytes()
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    for p in list(rd.iterdir()):
        if not p.name.startswith("cfg-"):
            shutil.move(str(p), hidden / p.name)
    c3 = cfg.override(runs_dir=runs3, data_dir=tmp_path / "d3")
    d, ing = _fresh(c3, tmp_path, "live.duckdb")
    ing.cfg = c3
    ing.set_live_vms({"forum-multi4-r0": {f"sb-forum-multi4-r0-a{i}" for i in range(4)} | {"sb-forum-multi4-r0-board"}})
    files = [("steps", i, f"a{i}/steps.jsonl", "/root/logs/steps.jsonl") for i in range(4)] + \
            [("relay", i, f"a{i}/relay.jsonl", "/root/logs/relay.jsonl") for i in range(4)] + \
            [("board", -1, "board_events.jsonl", "/root/logs/events.jsonl")]
    for stream, aid, rel, guest in files:
        data = (hidden / rel).read_bytes()
        vm = f"sb-forum-multi4-r0-{'board' if aid < 0 else f'a{aid}'}"
        off = 0
        for cut in (len(data) // 3 + 5, 2 * len(data) // 3 + 1, len(data)):
            ing.apply_live(LiveChunk("forum-multi4-r0", vm, aid, stream, guest, "epoch1", cut, off, data[off:cut]))
            st = ing.get_state("forum-multi4-r0", stream, aid, "live")
            off = st["byte_off"]
    ing.derive_dirty()
    assert d.con.execute("SELECT status, live_tapped FROM runs").fetchone() == ("live", True)
    live_counts = counts(d)
    assert d.con.execute("SELECT count(*) FROM steps WHERE src='live'").fetchone()[0] == live_counts["steps"]
    # mirrors are byte-exact
    assert (c3.live_dir / "forum-multi4-r0" / "sb-forum-multi4-r0-a2" / "steps.jsonl").read_bytes() == \
        (hidden / "a2" / "steps.jsonl").read_bytes()
    # rebuild while the final copy is missing: the byte-exact live mirrors are the source
    d_m, ing_m = _fresh(c3, tmp_path, "mirror.duckdb")
    ing_m.scan()
    ing_m.derive_dirty()
    cm = counts(d_m)
    for t in ("steps", "model_calls", "board_events", "relay_events", "agent_records"):
        assert cm[t] == live_counts[t], t
    assert d_m.con.execute("SELECT final_missing, live_tapped FROM runs").fetchone() == (True, True)
    d_m.close()
    # orchestrator copies out, writes run.json
    for p in hidden.iterdir():
        shutil.move(str(p), rd / p.name)
    ing.set_live_vms({})
    ing.scan()
    ing.derive_dirty()
    assert d.con.execute("SELECT status FROM runs").fetchone()[0] == "done"
    assert d.con.execute("SELECT count(*) FROM steps WHERE src='live'").fetchone()[0] == 0
    c_after = counts(d)
    for t in ("steps", "model_calls", "board_events", "relay_events", "agent_records"):
        assert c_after[t] == live_counts[t], t
    # identical to a direct file ingest
    d2, ing2 = _fresh(c3, tmp_path, "direct.duckdb")
    ing2.scan()
    ing2.derive_dirty()
    q = "SELECT * EXCLUDE (src) FROM board_events ORDER BY line_off"
    assert d.con.execute(q).fetchall() == d2.con.execute(q).fetchall()
    q = "SELECT * EXCLUDE (run_params) FROM mat_agent_metrics ORDER BY agent_id"
    a, b = d.con.execute(q).fetchall(), d2.con.execute(q).fetchall()
    for x, y in zip(a, b):
        for i, (u, v) in enumerate(zip(x, y)):
            assert u == v, (i, u, v)
    pa = json.loads(d.con.execute("SELECT params FROM runs").fetchone()[0])
    pb = json.loads(d2.con.execute("SELECT params FROM runs").fetchone()[0])
    assert pa == pb


def _cfg_for(cfg, runs):
    return cfg.override(runs_dir=runs)


def _settle(runs):
    for p in runs.rglob("*"):
        os.utime(p, (OLD, OLD))


def test_irc_broken_flag_needs_evidence(cfg, tmp_path):
    """irc_inbound_broken = an agent sent bytes up the relay yet the board parsed no IRC command from it;
    a read-only probe (connect/read/close, no up bytes) is not broken."""
    runs = copy_runs(tmp_path / "r", names={"irc-multi4-r1"})
    src = runs / "irc-multi4-r1"
    broken, probe = runs / "irc-multi4-r2", runs / "irc-multi4-r3"
    for dst in (broken, probe):
        shutil.copytree(src, dst)
        for f in [dst / "run.json", *dst.glob("cfg-a*.json")]:
            f.write_text(f.read_text().replace("irc-multi4-r1", dst.name))
        ev = [json.loads(x) for x in (dst / "board_events.jsonl").read_text().splitlines()]
        (dst / "board_events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in ev if e["kind"] != "irc_cmd"))
    for rl in probe.glob("a*/relay.jsonl"):
        keep = [x for x in rl.read_text().splitlines() if json.loads(x).get("ev") != "data"]
        rl.write_text("".join(x + "\n" for x in keep))
    _settle(runs)
    d, ing = _fresh(_cfg_for(cfg, runs), tmp_path, "irc.duckdb")
    ing.cfg = _cfg_for(cfg, runs)
    ing.scan()
    ing.derive_dirty()
    flag = dict(d.con.execute("SELECT run_id, params->>'irc_inbound_broken' FROM runs").fetchall())
    assert flag == {"irc-multi4-r1": None, "irc-multi4-r2": "true", "irc-multi4-r3": None}


def test_pilot_markers_attributed_to_attempt(cfg, tmp_path):
    """Two attempts of one run id: the archived first attempt and the rerun each get their own pilot.log
    block (matched on run.json duration); an unmatched block is superseded and ignored by derive."""
    runs = copy_runs(tmp_path / "r", names={"forum-multi4-r0"})
    arch = runs / "_invalid_x" / "forum-multi4-r0"
    shutil.copytree(runs / "forum-multi4-r0", arch)
    m = json.loads((arch / "run.json").read_text())
    m["finished"] = m["started"] + 150.2
    (arch / "run.json").write_text(json.dumps(m))
    first = ("[run] forum-multi4-r0: starting board + 4 agent(s)\n[run] forum-multi4-r0: agents running\n"
             "[run] forum-multi4-r0: finished in 150s\n"
             "[run] forum-multi4-r0: starting board + 4 agent(s)\n[run] forum-multi4-r0: finished in 999s\n")
    (runs / "pilot.log").write_text(first + (runs / "pilot.log").read_text())
    _settle(runs)
    c2 = _cfg_for(cfg, runs)
    d, ing = _fresh(c2, tmp_path, "pm.duckdb")
    ing.cfg = c2
    ing.scan()
    ing.derive_dirty()
    got = d.con.execute("SELECT run_id, superseded, list(detail ORDER BY marker_id) FILTER (WHERE kind='finished') "
                        "FROM run_markers WHERE log_run_id = 'forum-multi4-r0' GROUP BY 1, 2 ORDER BY 1, 2").fetchall()
    assert got == [("forum-multi4-r0", False, ["64"]), ("forum-multi4-r0", True, ["999"]),
                   ("forum-multi4-r0-invalid_x", False, ["150"])]
    seeds = dict(d.con.execute("SELECT run_id, params->>'pilot_rng_seed' FROM runs").fetchall())
    assert seeds == {"forum-multi4-r0": "1234", "forum-multi4-r0-invalid_x": "1234"}


def test_done_run_unterminated_tail_and_bad_record(cfg, tmp_path):
    """A finished run's last line without '\\n' is ingested if it parses, else noted; a record with a
    malformed shape is skipped with a note instead of making the run un-ingestable."""
    runs = copy_runs(tmp_path / "r", names={"forum-multi4-r0"})
    rd = runs / "forum-multi4-r0"
    steps = (rd / "a0" / "steps.jsonl").read_bytes()
    lines = steps.splitlines(keepends=True)
    bad = json.loads([x for x in lines if b'"kind": "model"' in x or b'"kind":"model"' in x][0])
    bad["usage"], bad["tool_calls"], bad["call"] = "x", "y", 1e400
    lines.insert(3, (json.dumps(bad) + "\n").encode())
    (rd / "a0" / "steps.jsonl").write_bytes(b"".join(lines)[:-1])  # drop the final newline
    (rd / "a1" / "relay.jsonl").write_bytes((rd / "a1" / "relay.jsonl").read_bytes() + b'{"ev": "op')
    _settle(runs)
    c2 = _cfg_for(cfg, runs)
    d, ing = _fresh(c2, tmp_path, "tail.duckdb")
    ing.cfg = c2
    ing.scan()
    ing.derive_dirty()
    n_steps_file = sum(1 for x in lines if b'"kind": "step"' in x or b'"kind":"step"' in x)
    assert d.con.execute("SELECT count(*) FROM steps WHERE run_id='forum-multi4-r0' AND agent_id=0").fetchone()[0] \
        == n_steps_file
    notes = json.loads(d.con.execute("SELECT notes FROM runs WHERE run_id='forum-multi4-r0'").fetchone()[0])
    assert any("trailing bytes" in n and "relay a1" in n for n in notes), notes
    st = d.con.execute("SELECT byte_off, size FROM ingest_state WHERE run_id='forum-multi4-r0' AND stream='steps' "
                       "AND agent_id=0").fetchone()
    assert st[0] == st[1]


def test_failed_ingest_emits_nothing(cfg, tmp_path, monkeypatch):
    """An exception inside a run's ingest transaction rolls back its emits (no phantom run_started) and is
    recorded in ingest_errors."""
    runs = copy_runs(tmp_path / "r", names={"forum-multi4-r0"})
    c2 = _cfg_for(cfg, runs)
    d = Database(tmp_path / "fe.duckdb", tmp_dir=cfg.tmp_dir)
    d.setup()
    events = []
    ing = Ingester(d, c2, emit=lambda e, p: events.append(e))

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(ing, "apply_summary", boom)
    for _ in range(3):
        ing.scan()
        ing.flush_emits()
    assert "run_started" not in events
    assert d.con.execute("SELECT count(*) FROM ingest_errors WHERE run_id='forum-multi4-r0'").fetchone()[0] == 1
    monkeypatch.undo()
    ing.scan()
    ing.flush_emits()
    assert events.count("run_started") == 1
    assert d.con.execute("SELECT count(*) FROM ingest_errors").fetchone()[0] == 0


def test_live_submissions_dropped_and_correct_rederived(cfg, tmp_path):
    runs = copy_runs(tmp_path / "r", names={"forum-multi4-r0"})
    c2 = _cfg_for(cfg, runs)
    d, ing = _fresh(c2, tmp_path, "sub.duckdb")
    ing.cfg = c2
    ing.scan()
    flag = d.con.execute("SELECT ctf->>'flag' FROM runs").fetchone()[0]
    assert flag
    # a provisional live-tap submission row and a step-derived row inserted before the flag was known
    d.con.execute("INSERT INTO submissions VALUES ('forum-multi4-r0', 0, 5, 1, NULL, ?, FALSE, NULL, 'live')",
                  [f"x {flag}"])
    d.con.execute("INSERT INTO submissions VALUES ('forum-multi4-r0', 1, -999999, 1, NULL, ?, FALSE, 999, 'final')",
                  [flag])
    d.con.execute("DELETE FROM ingest_state WHERE run_id='forum-multi4-r0' AND stream='summary'")
    ing._fp.clear()
    ing.scan()
    ing.derive_dirty()
    assert d.con.execute("SELECT count(*) FROM submissions WHERE line_off >= 0").fetchone()[0] == 0
    assert d.con.execute("SELECT correct FROM submissions WHERE line_off = -999999").fetchone()[0] is True
