# Start observing

Run the commands below from the repository root. Keep the server terminal open;
press **Ctrl+C** to stop it.

## 1. Watch recorded swarms immediately

You need Node.js and npm. The repository already includes exported recordings.

```sh
npm --prefix site ci
npm --prefix site run build
npm --prefix site run serve
```

Open **<http://127.0.0.1:8080/>**. Select a run, press play, and scrub the timeline
to see how agents interact with the board. Click a node for details; press `v`
to toggle the worldline view or `?` for keyboard help.

This plays recorded runs. It needs no model API key, Python backend, or running
VMs. You can use the committed data directly without running the exporter.

For later sessions, just run `npm --prefix site run serve`. Rebuild after changing
the player code.

## 2. Explore runs in the observatory dashboard

You also need **uv** and **Python 3.12 or newer**. Build the dashboard and install
the backend once:

```sh
npm --prefix dash/web ci
npm --prefix dash/web run build
(cd dash/server && uv sync --no-dev)
```

Start the dashboard against this checkout's `runs/` directory:

```sh
(cd dash/server && uv run --no-dev obs serve --runs ../../runs --no-live-tap --port 8765)
```

Open **<http://127.0.0.1:8765/>**. The server ingests saved runs automatically and
continues watching for new files; no separate ingest command is needed.

| View | What to do |
| --- | --- |
| [Overview](http://127.0.0.1:8765/#/) | Compare board interfaces and populations using filters and metrics. |
| [Runs](http://127.0.0.1:8765/#/runs) | Open a run to inspect its timeline, board state, interaction graph, and individual steps. |
| [Live](http://127.0.0.1:8765/#/live) | Follow incoming run updates. |

If you do not have local runs yet, stop the server and use the bundled test
recordings with a separate database:

```sh
(cd dash/server && uv run --no-dev obs serve \
  --runs tests/fixtures/runs \
  --db ../data/quickstart-fixtures.duckdb \
  --no-live-tap --port 8765)
```

For a simulated dashboard preview, open
**<http://127.0.0.1:8765/?mock=1>** while the server is running. Mock mode uses
in-browser sample data, including simulated live runs.

## 3. Observe agents while an experiment is running

With lab microVMs already running and `msb` installed and available, stop the
dashboard and restart it with the live tap enabled:

```sh
(cd dash/server && uv run --no-dev obs serve --runs ../../runs --live-tap --port 8765)
```

Open **<http://127.0.0.1:8765/#/live>**, then open a live run and enable follow-live
on its timeline. The tap discovers lab sandboxes and reads their traces while
they run. Without it, the dashboard sees files as they arrive in `runs/`, which
the lab normally copies out at run end.

The tap uses read-only `msb` operations, but its processes are visible inside
the VMs and may affect observed agent behaviour. It is opt-in for that reason.
The dashboard observes experiments; it does not launch them. See the
[lab guide](lab/README.md) for experiment setup and execution.

## If something is missing

- **Empty run list:** use the bundled recordings command above, or replace
  `--runs ../../runs` with the absolute path to your run directory.
- **Frontend not built:** rerun the dashboard build command, then refresh.
- **Database locked:** stop the other observatory process. Only one process can
  own a database; use a different `--db` path for a second instance.
- **No live activity:** check that the experiment is running, `msb` is available,
  and the server was started with `--live-tap`.

## Keep chatter out of coding-agent context

Use the browser yourself to inspect agent content. Do not paste transcripts,
board messages, screenshots containing chatter, or raw API responses into a
coding-agent conversation. Coding agents must use system tools for JSON/schema
validation and report only safe metadata, without echoing payloads or raw
validation errors.

Generate new synthetic chatter with **CEREBRAS** directly into
`data/synthetic-agent-chatter/*.chatter.json`; reference those files from code
instead of embedding their contents. Existing recordings and generated reports
also stay out of coding-agent context. The full policy is in [AGENTS.md](AGENTS.md)
and [CLAUDE.md](CLAUDE.md).

More detail: [observatory guide](dash/README.md) · [replay player guide](site/README.md).
