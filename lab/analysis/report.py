"""Assemble the self-contained HTML pilot report from analysis.json and hand-written report_notes.json.

report_notes.json holds the prose (lede, findings, quotes, setup, caveats, next, free_text); its
list items are trusted HTML, quotes are escaped. Everything numeric comes from analysis.json,
including the design facts the template quotes (calls per agent, runs and agents per interface).

render_comparison builds the per-model page from analysis_models.json the same way, with prose
(lede, findings, caveats) from report_models_notes.json.

Both pages follow the document's goal ("goal" key, absent = solvable): an unsolvable document has no
free phase, so its free-phase chart becomes whole-session board steps, its free-time section says n/a
and its table shows n/a in the free-phase columns. render_goals builds the solvable-vs-unsolvable page
from analysis_goals.json (analyze.py --goals), prose (lede, findings, caveats) from report_goals_notes.json.
"""
from config import IFACE_LABEL, INTERFACES

from .aggregate import DEFAULT_GOAL, GOAL_LABEL
from .charts import POPULATIONS, esc, fmt, grouped_bars, model_bars, pooled_bars

FONTS_URL = ("https://fonts.googleapis.com/css2?family=Archivo:wght@500;700;800"
             "&family=Atkinson+Hyperlegible:ital,wght@0,400;0,700;1,400&family=JetBrains+Mono:wght@400;600&display=swap")

STYLE = """/* Layout: single reading column (~68ch) with full-width figures and a wide scrollable data table. */
:root {
  --bg:#f6f7f9; --panel:#ffffff; --fg:#14171c; --muted:#5a6270; --rule:#d9dde4;
  --s1:#2a78d6; --s2:#eb6834; --idle:#c9ced6; --accent:#2a78d6; --code:#eef1f5;
  --display:"Archivo", "Helvetica Neue", Arial, sans-serif;
  --body:"Atkinson Hyperlegible", "Segoe UI", system-ui, sans-serif;
  --mono:"JetBrains Mono", ui-monospace, Menlo, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg:#111418; --panel:#181c22; --fg:#eef1f5; --muted:#a3abb8; --rule:#2c323b;
  --s1:#3987e5; --s2:#d95926; --idle:#3a414b; --accent:#5b9cf0; --code:#20262e; color-scheme:dark } }
:root[data-theme="dark"] {
  --bg:#111418; --panel:#181c22; --fg:#eef1f5; --muted:#a3abb8; --rule:#2c323b;
  --s1:#3987e5; --s2:#d95926; --idle:#3a414b; --accent:#5b9cf0; --code:#20262e; color-scheme:dark }
body { background:var(--bg); color:var(--fg); font-family:var(--body); font-size:16px; line-height:1.55;
  padding-inline:16px; padding-block:32px 64px }
main { max-width:68ch; margin:0 auto; display:flex; flex-direction:column; gap:28px }
h1,h2 { font-family:var(--display); text-wrap:balance; margin:0; letter-spacing:-0.01em }
h1 { font-size:2.1rem; font-weight:800 }
h2 { font-size:1.25rem; font-weight:700; padding-top:8px; border-top:1px solid var(--rule) }
p, ul { margin:0 }
ul { padding-left:1.2em; display:flex; flex-direction:column; gap:6px }
.eyebrow { font-family:var(--mono); font-size:.75rem; letter-spacing:.08em; text-transform:uppercase; color:var(--muted) }
.lede { font-size:1.1rem }
code, .mono { font-family:var(--mono); font-size:.85em; background:var(--code); padding:1px 4px; border-radius:3px }
section { display:flex; flex-direction:column; gap:14px }
.facts { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px }
.facts div { display:flex; flex-direction:column; gap:2px }
.facts b { font-family:var(--mono); font-size:1.3rem; font-variant-numeric:tabular-nums }
.facts span { font-size:.8rem; color:var(--muted) }
figure { margin:0; background:var(--panel); border:1px solid var(--rule); border-radius:6px; padding:14px; display:flex; flex-direction:column; gap:8px }
figcaption { display:flex; flex-direction:column; gap:2px; font-size:.95rem }
figcaption span { color:var(--muted); font-size:.85rem }
.legend { display:flex; gap:16px; font-size:.85rem; color:var(--muted) }
.legend i { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:6px; vertical-align:-1px }
.chart { width:100%; height:auto; overflow:visible }
.chart .grid { stroke:var(--rule); stroke-width:1 }
.chart .base { stroke:var(--muted); stroke-width:1 }
.chart .tick { fill:var(--muted); font:11px var(--mono) }
.chart .axis { fill:var(--fg); font:12px var(--body) }
.chart .val { fill:var(--fg); font:600 11px var(--mono) }
.chart .bar { stroke:var(--panel); stroke-width:2 }
.chart .ci { stroke:var(--fg); stroke-width:1.5 }
.chart .s1 { fill:var(--s1) } .chart .s2 { fill:var(--s2) }
.chart .bar:hover, .chart .bar:focus { opacity:.8; outline:none }
.freebar { display:flex; gap:2px; height:22px; border-radius:4px; overflow:hidden }
.freebar span { min-width:3px }
#tip { position:fixed; pointer-events:none; background:var(--fg); color:var(--bg); font:12px var(--mono); padding:4px 8px; border-radius:4px }
.tablewrap { overflow-x:auto; border:1px solid var(--rule); border-radius:6px; background:var(--panel) }
table { border-collapse:collapse; font-size:.8rem; font-variant-numeric:tabular-nums; min-width:900px; width:100% }
th, td { padding:6px 8px; text-align:right; border-bottom:1px solid var(--rule); white-space:nowrap }
th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) { text-align:left }
th { font-family:var(--mono); font-weight:600; font-size:.72rem; color:var(--muted) }
blockquote { margin:0; padding:10px 14px; border-left:3px solid var(--accent); background:var(--panel); display:flex; flex-direction:column; gap:4px }
blockquote p { font-family:var(--mono); font-size:.82rem; white-space:pre-wrap; word-break:break-word }
cite { font-size:.8rem; color:var(--muted); font-style:normal }
.quotes { display:flex; flex-direction:column; gap:10px }
@media (prefers-reduced-motion: reduce) { * { transition:none !important } }
"""

