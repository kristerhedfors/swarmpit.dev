import pytest
from starlette.testclient import TestClient

from obs.api import create_app
from obs.sse import Broadcaster

RID = "forum-multi4-r0"


@pytest.fixture
def client(cfg, db, ingested):
    return TestClient(create_app(cfg, db, Broadcaster()))


def test_health(client):
    d = client.get("/api/health").json()
    assert d["ok"] and d["schema_version"] == 1 and d["n_runs"] == 4 and d["web_built"] is False
    assert d["live_tap"]["enabled"] is False


def test_facets(client):
    d = client.get("/api/facets").json()
    keys = {x["key"] for x in d["dims"]}
    assert {"model", "iface", "pop", "n_agents", "tag", "status", "harness.budget", "param.code_version"} <= keys
    assert not any(k.startswith("param._") for k in keys)
    assert any(m["metric_id"] == "swarmability_index" for m in d["metrics"])
    budget = next(x for x in d["dims"] if x["key"] == "harness.budget")
    assert budget["kind"] == "num" and {v["value"] for v in budget["values"]} == {40}  # smoke (b25) is tagged
    d2 = client.get("/api/facets?include_tagged=1").json()
    budget = next(x for x in d2["dims"] if x["key"] == "harness.budget")
    assert {v["value"] for v in budget["values"]} == {40, 25}
    # chip counts follow the run list's tagged rule; the tag dim still lists tagged values
    n_runs = client.get("/api/runs").json()["total"]
    for dim in ("iface", "pop"):
        vals = next(x for x in d["dims"] if x["key"] == dim)["values"]
        assert sum(v["n_runs"] for v in vals) == n_runs
        for v in vals:
            assert client.get(f"/api/runs?{dim}={v['value']}").json()["total"] == v["n_runs"]
    tags = next(x for x in d["dims"] if x["key"] == "tag")["values"]
    assert "smoke" in {v["value"] for v in tags}


def test_runs_list_filters(client):
    d = client.get("/api/runs").json()
    assert d["total"] == 3  # smoke run excluded by default
    assert client.get("/api/runs?include_tagged=1").json()["total"] == 4
    assert client.get("/api/runs?tag=smoke").json()["total"] == 1
    assert client.get("/api/runs?iface=forum,irc").json()["total"] == 2
    assert client.get("/api/runs?harness.budget=40&include_tagged=1").json()["total"] == 3
    assert client.get("/api/runs?q=cache").json()["runs"][0]["run_id"] == "cache-multi4-r1"
    r = client.get("/api/runs?sort=run_id&order=asc").json()["runs"]
    assert [x["run_id"] for x in r] == sorted(x["run_id"] for x in r)
    assert set(r[0]["metrics"]) >= {"discovery_rate", "solve_rate", "total_posts", "live_live_edges", "any_leak"}
    assert client.get("/api/runs?sort=evil;drop").status_code == 400


def test_run_detail_and_etag(client):
    r = client.get(f"/api/runs/{RID}")
    assert r.status_code == 200
    d = r.json()
    assert d["run"]["ctf"]["flag"].startswith("FLAG{") and d["run"]["t_origin_final"] is True
    assert len(d["agents"]) == 4 and d["agents"][0]["metrics"]["discovered"] is True
    assert d["condition"]["label"] == "qwen-3.8-27b | forum | multi4 | b40"
    assert {t["thread"] for t in d["threads"]} >= {"roll-call", "flag-recovery-tips"}
    tag = r.headers["etag"]
    assert client.get(f"/api/runs/{RID}", headers={"If-None-Match": tag}).status_code == 304
    assert client.get("/api/runs/nope").status_code == 404


def test_timeline(client):
    d = client.get(f"/api/runs/{RID}/timeline").json()
    assert d["lanes"][0] == {"lane": -1, "label": "board"} and len(d["lanes"]) == 5
    kinds = {e["kind"] for e in d["events"]}
    assert {"step", "idle", "model", "req", "post", "start", "end", "board_start"} <= kinds
    assert not any(k.startswith("relay") for k in kinds)
    ts = [e["t"] for e in d["events"]]
    assert ts == sorted(ts)
    post = next(e for e in d["events"] if e["kind"] == "post")
    assert post["uid"].startswith("p") and "flags" in post
    assert any(p["is_seed"] and p["t"] < 0 for p in d["posts"])
    assert len(d["phases"]) == 4 and d["phases"][0]["solve"] > 0
    c = client.get(f"/api/runs/{RID}/timeline?format=columns&kinds=req,relay_open&agents=2&t0=0&t1=30").json()
    assert set(c["events"]["lane"]) == {2} and max(c["events"]["t"]) <= 30
    assert "relay_open" in set(c["events"]["kind"])
    rd = client.get(f"/api/runs/{RID}/timeline?include=relay_data&kinds=step").json()
    assert any(e["kind"] == "relay_data" for e in rd["events"]) and "posts" not in rd
    assert client.get(f"/api/runs/{RID}/timeline?kinds=bogus").status_code == 400


