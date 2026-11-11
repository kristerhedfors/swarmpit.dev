"""Inline SVG bar charts for the HTML report (styled by the report's .chart CSS classes)."""
import html

from config import IFACE_LABEL, INTERFACES

POPULATIONS = ("solo", "multi")


def esc(s) -> str:
    return html.escape(str(s))


def fmt(v, pct: bool = False, nd: int = 1) -> str:
    """Display a value: en dash for missing, whole percent, or nd decimals."""
    if v is None:
        return "–"
    return f"{v * 100:.0f}%" if pct else f"{v:.{nd}f}"


def nice_axis(raw_top, pct):
    """Round the axis top up to a tidy step so every tick label is an exact value."""
    if pct:
        return 1.0, 0.25
    for step in (0.25, 0.5, 1, 2, 5, 10, 20, 50):
        if raw_top / step <= 5:
            n = max(1, -(-raw_top // step))
            return n * step, step
    return raw_top, raw_top / 4


def bar_path(x: float, yy: float, bw: float, base: float, r: float) -> str:
    """Bar from the baseline up to yy with rounded top corners of radius r."""
    return (f"M{x:.1f},{base} V{yy + r:.1f} Q{x:.1f},{yy:.1f} {x + r:.1f},{yy:.1f} "
            f"H{x + bw - r:.1f} Q{x + bw:.1f},{yy:.1f} {x + bw:.1f},{yy + r:.1f} V{base} Z")


def figure(title: str, note: str, svg: list[str]) -> str:
    return (f'<figure><figcaption><b>{esc(title)}</b>{"<span>" + esc(note) + "</span>" if note else ""}'
            f'</figcaption>{"".join(svg)}</figure>')


def svg_open(w: int, h: int, title: str) -> str:
    return f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{esc(title)}" class="chart">'


def gridline(left: int, w: int, yy: float, label: str) -> list[str]:
    return [f'<line x1="{left}" x2="{w - 10}" y1="{yy:.1f}" y2="{yy:.1f}" class="grid"/>',
            f'<text x="{left - 6}" y="{yy + 4:.1f}" class="tick" text-anchor="end">{label}</text>']


def axis_label(cx: float, y: int, iface: str) -> str:
    return f'<text x="{cx:.1f}" y="{y}" class="axis" text-anchor="middle">{esc(IFACE_LABEL[iface])}</text>'


def baseline(left: int, w: int, base: int) -> str:
    return f'<line x1="{left}" x2="{w - 10}" y1="{base}" y2="{base}" class="base"/></svg>'


def grouped_bars(cells, key, title, pct=False, ymax=None, note=""):
    """Grouped vertical bars: one group per interface, solo vs multi."""
    W, H, L, B, T = 640, 260, 44, 40, 16
    vals = [(c["iface"], c["pop"], c.get(key)) for c in cells]
    top, step = nice_axis(ymax or max([v or 0 for *_, v in vals] + [1e-9]) * 1.1, pct)
    ticks = round(top / step)
    gw = (W - L - 10) / len(INTERFACES)
    bw = min(34, gw / 3)
    y = lambda v: T + (H - T - B) * (1 - (v or 0) / top)
    out = [svg_open(W, H, title)]
    for i in range(ticks + 1):
        v = top * i / ticks
        out += gridline(L, W, y(v), fmt(v, pct) if pct else f"{v:g}")
    for gi, iface in enumerate(INTERFACES):
        cx = L + gw * gi + gw / 2
        for si, pop in enumerate(POPULATIONS):
            v = next((vv for f, p, vv in vals if f == iface and p == pop), None)
            x = cx + (si - 1) * (bw + 2) + 1
            if v is None:
                out.append(f'<text x="{x + bw / 2:.1f}" y="{H - B - 4}" class="tick" text-anchor="middle">n/a</text>')
                continue
            yy = y(v)
            r = min(4, max(0.0, H - B - yy) / 2)
            label = f"{IFACE_LABEL[iface]} · {pop}: {fmt(v, pct, 2)}"
            out.append(f'<path d="{bar_path(x, yy, bw, H - B, r)}" class="bar s{si + 1}" '
                       f'data-tip="{esc(label)}" tabindex="0"/>')
            out.append(f'<text x="{x + bw / 2:.1f}" y="{yy - 5:.1f}" class="val" text-anchor="middle">'
                       f'{fmt(v, pct)}</text>')
        out.append(axis_label(cx, H - B + 18, iface))
    out.append(baseline(L, W, H - B))
    return figure(title, note, out)


def pooled_bars(pooled, key, title, pct=False, note="", ymax=None):
    """One bar per interface (solo + live agents pooled) with a bootstrap 95% CI whisker."""
    W, H, L, B, T = 640, 250, 44, 40, 14
    rows = {r["iface"]: r for r in pooled}
    top, step = nice_axis(ymax or max(rows[i][key + "_ci"][1] for i in INTERFACES) * 1.05, pct)
    y = lambda v: T + (H - T - B) * (1 - v / top)
    gw = (W - L - 10) / len(INTERFACES)
    bw = min(56, gw * 0.45)
    out = [svg_open(W, H, title)]
    for i in range(round(top / step) + 1):
        v = step * i
        out += gridline(L, W, y(v), fmt(v, True) if pct else f"{v:g}")
    for gi, iface in enumerate(INTERFACES):
        r = rows[iface]
        v, (lo, hi) = r[key], r[key + "_ci"]
        cx = L + gw * gi + gw / 2
        x, yy = cx - bw / 2, y(v)
        rr = min(4, max(0.0, H - B - yy) / 2)
        tip = f"{IFACE_LABEL[iface]}: {fmt(v, pct, 2)} (95% CI {fmt(lo, pct, 2)}–{fmt(hi, pct, 2)}, n={r['n_agents']})"
        out.append(f'<path d="{bar_path(x, yy, bw, H - B, rr)}" class="bar s1" data-tip="{esc(tip)}" tabindex="0"/>')
        out.append(f'<line x1="{cx:.1f}" x2="{cx:.1f}" y1="{y(lo):.1f}" y2="{y(hi):.1f}" class="ci"/>')
        for yv in (lo, hi):
            out.append(f'<line x1="{cx - 6:.1f}" x2="{cx + 6:.1f}" y1="{y(yv):.1f}" y2="{y(yv):.1f}" class="ci"/>')
        out.append(f'<text x="{cx + bw / 2 + 6:.1f}" y="{yy + 4:.1f}" class="val">{fmt(v, pct)}</text>')
        out.append(axis_label(cx, H - B + 18, iface))
    out.append(baseline(L, W, H - B))
    return figure(title, note, out)


def model_bars(by_model, key, title, pct=False, ymax=None, note=""):
    """Grouped bars from analysis_models.json: one group per interface, one bar per model (class m1, m2, ...)
    with its pooled bootstrap 95% CI whisker; "n/a" where a model has no run of that interface."""
    W, H, L, B, T = 640, 270, 44, 40, 16
    models = list(by_model.items())
    rows = [{p["iface"]: p for p in m["pooled"]} for _, m in models]
    highs = [v for r in rows for p in r.values() for v in (p.get(key), (p.get(key + "_ci") or [None, None])[1])
             if v is not None]
    top, step = nice_axis(ymax or max(highs + [1e-9]) * 1.05, pct)
    y = lambda v: T + (H - T - B) * (1 - v / top)
    gw = (W - L - 10) / len(INTERFACES)
    bw = min(30, (gw - 12) / max(1, len(models)) - 2)
    out = [svg_open(W, H, title)]
    for i in range(round(top / step) + 1):
        v = step * i
        out += gridline(L, W, y(v), fmt(v, True) if pct else f"{v:g}")
    for gi, iface in enumerate(INTERFACES):
        cx = L + gw * gi + gw / 2
        x0 = cx - len(models) * (bw + 2) / 2
        for k, ((_, m), r) in enumerate(zip(models, rows)):
            x = x0 + k * (bw + 2) + 1
            p = r.get(iface)
            v = None if p is None else p.get(key)
            if v is None:
                out.append(f'<text x="{x + bw / 2:.1f}" y="{H - B - 4}" class="tick" text-anchor="middle">n/a</text>')
                continue
            lo, hi = p.get(key + "_ci") or [None, None]
            yy = y(v)
            rr = min(4, max(0.0, H - B - yy) / 2)
            ci = "" if lo is None else f" (95% CI {fmt(lo, pct, 2)}–{fmt(hi, pct, 2)})"
            tip = f"{m['label']} · {IFACE_LABEL[iface]}: {fmt(v, pct, 2)}{ci}, n={p['n_agents']}"
            out.append(f'<path d="{bar_path(x, yy, bw, H - B, rr)}" class="bar m{k + 1}" '
                       f'data-tip="{esc(tip)}" tabindex="0"/>')
            bx = x + bw / 2
            if lo is not None:
                out.append(f'<line x1="{bx:.1f}" x2="{bx:.1f}" y1="{y(lo):.1f}" y2="{y(hi):.1f}" class="ci"/>')
                for yv in (lo, hi):
                    out.append(f'<line x1="{bx - 3:.1f}" x2="{bx + 3:.1f}" y1="{y(yv):.1f}" y2="{y(yv):.1f}" '
                               f'class="ci"/>')
            label_y = min(yy, y(hi) if hi is not None else yy) - 5
            out.append(f'<text x="{bx:.1f}" y="{label_y:.1f}" class="val" text-anchor="middle">{fmt(v, pct)}</text>')
        out.append(axis_label(cx, H - B + 18, iface))
    out.append(baseline(L, W, H - B))
    return figure(title, note, out)