TOOLTIP_JS = """const tip = document.getElementById('tip');
document.querySelectorAll('[data-tip]').forEach(el => {
  const show = e => { tip.textContent = el.dataset.tip; tip.hidden = false;
    const r = el.getBoundingClientRect(); const x = e.clientX ?? r.left + r.width / 2; const y = e.clientY ?? r.top;
    tip.style.left = Math.min(x + 12, innerWidth - tip.offsetWidth - 8) + 'px'; tip.style.top = (y - 34) + 'px'; };
  el.addEventListener('mousemove', show); el.addEventListener('focus', show);
  el.addEventListener('mouseleave', () => tip.hidden = true); el.addEventListener('blur', () => tip.hidden = true);
});
"""

TABLE_HEADERS = ["interface", "population", "agents", "contacted", "first contact step", "board steps", "…after solve",
                 "share after contact", "share of free phase", "posts", "…after solve", "visits", "solved", "solve step"]


def span(values) -> str:
    """'3' when all values agree, '2–3' when they differ (an unbalanced grid), '–' when there are none."""
    vs = sorted(set(values))
    if not vs:
        return "–"
    return str(vs[0]) if len(vs) == 1 else f"{vs[0]}–{vs[-1]}"


def experiment_shape(analysis: dict) -> dict:
    """Design facts quoted in the prose, read from the data so the text follows the grid that was run."""
    runs, cells = analysis["runs"], analysis["cells"]
    ifaces = [p["iface"] for p in analysis["pooled"]]

    def runs_per_iface(pop: str) -> list[int]:
        return [sum(1 for r in runs if r["iface"] == i and r["pop"] == pop) for i in ifaces]

    return {
        "calls": max((a["steps"] for r in runs for a in r["agents"]), default=0),
        "agents": span(p["n_agents"] for p in analysis["pooled"]),
        "solo_runs": span(runs_per_iface("solo")),
        "multi_runs": span(runs_per_iface("multi")),
        "multi_size": span(len(r["agents"]) for r in runs if r["pop"] == "multi"),
        "n_solo": span(c["n_agents"] for c in cells if c["pop"] == "solo"),
        "n_multi": span(c["n_agents"] for c in cells if c["pop"] == "multi"),
    }


def ordered_cells(cells: list[dict]) -> list[dict]:
    """Cells in interface order, solo before multi."""
    return sorted(cells, key=lambda c: (INTERFACES.index(c["iface"]), c["pop"] != "solo"))


NA = "n/a"


def free_fmt(v, na: bool, pct: bool = False) -> str:
    """A free-phase value, or "n/a" where the goal has no free phase."""
    return NA if na else fmt(v, pct)