def test_state_matches_counts(client):
    end = client.get(f"/api/runs/{RID}/state").json()
    assert end["metrics"]["agents_solved"] == 4 and end["metrics"]["posts"] == 4
    assert all(a["status"] == "done" for a in end["agents"])
    s0 = client.get(f"/api/runs/{RID}/state?t=0").json()
    assert all(a["status"] == "not_started" for a in s0["agents"])
    assert s0["board"]["n_posts"] == 9  # seed posts only
    mid = client.get(f"/api/runs/{RID}/state?t=25&include=board,agents,metrics,graph").json()
    assert 9 < mid["board"]["n_posts"] < 13 and "graph" in mid
    news = [p for t in mid["board"]["threads"] for p in t["posts"] if p["new"]]
    assert news and all(not p["is_seed"] for p in news)


def test_graph(client):
    g = client.get(f"/api/runs/{RID}/graph").json()
    assert {n["id"] for n in g["nodes"]} >= {"a0", "a1", "a2", "a3"}
    assert {e["kind"] for e in g["edges"]} >= {"exposure", "reply"}
    g2 = client.get(f"/api/runs/{RID}/graph?seed=0&kinds=reply").json()
    assert all(not e["dst"].startswith("seed:") and e["kind"] == "reply" for e in g2["edges"])
    g0 = client.get(f"/api/runs/{RID}/graph?t=0").json()
    assert g0["edges"] == []


def test_agent_steps(client):
    d = client.get(f"/api/runs/{RID}/agents/2/steps").json()
    assert len(d["steps"]) == 40 and d["output_chars"] == 3000
    solve = next(s for s in d["steps"] if s["is_solve"])
    assert solve["submissions"][0]["correct"] is True and solve["model"]["tool_calls"]
    board = [s for s in d["steps"] if s["is_board"] and s["board"]]
    assert board and board[0]["relay"]["conns"] >= 1
    f = client.get(f"/api/runs/{RID}/agents/2/steps?from=5&to=6&fields=command").json()
    assert [s["step"] for s in f["steps"]] == [5, 6] and set(f["steps"][0]) == {"step", "command"}
    assert client.get(f"/api/runs/{RID}/agents/9/steps").status_code == 404


def test_cells(client):
    d = client.get("/api/metrics/cells?B=200").json()
    assert d["group"] == ["model", "iface", "pop"]
    assert len(d["cells"]) == 3  # 3 untagged done runs, one per iface
    v = d["cells"][0]["values"]["share_after_contact"]
    assert set(v) >= {"mean", "median", "sd", "lo", "hi", "n"}
    assert v["lo"] is None  # bootstrap needs >= 2 runs per cell
    d2 = client.get("/api/metrics/cells?group=pop&metrics=posts,discovered&points=1&B=200").json()
    c = d2["cells"][0]
    assert c["n_runs"] == 3 and c["values"]["posts"]["lo"] is not None and len(c["points"]) == 12
    assert client.get("/api/metrics/cells?group=param.x;1").status_code == 400
    assert client.get("/api/metrics/cells?metrics=nope").status_code == 400


def test_series(client):
    d = client.get(f"/api/metrics/series?metric=board_share_rolling5&run={RID}").json()
    assert d["xs"][0] == 1 and len(d["series"]) == 4 and len(d["series"][0]["y"]) == len(d["xs"])
    d = client.get("/api/metrics/series?metric=cum_tokens&x=t&group=iface&band=1&bucket=10").json()
    assert d["series"] and "lo" in d["series"][0]
    d = client.get("/api/metrics/series?metric=requests&x=t_since_contact&group=iface").json()
    assert d["xs"]
    assert client.get("/api/metrics/series?metric=x").status_code == 400


def test_live_recent_and_control(client):
    assert client.get("/api/live/recent").json()["items"] == []
    for m in ("get", "post", "patch", "delete"):
        r = getattr(client, m)("/api/control/runs/x/stop")
        assert r.status_code == 501 and r.json()["error"] == "not_implemented"


def test_static_hint_when_unbuilt(client):
    r = client.get("/")
    assert r.status_code == 503 and "npm run build" in r.text
    assert client.get("/api/nope").status_code == 404


def test_static_containment(cfg, db, tmp_path):
    import asyncio
    dist = tmp_path / "web" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("INDEX")
    (dist / "ok.txt").write_text("OK")
    (tmp_path / "web" / "dist-secret").mkdir()
    (tmp_path / "web" / "dist-secret" / "x.txt").write_text("SECRET")
    app = create_app(cfg.override(web_dist=dist), db, Broadcaster())

    async def raw(path):
        body, status = b"", None
        scope = {"type": "http", "method": "GET", "path": path, "raw_path": path.encode(), "query_string": b"",
                 "headers": [], "http_version": "1.1", "scheme": "http", "server": ("t", 80), "root_path": ""}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(m):
            nonlocal body, status
            if m["type"] == "http.response.start":
                status = m["status"]
            elif m["type"] == "http.response.body":
                body += m.get("body", b"")
        await app(scope, receive, send)
        return status, body
    assert asyncio.run(raw("/ok.txt")) == (200, b"OK")
    assert asyncio.run(raw("/../dist-secret/x.txt"))[1] == b"INDEX"
