"""Server-rendered SVG chart of a room's history: indoor temperature, the target band,
outdoor temperature, and a strip showing when heating/cooling ran.

Colors come from CSS classes (app.css), so the chart follows light/dark mode. No JS
library; a small script on the page adds the hover readout from `data-points`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from markupsafe import Markup, escape

from app.insights import Point

W, H = 800, 250
LEFT, RIGHT, TOP, BOTTOM = 34, 8, 10, 40  # bottom = runtime strip + x labels
STRIP = 12
GAP = timedelta(minutes=16)  # longer than this between points → break the line


def room_chart(points: list[Point], start: datetime, end: datetime, tz: str) -> Markup:
    zone = ZoneInfo(tz)
    plot_h = H - TOP - BOTTOM
    temps = [v for p in points for v in (p.temp, p.outdoor) if v is not None]
    if not temps:
        return Markup('<div class="chart-empty">No history yet for this period.</div>')
    lo, hi = min(temps), max(temps)
    # Include the target band only where it's near the readings (a 45–90 setback would
    # flatten everything).
    for p in points:
        for v in (p.heat_sp, p.cool_sp):
            if v is not None and lo - 6 <= v <= hi + 6:
                lo, hi = min(lo, v), max(hi, v)
    lo, hi = 5 * int((lo - 1) // 5), 5 * int((hi + 1) // 5 + 1)
    span = (end - start).total_seconds()

    def x(t: datetime) -> float:
        return LEFT + (W - LEFT - RIGHT) * (t - start).total_seconds() / span

    def y(v: float) -> float:
        v = min(max(v, lo), hi)
        return TOP + plot_h * (hi - v) / (hi - lo)

    def path(attr: str) -> str:
        d, prev = [], None
        for p in points:
            v = getattr(p, attr)
            if v is None:
                prev = None
                continue
            cmd = "L" if prev and p.at - prev <= GAP else "M"
            d.append(f"{cmd}{x(p.at):.1f},{y(v):.1f}")
            prev = p.at
        return " ".join(d)

    parts = [(f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" '
              'aria-label="Indoor temperature, target band and outdoor temperature">')]

    # grid + y labels
    for v in range(lo, hi + 1, 5):
        parts.append(f'<line class="grid" x1="{LEFT}" x2="{W - RIGHT}" y1="{y(v):.1f}" '
                     f'y2="{y(v):.1f}"/><text class="ylab" x="{LEFT - 6}" y="{y(v) + 4:.1f}">'
                     f'{v}°</text>')

    # x labels: midnights (and noon on short ranges)
    local = start.astimezone(zone).replace(minute=0, second=0, microsecond=0)
    step = timedelta(hours=6) if span <= 2 * 86400 else timedelta(days=1)
    t = local.replace(hour=0) if step >= timedelta(days=1) else local.replace(
        hour=local.hour - local.hour % 6)
    while t <= end:
        if t >= start:
            label = t.strftime("%a") if t.hour == 0 and step >= timedelta(days=1) else (
                t.strftime("%a") if t.hour == 0 else t.strftime("%-I %p"))
            parts.append(f'<line class="grid v" x1="{x(t):.1f}" x2="{x(t):.1f}" y1="{TOP}" '
                         f'y2="{H - BOTTOM + STRIP + 4}"/><text class="xlab" x="{x(t):.1f}" '
                         f'y="{H - 8}">{label}</text>')
        t = (t + step).astimezone(zone)  # re-localize across DST

    # target band (step shape: a setpoint holds until the next sample)
    band = []
    for i, p in enumerate(points):
        if p.heat_sp is None or p.cool_sp is None:
            continue
        nxt = points[i + 1].at if i + 1 < len(points) and points[i + 1].at - p.at <= GAP \
            else p.at + timedelta(minutes=5)
        band.append(f'<rect class="band" x="{x(p.at):.1f}" y="{y(p.cool_sp):.1f}" '
                    f'width="{max(x(nxt) - x(p.at), 0.5):.1f}" '
                    f'height="{max(y(p.heat_sp) - y(p.cool_sp), 0):.1f}"/>')
    parts.append('<g class="bands">' + "".join(band) + "</g>")

    parts.append(f'<path class="sp heat" d="{path("heat_sp")}"/>')
    parts.append(f'<path class="sp cool" d="{path("cool_sp")}"/>')
    parts.append(f'<path class="outdoor" d="{path("outdoor")}"/>')
    parts.append(f'<path class="indoor" d="{path("temp")}"/>')

    # runtime strip
    sy = H - BOTTOM + 2
    strip = []
    for p in points:
        w = max(x(p.at + timedelta(minutes=5)) - x(p.at), 0.6)
        for cls, s in (("run-heat", p.heat_s), ("run-cool", p.cool_s)):
            if s:
                strip.append(f'<rect class="{cls}" x="{x(p.at):.1f}" y="{sy}" width="{w:.1f}" '
                             f'height="{STRIP}" opacity="{min(s / 300, 1):.2f}"/>')
    parts.append(f'<rect class="strip-bg" x="{LEFT}" y="{sy}" width="{W - LEFT - RIGHT}" '
                 f'height="{STRIP}"/>' + "".join(strip))

    parts.append(f'<line class="cursor" x1="0" x2="0" y1="{TOP}" y2="{H - BOTTOM + STRIP + 2}" '
                 f'visibility="hidden"/>')
    parts.append("</svg>")

    data = [[round(x(p.at), 1), p.at.astimezone(zone).strftime("%a %-I:%M %p"), p.temp,
             p.heat_sp, p.cool_sp, p.outdoor,
             "heating" if p.heat_s else "cooling" if p.cool_s else ""] for p in points]
    return Markup(f'<div class="chartwrap" data-points="{escape(json.dumps(data))}" '
                  f'data-w="{W}">') + Markup("".join(parts)) + Markup(
        '<div class="readout" hidden></div></div>')