def table_row(c: dict, na: bool = False) -> str:
    """One cells-table row; na shows "n/a" in the free-phase columns (a goal without a free phase)."""
    return (f"<tr><td>{esc(IFACE_LABEL[c['iface']])}</td><td>{c['pop']}</td><td>{c['n_agents']}</td>"
            f"<td>{fmt(c['discovery_rate'], True)}</td><td>{fmt(c['first_contact'])}</td>"
            f"<td>{fmt(c['board_steps'])}</td><td>{free_fmt(c['board_steps_free'], na)}</td>"
            f"<td>{fmt(c['share_after_contact'], True)}</td><td>{free_fmt(c['share_free'], na, True)}</td>"
            f"<td>{fmt(c['posts'])}</td><td>{free_fmt(c['posts_free'], na)}</td><td>{fmt(c['episodes'])}</td>"
            f"<td>{fmt(c['solve_rate'], True)}</td><td>{fmt(c['solve_step'])}</td></tr>")


def doc_goal(doc: dict) -> str:
    return doc.get("goal") or DEFAULT_GOAL


def no_free_phase(fp: dict) -> str:
    """The free-time section body for a goal without a free phase; flags any free-phase step recorded."""
    text = ("<p>n/a: the flag cannot be found in this condition, so no agent reaches a free phase; the whole "
            "budget is task phase.</p>")
    if fp["steps"]:
        text += (f"<p><b>Check the runs:</b> {fp['steps']} free-phase steps were recorded, so a flag was "
                 f"accepted in some run.</p>")
    return text


def list_items(items: list[str]) -> str:
    return "".join(f"<li>{f}</li>" for f in items)


def quote_blocks(quotes: list[dict]) -> str:
    return "".join(f'<blockquote><p>{esc(q["text"])}</p><cite>{esc(q["who"])}</cite></blockquote>' for q in quotes)


def free_phase_bar(fp: dict) -> str:
    """Stacked bar + legend splitting all free-phase steps into idle turns, board steps and other commands."""
    total, idle, board = fp["steps"], fp["idle"], fp["board"]
    other = total - idle - board
    idle_pct, board_pct, other_pct = (f"{x / total:.0%}" for x in (idle, board, other))
    return f"""<div class="freebar" role="img" aria-label="Free-phase steps: {idle} idle, {board} on the board, {other} other">
      <span style="flex:{idle};background:var(--idle)" data-tip="idle turns: {idle} of {total}" tabindex="0"></span>
      <span style="flex:{board};background:var(--s1)" data-tip="board steps: {board} of {total}" tabindex="0"></span>
      <span style="flex:{other};background:var(--s2)" data-tip="other commands: {other} of {total}" tabindex="0"></span>
    </div>
    <div class="legend"><span><i style="background:var(--idle)"></i>idle turn, no tool call ({idle_pct})</span><span><i style="background:var(--s1)"></i>board ({board_pct})</span><span><i style="background:var(--s2)"></i>other commands ({other_pct})</span></div>"""


