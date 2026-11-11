import asyncio
import subprocess

import os

from obs.livetap import GUEST_ARGV, GUEST_CMDS, exec_argv, exec_env, parse_framed
from obs.sse import Broadcaster, stream


def test_guest_allow_list_and_argv():
    assert set(GUEST_CMDS) <= {"sh", "wc", "tail", "head", "test"}
    argv = exec_argv("/x/msb", "sb-forum-solo-r0-a0", [("/root/logs/steps.jsonl", 10)])
    assert argv[1] == "exec" and "--no-stdin" in argv
    for bad in ("create", "stop", "rm", "cp", "--replace"):
        assert bad not in argv[:argv.index("--")]
    assert argv[argv.index("--") + 1] == "sh"
    # observer-effect hardening: the guest argv names neither the tap nor any log path
    guest = argv[argv.index("--") + 1:]
    assert guest == GUEST_ARGV and not any("logs" in a or "tap" in a for a in guest)


def _run_script(files, max_bytes):
    env = {**os.environ, **exec_env(files, max_bytes)}
    return subprocess.run(GUEST_ARGV, capture_output=True, env=env).stdout


def test_framed_script_runs_locally(tmp_path):
    """The guest script only needs POSIX sh + wc/tail/head/test, so it can be exercised on the host."""
    f = tmp_path / "steps.jsonl"
    f.write_bytes(b'{"a":1}\n{"b":22}\n{"c":3')
    out = _run_script([(str(f), 8), (str(tmp_path / "missing"), 0)], 1000)
    parsed, done, complete = parse_framed(out)
    assert complete and not done
    assert parsed[0]["size"] == f.stat().st_size and parsed[0]["off"] == 8
    assert parsed[0]["data"] == b'{"b":22}\n{"c":3'
    assert parsed[1]["size"] == -1
    out = _run_script([(str(f), 0)], 4)
    parsed, _, complete = parse_framed(out)
    assert complete and parsed[0]["data"] == b'{"a"'


def test_broadcaster_replay_and_reset():
    bc = Broadcaster(ring=3, start_id=1)
    for i in range(5):
        bc.publish("event", {"run_id": "r", "kind": "step", "i": i})
    assert [x["data"]["i"] for x in bc.recent(10)] == [2, 3, 4]
    assert [x["id"] for x in bc.replay_after(3)] == [4, 5]
    assert bc.replay_after(1) is None

    async def first(last, n):
        out = []
        async for chunk in stream(bc, last, None, None):
            out.append(chunk)
            if len(out) == n:
                break
        return out

    chunks = asyncio.run(first("1", 2))
    assert chunks[0].startswith("retry:") and "event: reset" in chunks[1]
    chunks = asyncio.run(first("3", 3))
    assert "id: 4" in chunks[1] and "id: 5" in chunks[2]


def test_replay_after_restart_resets():
    old = Broadcaster(start_id=1)
    for i in range(340):
        old.publish("event", {"i": i})
    new = Broadcaster()  # restarted process: ids start at boot ms, far above the old ids
    for i in range(50):
        new.publish("event", {"i": i})
    assert new.replay_after(340) is None  # stale pre-restart id -> reset
    assert new.replay_after(new.next_id + 5) is None  # id from the future -> reset
    assert len(new.replay_after(new.next_id - 3)) == 2
    empty = Broadcaster(start_id=100)
    assert empty.replay_after(99) == [] and empty.replay_after(50) is None


def test_slow_subscriber_gets_reset():
    async def go():
        bc = Broadcaster(start_id=1, queue_max=5)
        gen = stream(bc, None, None, None)
        assert (await gen.__anext__()).startswith("retry:")
        nxt = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0)
        for i in range(12):
            bc.publish("event", {"i": i})
        await asyncio.sleep(0.05)
        out = [await nxt]
        for _ in range(3):
            try:
                out.append(await asyncio.wait_for(gen.__anext__(), 0.2))
            except asyncio.TimeoutError:
                break
        await gen.aclose()
        return out
    out = asyncio.run(go())
    assert any("event: reset" in c and "subscriber_overflow" in c for c in out), out


def test_discover_failure_keeps_vms(tmp_path):
    import stat
    from obs.config import Config
    from obs.livetap import LiveTap
    fail = tmp_path / "FAIL"
    msb = tmp_path / "msb"
    msb.write_text("#!/bin/sh\nif [ -e %s ]; then exit 1; fi\n"
                   "echo '[{\"name\":\"sb-forum-multi2-r0-a0\",\"status\":\"running\"},"
                   "{\"name\":\"sb-forum-multi2-r0-a1\",\"status\":\"running\"}]'\n" % fail)
    msb.chmod(msb.stat().st_mode | stat.S_IEXEC)
    cfg = Config(runs_dir=tmp_path / "runs", db_path=tmp_path / "x.duckdb", data_dir=tmp_path, msb=str(msb),
                 live_tap=True)
    tap = LiveTap(cfg, submit=lambda i: None, offsets_fn=lambda r, v: {}, run_done_fn=lambda r: False)
    assert tap.discover() == {"forum-multi2-r0": {"sb-forum-multi2-r0-a0", "sb-forum-multi2-r0-a1"}}
    fail.write_text("")
    assert tap.discover() is None and len(tap.vms) == 2  # transient failure: no change submitted
    fail.unlink()
    msb.write_text("#!/bin/sh\necho '[{\"name\":\"sb-forum-multi2-r0-a0\",\"status\":\"running\"}]'\n")
    assert tap.discover() == {"forum-multi2-r0": {"sb-forum-multi2-r0-a0", "sb-forum-multi2-r0-a1"}}  # 1 miss: kept
    assert tap.discover() == {"forum-multi2-r0": {"sb-forum-multi2-r0-a0"}}  # 2nd miss: gone


