"""Small server-side SVG charts (no JS dependency, print friendly, theme aware through CSS classes in app.css)."""
from html import escape


def _nice(maxv):
    if maxv <= 0:
        return 1
    import math
    e = 10 ** int(math.floor(math.log10(maxv)))
    for m in (1, 2, 2.5, 5, 10):
        if maxv <= m * e:
            return m * e
    return 10 * e


def bars(labels, series, *, width=900, height=280, title="", unit=""):
    """series = [(name, css_class, [values|None], kind)] where kind is 'bar' or 'line'."""
    n = len(labels)
    vals = [v for _, _, vs, _ in series for v in vs if v is not None]
    top = _nice(max(vals) if vals else 1)
    lo = min([0] + [float(v) for v in vals])
    span = (top - lo) or 1
    l, r, t, b = 60, 16, 34, 34
    pw, ph = width - l - r, height - t - b
    y = lambda v: t + ph - (float(v) - lo) / span * ph
    out = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="{escape(title)}">', f'<title>{escape(title)}</title>']
    for i in range(5):
        v = lo + span * i / 4
        out.append(f'<line class="ch-grid" x1="{l}" x2="{width - r}" y1="{y(v):.1f}" y2="{y(v):.1f}"/><text class="ch-tick" x="{l - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{_short(v)}</text>')
    bar_series = [s for s in series if s[3] == "bar"]
    group = pw / max(n, 1)
    bw = min(34, group * 0.7 / max(len(bar_series), 1))
    for i, lab in enumerate(labels):
        cx = l + group * (i + 0.5)
        out.append(f'<text class="ch-tick" x="{cx:.1f}" y="{height - 12}" text-anchor="middle">{escape(str(lab).split()[0])}</text>')
        for j, (name, cls, vs, kind) in enumerate(bar_series):
            v = vs[i]
            if v is None:
                continue
            x0 = cx - bw * len(bar_series) / 2 + j * bw
            y0, yz = y(v), y(0)
            out.append(f'<rect class="{cls}" x="{x0:.1f}" y="{min(y0, yz):.1f}" width="{bw - 2:.1f}" height="{abs(yz - y0):.1f}" rx="2"><title>{escape(name)} {escape(str(lab))}: {float(v):,.1f}{unit}</title></rect>')
    for name, cls, vs, kind in series:
        if kind != "line":
            continue
        pts = [(l + group * (i + 0.5), y(v)) for i, v in enumerate(vs) if v is not None]
        if len(pts) > 1:
            out.append(f'<polyline class="{cls}" fill="none" points="{" ".join(f"{x:.1f},{yy:.1f}" for x, yy in pts)}"/>')
        for x, yy in pts:
            out.append(f'<circle class="{cls}-dot" cx="{x:.1f}" cy="{yy:.1f}" r="3"/>')
    lx = l
    for name, cls, _, kind in series:
        out.append(f'<rect class="{cls if kind == "bar" else cls + "-key"}" x="{lx}" y="8" width="12" height="12" rx="2"/><text class="ch-tick" x="{lx + 17}" y="18">{escape(name)}</text>')
        lx += 20 + 7 * len(name)
    out.append("</svg>")
    return "".join(out)


def _short(v):
    v = float(v)
    for lim, s in ((1e6, "M"), (1e3, "k")):
        if abs(v) >= lim:
            return f"{v / lim:.1f}{s}".replace(".0", "")
    return f"{v:.0f}" if abs(v) >= 10 else f"{v:.1f}"