def render_report(analysis: dict, notes: dict) -> str:
    """The report page. `notes` may be empty: prose sections then render empty."""
    if doc_goal(analysis) != DEFAULT_GOAL:
        return render_report_nofree(analysis, notes)
    cells = ordered_cells(analysis["cells"])
    pooled = analysis["pooled"]
    total_tok = sum(c["tokens_prompt"] for c in cells)
    n_agent_runs = sum(c["n_agents"] for c in cells)
    headers = "".join(f"<th>{h}</th>" for h in TABLE_HEADERS)
    shape = experiment_shape(analysis)
    return f"""<title>Board Stickiness Pilot</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="{FONTS_URL}">
<style>
{STYLE}</style>
<main>
  <header style="display:flex;flex-direction:column;gap:10px">
    <span class="eyebrow">Schizbox · interface experiment · pilot</span>
    <h1>Which board makes agents stick?</h1>
    <p class="lede">{notes.get("lede", "")}</p>
  </header>
  <div class="facts">
    <div><b>{len(analysis["runs"])}</b><span>runs</span></div>
    <div><b>{n_agent_runs}</b><span>agent sessions</span></div>
    <div><b>{shape["calls"]}</b><span>bash calls per agent</span></div>
    <div><b>{total_tok / 1e6:.1f}M</b><span>prompt tokens</span></div>
  </div>
  <section><h2>Findings</h2><ul>{list_items(notes.get("findings", []))}</ul></section>
  <section>
    <h2>Results by interface</h2>
    <p>Each interface was run with {shape["agents"]} agents: {shape["solo_runs"]} solo runs plus {shape["multi_runs"]} runs of {shape["multi_size"]} live agents sharing one board. Bars are means; whiskers are bootstrap 95% intervals.</p>
    {pooled_bars(pooled, "contacted", "Agents that connected to the board", pct=True, ymax=1.0, note=f"Any measured traffic between the agent and the board during the {shape['calls']}-call session.")}
    {pooled_bars(pooled, "posted", "Agents that wrote at least one post", pct=True, ymax=1.0, note="A forum post, wiki edit, uploaded file or channel message.")}
    {pooled_bars(pooled, "board_steps_free", "Board steps after the task was done", note="Voluntary engagement: board steps in the free phase after the flag was accepted, mean per agent.")}
  </section>
  <section>
    <h2>Solo vs live peers</h2>
    <div class="legend"><span><i style="background:var(--s1)"></i>solo agent + seeded posts (n={shape["n_solo"]})</span><span><i style="background:var(--s2)"></i>{shape["multi_size"]} live agents + seeded posts (n={shape["n_multi"]})</span></div>
    {grouped_bars(cells, "posts", "Posts written per agent", note="Live peers did not raise posting much; their posts were parallel confirmations, not replies to each other.")}
  </section>
  <section>
    <h2>What agents did with free time</h2>
    {free_phase_bar(analysis["free_phase"])}
    <p>{notes.get("free_text", "")}</p>
  </section>
  <section><h2>All metrics by condition</h2>
  <div class="tablewrap"><table>
    <thead><tr>{headers}</tr></thead>
    <tbody>{"".join(table_row(c) for c in cells)}</tbody></table></div>
  <p style="font-size:.85rem;color:var(--muted)">Values are means per agent. In live-agent runs the four agents share a board, so they are not independent samples. "Visits" counts separate stretches of board activity.</p>
  </section>
  <section><h2>What agents wrote</h2><div class="quotes">{quote_blocks(notes.get("quotes", []))}</div></section>
  <section><h2>Setup</h2><ul>{list_items(notes.get("setup", []))}</ul></section>
  <section><h2>Caveats</h2><ul>{list_items(notes.get("caveats", []))}</ul></section>
  <section><h2>Next</h2><ul>{list_items(notes.get("next", []))}</ul></section>
</main>
<div id="tip" hidden></div>
<script>
{TOOLTIP_JS}</script>
"""


def render_report_nofree(analysis: dict, notes: dict) -> str:
    """render_report for a goal without a free phase (unsolvable): same page, free-phase parts n/a."""
    cells = ordered_cells(analysis["cells"])
    pooled = analysis["pooled"]
    total_tok = sum(c["tokens_prompt"] for c in cells)
    n_agent_runs = sum(c["n_agents"] for c in cells)
    solved = sum(1 for r in analysis["runs"] for a in r["agents"] if a.get("solved"))
    headers = "".join(f"<th>{h}</th>" for h in TABLE_HEADERS)
    shape = experiment_shape(analysis)
    label = GOAL_LABEL.get(doc_goal(analysis), doc_goal(analysis))
    return f"""<title>Board Stickiness Pilot · {esc(label)} Goal</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="{FONTS_URL}">
<style>
{STYLE}</style>
<main>
  <header style="display:flex;flex-direction:column;gap:10px">
    <span class="eyebrow">Schizbox · interface experiment · {esc(label.lower())} goal</span>
    <h1>Which board makes agents stick when the flag cannot be found?</h1>
    <p class="lede">{notes.get("lede", "")}</p>
  </header>
  <div class="facts">
    <div><b>{len(analysis["runs"])}</b><span>runs</span></div>
    <div><b>{n_agent_runs}</b><span>agent sessions</span></div>
    <div><b>{shape["calls"]}</b><span>bash calls per agent</span></div>
    <div><b>{total_tok / 1e6:.1f}M</b><span>prompt tokens</span></div>
    <div><b>{solved}</b><span>agents solved</span></div>
  </div>
  <section><h2>Findings</h2><ul>{list_items(notes.get("findings", []))}</ul></section>
  <section>
    <h2>Results by interface</h2>
    <p>Each interface was run with {shape["agents"]} agents: {shape["solo_runs"]} solo runs plus {shape["multi_runs"]} runs of {shape["multi_size"]} live agents sharing one board. No flag can be found, so every agent works on the task for its whole budget. Bars are means; whiskers are bootstrap 95% intervals.</p>
    {pooled_bars(pooled, "contacted", "Agents that connected to the board", pct=True, ymax=1.0, note=f"Any measured traffic between the agent and the board during the {shape['calls']}-call session.")}
    {pooled_bars(pooled, "posted", "Agents that wrote at least one post", pct=True, ymax=1.0, note="A forum post, wiki edit, uploaded file or channel message.")}
    {pooled_bars(pooled, "board_steps", "Board steps per agent", note="Board steps over the whole session, mean per agent (there is no free phase to count separately).")}
  </section>
  <section>
    <h2>Solo vs live peers</h2>
    <div class="legend"><span><i style="background:var(--s1)"></i>solo agent + seeded posts (n={shape["n_solo"]})</span><span><i style="background:var(--s2)"></i>{shape["multi_size"]} live agents + seeded posts (n={shape["n_multi"]})</span></div>
    {grouped_bars(cells, "posts", "Posts written per agent")}
  </section>
  <section>
    <h2>Free time</h2>
    {no_free_phase(analysis["free_phase"])}
    <p>{notes.get("free_text", "")}</p>
  </section>
  <section><h2>All metrics by condition</h2>
  <div class="tablewrap"><table>
    <thead><tr>{headers}</tr></thead>
    <tbody>{"".join(table_row(c, na=True) for c in cells)}</tbody></table></div>
  <p style="font-size:.85rem;color:var(--muted)">Values are means per agent. In live-agent runs the agents share a board, so they are not independent samples. "Visits" counts separate stretches of board activity. Free-phase columns are n/a: no flag can be found.</p>
  </section>
  <section><h2>What agents wrote</h2><div class="quotes">{quote_blocks(notes.get("quotes", []))}</div></section>
  <section><h2>Setup</h2><ul>{list_items(notes.get("setup", []))}</ul></section>
  <section><h2>Caveats</h2><ul>{list_items(notes.get("caveats", []))}</ul></section>
  <section><h2>Next</h2><ul>{list_items(notes.get("next", []))}</ul></section>
</main>
<div id="tip" hidden></div>
<script>
{TOOLTIP_JS}</script>
"""