def test_discover_maps_goalless_vm_names_to_the_live_run(tmp_path):
    """VM names leave the goal segment out: 'sb-forum-multi2-r0-a0' belongs to whichever of forum-multi2-r0 /
    forum-multi2-r0-unsolvable has a run dir without run.json, decided once per VM instance."""
    import os
    import stat
    from obs.config import Config
    from obs.livetap import LiveTap
    msb = tmp_path / "msb"
    msb.write_text("#!/bin/sh\necho '[{\"name\":\"sb-forum-multi2-r0-a0\",\"status\":\"running\",\"created_at\":\"1\"},"
                   "{\"name\":\"sb-forum-multi2-r0-board\",\"status\":\"running\",\"created_at\":\"1\"}]'\n")
    msb.chmod(msb.stat().st_mode | stat.S_IEXEC)
    runs = tmp_path / "runs"
    cfg = Config(runs_dir=runs, db_path=tmp_path / "x.duckdb", data_dir=tmp_path, msb=str(msb), live_tap=True)
    vms = {"sb-forum-multi2-r0-a0", "sb-forum-multi2-r0-board"}
    # no run dir yet: the VM id is the run id (pre-goal behaviour)
    tap = LiveTap(cfg, submit=lambda i: None, offsets_fn=lambda r, v: {}, run_done_fn=lambda r: False)
    assert tap.discover() == {"forum-multi2-r0": vms}
    # the solvable run is done, the unsolvable one is in progress
    (runs / "forum-multi2-r0").mkdir(parents=True)
    (runs / "forum-multi2-r0" / "run.json").write_text("{}")
    (runs / "forum-multi2-r0-unsolvable").mkdir()
    (runs / "forum-multi2-r0-unsolvable" / "cfg-a0.json").write_text("{}")
    tap = LiveTap(cfg, submit=lambda i: None, offsets_fn=lambda r, v: {})
    assert tap.discover() == {"forum-multi2-r0-unsolvable": vms}
    # its run.json lands while the VMs are still up: the mapping sticks, so the run is skipped (not re-routed)
    (runs / "forum-multi2-r0-unsolvable" / "run.json").write_text("{}")
    (runs / "forum-multi2-r0" / "run.json").unlink()
    assert tap.discover() == {}
    # a stale solvable dir without run.json and a fresh unsolvable one: the newest wins
    (runs / "forum-multi2-r0-unsolvable" / "run.json").unlink()
    os.utime(runs / "forum-multi2-r0", (1000, 1000))
    tap = LiveTap(cfg, submit=lambda i: None, offsets_fn=lambda r, v: {})
    assert tap.discover() == {"forum-multi2-r0-unsolvable": vms}


def test_discover_lingering_vm_after_run_json_is_not_rerouted(tmp_path):
    """A failed `msb rm -f` leaves a finished unsolvable run's VMs listed: they stay mapped to that run (skipped),
    never re-resolved to the goal-less id as a phantom solvable run -- also for a fresh tap (dash restart)."""
    import stat
    from obs.config import Config
    from obs.livetap import LiveTap
    msb = tmp_path / "msb"
    msb.write_text("#!/bin/sh\necho '[{\"name\":\"sb-forum-solo-r10-a0\",\"status\":\"running\",\"created_at\":\"1\"}]'\n")
    msb.chmod(msb.stat().st_mode | stat.S_IEXEC)
    runs = tmp_path / "runs"
    (runs / "forum-solo-r10-unsolvable").mkdir(parents=True)
    cfg = Config(runs_dir=runs, db_path=tmp_path / "x.duckdb", data_dir=tmp_path, msb=str(msb), live_tap=True)
    tap = LiveTap(cfg, submit=lambda i: None, offsets_fn=lambda r, v: {})
    assert tap.discover() == {"forum-solo-r10-unsolvable": {"sb-forum-solo-r10-a0"}}
    (runs / "forum-solo-r10-unsolvable" / "run.json").write_text("{}")
    for _ in range(4):
        assert tap.discover() == {}
    assert LiveTap(cfg, submit=lambda i: None, offsets_fn=lambda r, v: {}).discover() == {}