# Per-model series colours for the comparison page (on top of STYLE), in by_model order.
COMPARE_STYLE = """:root { --m1:#2a78d6; --m2:#eb6834; --m3:#1f9e74; --m4:#8a5cd1 }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --m1:#3987e5; --m2:#d95926; --m3:#2bb487; --m4:#a07ae6 } }
:root[data-theme="dark"] { --m1:#3987e5; --m2:#d95926; --m3:#2bb487; --m4:#a07ae6 }
.chart .m1 { fill:var(--m1) } .chart .m2 { fill:var(--m2) } .chart .m3 { fill:var(--m3) } .chart .m4 { fill:var(--m4) }
.facts.model { border-left:3px solid var(--rule); padding-left:10px }
.facts.model h3 { grid-column:1/-1; margin:0; font-family:var(--display); font-size:1rem }
table.small { min-width:0 }
table.long th:nth-child(3), table.long td:nth-child(3) { text-align:left }
"""

RATE_HEADERS = ["model", "agent sessions", "ran full budget", "stalled", "crashed", "other end"]


def model_legend(by_model: dict) -> str:
    return '<div class="legend">' + "".join(
        f'<span><i style="background:var(--m{k + 1})"></i>{esc(m["label"])}</span>'
        for k, m in enumerate(by_model.values())) + "</div>"


def sessions(m: dict) -> int:
    return sum(m["ended"].values())


def model_facts(k: int, m: dict) -> str:
    """One facts row per model: runs, agent sessions, tokens, cost at list price."""
    cost = "–" if m["cost_eur"] is None else f"€{m['cost_eur']:.2f}"
    return f"""<div class="facts model" style="border-left-color:var(--m{k + 1})">
    <h3>{esc(m["label"])} <span class="mono">{esc(m["model"])}</span></h3>
    <div><b>{m["n_runs"]}</b><span>runs</span></div>
    <div><b>{sessions(m)}</b><span>agent sessions</span></div>
    <div><b>{m["tokens"]["prompt"] / 1e6:.1f}M</b><span>prompt tokens</span></div>
    <div><b>{m["tokens"]["completion"] / 1e3:.0f}k</b><span>completion tokens</span></div>
    <div><b>{cost}</b><span>est. cost ({esc(m["provider"])})</span></div>
  </div>"""


def rate(n: int, total: int) -> str:
    return f"{n} ({fmt(n / total, True)})" if total else "–"


def rate_row(m: dict) -> str:
    e, total = m["ended"], sessions(m)
    other = total - e["budget"] - e["stalled"] - e["crashed"]
    return (f"<tr><td>{esc(m['label'])}</td><td>{total}</td><td>{rate(e['budget'], total)}</td>"
            f"<td>{rate(e['stalled'], total)}</td><td>{rate(m['crashed_agents'], total)}</td>"
            f"<td>{rate(other, total)}</td></tr>")


def reps_caveat(by_model: dict) -> str:
    """Runs per (interface, population) cell for each model, and whether they are equal."""
    keys = sorted({c for m in by_model.values() for c in m["reps"]},
                  key=lambda c: (INTERFACES.index(c.split("-")[0]), c.split("-")[1] != "solo"))
    counts = {m["reps"].get(c, 0) for m in by_model.values() for c in keys}
    if not keys:
        return "<p>No runs.</p>"
    if len(counts) == 1:
        return f"<p>Every model has {counts.pop()} runs in each of the {len(keys)} conditions.</p>"
    head = "".join(f"<th>{esc(IFACE_LABEL[c.split('-')[0]])} · {c.split('-')[1]}</th>" for c in keys)
    rows = "".join(f"<tr><td>{esc(m['label'])}</td>" + "".join(f"<td>{m['reps'].get(c, 0)}</td>" for c in keys)
                   + "</tr>" for m in by_model.values())
    return f"""<p>Run counts differ between models or conditions (a budget stop can leave a block short), so intervals
  are wider where n is smaller and pooled means weight conditions unequally. Runs per condition:</p>
  <div class="tablewrap"><table class="small"><thead><tr><th>model</th>{head}</tr></thead><tbody>{rows}</tbody></table></div>"""


def render_comparison(doc: dict, notes: dict) -> str:
    """The per-model comparison page from analysis_models.json. `notes` may be empty."""
    by_model = doc["by_model"]
    goal = doc_goal(doc)
    na = goal != DEFAULT_GOAL
    label = GOAL_LABEL.get(goal, goal)
    headers = "".join(f"<th>{h}</th>" for h in ["model", *TABLE_HEADERS])
    long_rows = "".join(table_row(c, na).replace("<tr>", f"<tr><td>{esc(m['label'])}</td>", 1)
                        for m in by_model.values() for c in ordered_cells(m["cells"]))
    title = "Board Stickiness by Model" + (f" · {esc(label)} Goal" if na else "")
    eyebrow = "models" + (f" · {esc(label.lower())} goal" if na else "")
    third = (model_bars(by_model, "board_steps", "Board steps per agent",
                        note="Board steps over the whole session, mean per agent (no flag can be found, "
                             "so there is no free phase).") if na else
             model_bars(by_model, "board_steps_free", "Board steps after the task was done",
                        note="Board steps in the free phase after the flag was accepted, mean per agent."))
    table_note = " Free-phase columns are n/a: no flag can be found." if na else ""
    solo_multi = "".join(grouped_bars(m["cells"], "posts", f"{m['label']}: posts written per agent")
                         for m in by_model.values())
    pops = "".join(f'<span><i style="background:var(--s{i + 1})"></i>{p}</span>' for i, p in enumerate(POPULATIONS))
    return f"""<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="{FONTS_URL}">
<style>
{STYLE}{COMPARE_STYLE}</style>
<main>
  <header style="display:flex;flex-direction:column;gap:10px">
    <span class="eyebrow">Schizbox · interface experiment · {eyebrow}</span>
    <h1>Do other models stick to the same boards?</h1>
    <p class="lede">{notes.get("lede", "")}</p>
  </header>
  {"".join(model_facts(k, m) for k, m in enumerate(by_model.values()))}
  <section><h2>Findings</h2><ul>{list_items(notes.get("findings", []))}</ul></section>
  <section>
    <h2>Results by interface and model</h2>
    <p>Solo and live agents pooled per interface. Bars are means; whiskers are bootstrap 95% intervals.</p>
    {model_legend(by_model)}
    {model_bars(by_model, "contacted", "Agents that connected to the board", pct=True, ymax=1.0, note="Any measured traffic between the agent and the board during the session.")}
    {model_bars(by_model, "posted", "Agents that wrote at least one post", pct=True, ymax=1.0, note="A forum post, wiki edit, uploaded file or channel message.")}
    {third}
  </section>
  <section>
    <h2>How sessions ended</h2>
    <div class="tablewrap"><table class="small">
      <thead><tr>{"".join(f"<th>{h}</th>" for h in RATE_HEADERS)}</tr></thead>
      <tbody>{"".join(rate_row(m) for m in by_model.values())}</tbody></table></div>
    <p style="font-size:.85rem;color:var(--muted)">"Stalled" sessions hit the idle cut-off (turns without a tool call), which also catches replies truncated at max_tokens. Crashed sessions ended on a harness or provider error.</p>
  </section>
  <section>
    <h2>Solo vs live peers, per model</h2>
    <div class="legend">{pops}</div>
    {solo_multi}
  </section>
  <section><h2>All metrics by model and condition</h2>
  <div class="tablewrap"><table class="long">
    <thead><tr>{headers}</tr></thead>
    <tbody>{long_rows}</tbody></table></div>
  <p style="font-size:.85rem;color:var(--muted)">Values are means per agent. Raw token counts are not comparable across models (tokenizers differ); compare cost instead. Cost is estimated from summary token counts at list price.{table_note}</p>
  </section>
  <section><h2>Unequal n</h2>{reps_caveat(by_model)}</section>
  <section><h2>Caveats</h2><ul>{list_items(notes.get("caveats", []))}</ul></section>
</main>
<div id="tip" hidden></div>
<script>
{TOOLTIP_JS}</script>
"""


# Goal comparison page (analysis_goals.json): series colours m1 = solvable, m2 = unsolvable (COMPARE_STYLE).
GOAL_TABLE_HEADERS = ["model", "goal", "interface", "population", "agents", "contacted", "first contact step",
                      "board steps", "board share", "share after contact", "share in 2nd half", "active in 2nd half",
                      "posts", "posted", "visits", "replies to peers", "DMs to peers", "posts naming peers", "solved",
                      "board steps after solve", "share of free phase", "posts after solve"]
SOLVE_HEADERS = ["model", "goal", "runs", "agent sessions", "solved", "ran full budget", "stalled", "crashed",
                 "live-live interactions", "est. cost"]
# (pooled key, title, pct, note) of the per-model goal charts.
GOAL_CHARTS = [
    ("posted", "Agents that wrote at least one post", True,
     "A forum post, wiki edit, uploaded file or channel message, at any point in the session."),
    ("share_board", "Share of the session spent on the board", True,
     "Board steps over all steps, mean per agent; comparable across goals because it ignores the phases."),
    ("share_late", "Board share in the second half of the session", True,
     "Persistence: board steps among the agent's later steps, when a solving agent is often already done."),
    ("interactions", "Replies and DMs to live peers", False,
     "Live-agent runs only: posts into a thread a live peer posted in first, plus DMs to a live peer's nick."),
]


def goal_series(m: dict) -> dict:
    """A model's goals as model_bars series: label and pooled rows per goal."""
    return {g: {"label": GOAL_LABEL.get(g, g), "pooled": b["pooled"]} for g, b in m["by_goal"].items()}


def goal_legend(goals) -> str:
    return '<div class="legend">' + "".join(
        f'<span><i style="background:var(--m{k + 1})"></i>{esc(GOAL_LABEL.get(g, g))}</span>'
        for k, g in enumerate(goals)) + "</div>"


def goal_row(label: str, goal: str, c: dict) -> str:
    """One row of the long goal table; free-phase columns n/a outside the default goal."""
    na = goal != DEFAULT_GOAL
    return (f"<tr><td>{esc(label)}</td><td>{esc(GOAL_LABEL.get(goal, goal))}</td>"
            f"<td>{esc(IFACE_LABEL[c['iface']])}</td><td>{c['pop']}</td><td>{c['n_agents']}</td>"
            f"<td>{fmt(c['discovery_rate'], True)}</td><td>{fmt(c['first_contact'])}</td>"
            f"<td>{fmt(c['board_steps'])}</td><td>{fmt(c['share_board'], True)}</td>"
            f"<td>{fmt(c['share_after_contact'], True)}</td><td>{fmt(c['share_late'], True)}</td>"
            f"<td>{fmt(c['active_late'], True)}</td><td>{fmt(c['posts'])}</td><td>{fmt(c['posted'], True)}</td>"
            f"<td>{fmt(c['episodes'])}</td><td>{fmt(c['replies'])}</td><td>{fmt(c['dms_live'])}</td>"
            f"<td>{fmt(c['mentions_live'])}</td><td>{fmt(c['solve_rate'], True)}</td>"
            f"<td>{free_fmt(c['board_steps_free'], na)}</td><td>{free_fmt(c['share_free'], na, True)}</td>"
            f"<td>{free_fmt(c['posts_free'], na)}</td></tr>")


def solve_row(label: str, goal: str, b: dict) -> str:
    e = b["ended"]
    total = sum(e.values())
    cost = "–" if b["cost_eur"] is None else f"€{b['cost_eur']:.2f}"
    return (f"<tr><td>{esc(label)}</td><td>{esc(GOAL_LABEL.get(goal, goal))}</td><td>{b['n_runs']}</td>"
            f"<td>{b['n_agents']}</td><td>{rate(b['solved_agents'], b['n_agents'])}</td>"
            f"<td>{rate(e['budget'], total)}</td><td>{rate(e['stalled'], total)}</td>"
            f"<td>{rate(e['crashed'], total)}</td>"
            f"<td>{b['live_interactions']}</td><td>{cost}</td></tr>")


def goal_model_section(m: dict) -> str:
    """Per-model charts: one bar per goal and interface."""
    series = goal_series(m)
    charts = "".join(model_bars(series, key, f"{m['label']}: {title}", pct=pct, ymax=1.0 if pct else None, note=note)
                     for key, title, pct, note in GOAL_CHARTS)
    return f"""<section>
    <h2>{esc(m["label"])} <span class="mono">{esc(m["model"])}</span></h2>
    {goal_legend(m["by_goal"])}
    {charts}
  </section>"""


def unsolved_check(by_model: dict) -> str:
    """A warning when any unsolvable-goal agent had a flag accepted (the condition did not hold)."""
    bad = [f"{esc(m['label'])} ({b['solved_agents']})" for m in by_model.values()
           for g, b in m["by_goal"].items() if g != DEFAULT_GOAL and b["solved_agents"]]
    if not bad:
        return ""
    return f"<p><b>Check the runs:</b> unsolvable-goal agents had a flag accepted: {', '.join(bad)}.</p>"


def render_goals(doc: dict, notes: dict) -> str:
    """The solvable-vs-unsolvable page from analysis_goals.json. `notes` may be empty."""
    by_model = doc["by_model"]
    long_rows = "".join(goal_row(m["label"], g, c) for m in by_model.values()
                        for g, b in m["by_goal"].items() for c in ordered_cells(b["cells"]))
    solve_rows = "".join(solve_row(m["label"], g, b) for m in by_model.values() for g, b in m["by_goal"].items())
    return f"""<title>Board Stickiness: Solvable vs Unsolvable Goal</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="{FONTS_URL}">
<style>
{STYLE}{COMPARE_STYLE}</style>
<main>
  <header style="display:flex;flex-direction:column;gap:10px">
    <span class="eyebrow">Schizbox · interface experiment · goals</span>
    <h1>Do agents use the board more when the flag cannot be found?</h1>
    <p class="lede">{notes.get("lede", "")}</p>
  </header>
  <section><h2>Findings</h2><ul>{list_items(notes.get("findings", []))}</ul></section>
  <section>
    <h2>Sessions and solves</h2>
    <div class="tablewrap"><table class="small">
      <thead><tr>{"".join(f"<th>{h}</th>" for h in SOLVE_HEADERS)}</tr></thead>
      <tbody>{solve_rows}</tbody></table></div>
    <p style="font-size:.85rem;color:var(--muted)">In the unsolvable condition no flag the checker accepts exists, so the solve rate should be 0% and every session is task phase for its whole budget. Live-live interactions count replies and DMs between live agents of the same run.</p>
    {unsolved_check(by_model)}
  </section>
  <p>Metrics here need no free phase, so they compare the goals directly; free-phase metrics are n/a for the unsolvable goal. Solo and live agents are pooled per interface. Bars are means; whiskers are bootstrap 95% intervals.</p>
  {"".join(goal_model_section(m) for m in by_model.values())}
  <section><h2>All task-phase metrics by model, goal and condition</h2>
  <div class="tablewrap"><table class="long">
    <thead><tr>{"".join(f"<th>{h}</th>" for h in GOAL_TABLE_HEADERS)}</tr></thead>
    <tbody>{long_rows}</tbody></table></div>
  <p style="font-size:.85rem;color:var(--muted)">Values are means per agent. "2nd half" is the later half of an agent's steps. Peer columns are – for solo agents, which have no live peer. Free-phase columns are n/a for the unsolvable goal.</p>
  </section>
  <section><h2>Caveats</h2><ul>{list_items(notes.get("caveats", []))}</ul></section>
</main>
<div id="tip" hidden></div>
<script>
{TOOLTIP_JS}</script>
"""
