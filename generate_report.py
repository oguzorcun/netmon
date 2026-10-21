#!/usr/bin/env python3
"""
generate_report.py
Reads ping_monitor.sh's CSV log and renders an HTML page (inline SVG, no
external JS/CSS, no internet required) with two latency graphs - a
fine-grained last-1h view and the full last-24h view - plus summary stats
for each.

Run manually any time:
    python3 generate_report.py
Then open ~/netmon/report.html in a browser. The page live-refreshes itself
every 10s, and a manual browser reload always renders the freshest data.

How the live part works: the launchd agent runs this script every few
seconds. Besides report.html it writes report_data.js, which the page pulls
with a cache-busting <script> tag every 10s (works even when opened as a
file:// URL, where fetch()/XHR of local files is blocked). Nothing is served
over the network.

For a page reload that actually measures a fresh ping, view the report
through report_server.py (http://127.0.0.1:8765/) instead of the file. That
server runs this module's rendering on demand and exposes a ping endpoint the
page hits on load.
"""

import csv
import json
import os
import statistics
from datetime import datetime, timedelta, timezone

HOME = os.path.expanduser("~")
LOGFILE = os.path.join(HOME, "Library", "Logs", "netmon", "latency.csv")
OUTFILE = os.path.join(HOME, "netmon", "report.html")
DATA_FILE = os.path.join(HOME, "netmon", "report_data.js")
REFRESH_SECONDS = 10
WINDOW_HOURS_LONG = 24
WINDOW_HOURS_SHORT = 1
MAX_Y_MS = 300  # y-axis ceiling; pings above this get flagged at the top edge


def atomic_write(path, text):
    """Write via a temp file + rename so the browser never sees a half-written file."""
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def load_rows(window_hours):
    if not os.path.exists(LOGFILE):
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    rows = []
    with open(LOGFILE, newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                ts = datetime.strptime(r["timestamp"], "%Y-%m-%dT%H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
            except (ValueError, KeyError):
                continue
            if ts < cutoff:
                continue
            latency = None
            if r.get("latency_ms"):
                try:
                    latency = float(r["latency_ms"])
                except ValueError:
                    latency = None
            # Defensive filter for old log rows written before the sanity cap
            # existed in ping_monitor.sh: a "latency" over 5s is almost certainly
            # a sleep/wake artifact, not a real reply. Treat it as a gap, not a spike.
            if latency is not None and latency > 5000:
                latency = None
            # Convert from UTC (as logged) to local system time for display
            ts = ts.astimezone()
            rows.append((ts, latency, r.get("status", "")))
    rows.sort(key=lambda x: x[0])
    return rows


def nice_y_max(value):
    """Round the axis top up to a readable value with evenly divisible ticks."""
    for candidate in (20, 30, 40, 50, 60, 80, 100, 150, 200, 250, MAX_Y_MS):
        if value <= candidate:
            return candidate
    return MAX_Y_MS


def choose_y_max(latencies, span_secs):
    """Pick a readable, stable axis top.

    A short window is scaled to p95 so minute-to-minute variation is legible.
    A day-long window is scaled to p99.5 instead, because a handful of spikes
    in 24h shouldn't flatten the other 23 hours into a strip. Either way the
    axis sits just above the bulk of the data, and anything over the top edge
    is flagged. Never past the MAX_Y_MS cap.
    """
    if not latencies:
        return 100
    s = sorted(latencies)
    n = len(s)
    q = 0.995 if span_secs > 6 * 3600 else 0.95
    base = s[min(n - 1, max(0, int(round(n * q)) - 1))]
    target = max(base * 1.15, 30)
    target = min(target, s[-1], MAX_Y_MS)  # never exceed the actual max or the cap
    return nice_y_max(max(target, 20))


def build_svg(rows, width=1000, height=360, pad_l=52, pad_r=16, pad_t=22, pad_b=38,
              x_label_step=None, x_label_fmt="%H:%M", grad_id="area"):
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    base_y = pad_t + plot_h

    if not rows:
        return (
            f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">'
            f'<text x="{width / 2}" y="{height / 2}" fill="#5d686d" font-size="12" '
            f'text-anchor="middle">No data yet — let the logger run for a while.</text></svg>'
        )

    t0 = rows[0][0]
    t1 = rows[-1][0]
    span = max((t1 - t0).total_seconds(), 1)

    # Default x-axis label spacing scales with how wide a window we're plotting:
    # dense minute-level ticks for a 1h view, hourly ticks for a 24h view.
    if x_label_step is None:
        if span <= 2 * 3600:
            x_label_step = timedelta(minutes=10)
        elif span <= 6 * 3600:
            x_label_step = timedelta(minutes=30)
        else:
            x_label_step = timedelta(hours=2)

    latencies = [l for _, l, s in rows if l is not None]
    y_max = choose_y_max(latencies, span)

    def x_of(ts):
        return pad_l + (ts - t0).total_seconds() / span * plot_w

    def y_of(val):
        v = min(val, y_max)
        return base_y - (v / y_max * plot_h)

    # horizontal grid + y labels (the unit rides on the top tick only)
    grid = []
    n_grid = 5
    for i in range(n_grid + 1):
        val = y_max * i / n_grid
        y = base_y - (val / y_max * plot_h)
        unit = " ms" if i == n_grid else ""
        grid.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
            f'stroke="rgba(255,255,255,0.055)" stroke-width="1"/>'
            f'<text x="{pad_l - 12}" y="{y + 3.5:.1f}" fill="#5d686d" font-size="10.5" '
            f'text-anchor="end">{val:.0f}{unit}</text>'
        )

    # x labels, spaced by x_label_step
    x_labels = []
    step_secs = x_label_step.total_seconds()
    if step_secs >= 3600:
        cur = t0.replace(minute=0, second=0, microsecond=0)
    else:
        # snap down to the nearest step boundary within the hour
        minute_bucket = (t0.minute // int(step_secs // 60)) * int(step_secs // 60)
        cur = t0.replace(minute=minute_bucket, second=0, microsecond=0)
    if cur < t0:
        cur += x_label_step
    while cur <= t1:
        x = x_of(cur)
        x_labels.append(
            f'<line x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" y2="{base_y}" '
            f'stroke="rgba(255,255,255,0.04)" stroke-width="1" stroke-dasharray="2,6"/>'
            f'<text x="{x:.1f}" y="{height - 14}" fill="#5d686d" font-size="10.5" '
            f'text-anchor="middle">{cur.strftime(x_label_fmt)}</text>'
        )
        cur += x_label_step

    # line path + points, with gaps where timeouts occur
    path_segments = []
    current_seg = []
    timeout_marks = []
    spike_marks = []

    for ts, lat, status in rows:
        if lat is None:
            if current_seg:
                path_segments.append(current_seg)
                current_seg = []
            timeout_marks.append(x_of(ts))
            continue
        x, y = x_of(ts), y_of(lat)
        current_seg.append((x, y))
        if lat > y_max:
            spike_marks.append((x, pad_t))
    if current_seg:
        path_segments.append(current_seg)

    path_els = []
    for seg in path_segments:
        if len(seg) == 1:
            x, y = seg[0]
            path_els.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.2" fill="#4fd1c5"/>')
            continue
        line = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in seg)
        area = (
            f"M {seg[0][0]:.1f},{base_y:.1f} L "
            + " L ".join(f"{x:.1f},{y:.1f}" for x, y in seg)
            + f" L {seg[-1][0]:.1f},{base_y:.1f} Z"
        )
        path_els.append(f'<path d="{area}" fill="url(#{grad_id})"/>')
        path_els.append(
            f'<path d="{line}" fill="none" stroke="#4fd1c5" stroke-width="1.5" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )

    timeout_els = [
        f'<line x1="{x:.1f}" y1="{base_y - 7}" x2="{x:.1f}" y2="{base_y}" '
        f'stroke="#ff6b6b" stroke-width="1.5" stroke-linecap="round" opacity="0.9"/>'
        for x in timeout_marks
    ]
    spike_els = [
        f'<polygon points="{x - 3.5:.1f},{y + 7:.1f} {x + 3.5:.1f},{y + 7:.1f} {x:.1f},{y:.1f}" '
        f'fill="#f5a524"/>'
        for x, y in spike_marks
    ]

    svg = f"""
<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img">
  <defs>
    <linearGradient id="{grad_id}" gradientUnits="userSpaceOnUse"
                    x1="0" y1="{pad_t}" x2="0" y2="{base_y}">
      <stop offset="0%" stop-color="#4fd1c5" stop-opacity="0.13"/>
      <stop offset="100%" stop-color="#4fd1c5" stop-opacity="0"/>
    </linearGradient>
  </defs>
  {''.join(grid)}
  {''.join(x_labels)}
  {''.join(path_els)}
  {''.join(timeout_els)}
  {''.join(spike_els)}
  <line x1="{pad_l}" y1="{base_y}" x2="{width - pad_r}" y2="{base_y}" stroke="rgba(255,255,255,0.10)"/>
</svg>
"""
    return svg


def build_stats(rows, window_label="24h"):
    latencies = [l for _, l, s in rows if l is not None]
    timeouts = sum(1 for _, l, s in rows if l is None)
    total = len(rows)
    if not total:
        return f"<p>No data logged in the last {window_label} yet.</p>"

    loss_pct = timeouts / total * 100
    avg = statistics.mean(latencies) if latencies else 0
    jitter = statistics.pstdev(latencies) if len(latencies) > 1 else 0
    p95 = sorted(latencies)[int(len(latencies) * 0.95) - 1] if latencies else 0
    worst = max(latencies) if latencies else 0

    def stat(label, value, unit):
        unit_html = f'<span class="unit">{unit}</span>' if unit else ""
        return (
            f'<div class="stat"><div class="val">{value}{unit_html}</div>'
            f'<div class="lbl">{label}</div></div>'
        )

    return f"""
    <div class="stats">
      {stat("Avg latency", f"{avg:.0f}", "ms")}
      {stat("Jitter", f"{jitter:.0f}", "ms")}
      {stat("p95 latency", f"{p95:.0f}", "ms")}
      {stat("Worst ping", f"{worst:.0f}", "ms")}
      {stat("Packet loss", f"{loss_pct:.1f}", "%")}
      {stat("Samples", f"{total}", "")}
    </div>
    """


def build_last_ping(rows):
    if not rows:
        return (
            '<div class="last-ping unknown"><span class="dot-big"></span>'
            '<span class="lp-label">last ping</span>'
            '<span class="lp-val">no data yet</span></div>'
        )

    ts, lat, status = rows[-1]
    age_secs = (datetime.now().astimezone() - ts).total_seconds()
    age_str = f"{age_secs:.0f}s ago" if age_secs < 90 else f"{age_secs/60:.1f}m ago"
    time_str = ts.strftime("%H:%M:%S")

    if lat is not None:
        css_class = "ok"
        detail = f"{lat:.0f} ms"
    else:
        css_class = "down"
        detail = "timeout" if status == "timeout" else (status or "no reply")

    return f"""
    <div class="last-ping {css_class}">
      <span class="dot-big"></span>
      <span class="lp-label">last ping</span>
      <span class="lp-val">{detail}</span>
      <span class="lp-time" data-ts="{ts.isoformat()}" data-clock="{time_str}">at {time_str} ({age_str})</span>
    </div>
    """


LEGEND = f"""
      <div class="legend">
        <span><i class="swatch" style="background:#4fd1c5"></i>latency</span>
        <span><i class="swatch" style="background:#ff6b6b"></i>timeout / dropped ping</span>
        <span><i class="swatch" style="background:#f5a524"></i>spike off top of chart</span>
      </div>
"""

STYLE = """
  :root {
    --bg: #0a0c0d;
    --panel: #101415;
    --line: rgba(255,255,255,0.07);
    --text: #e8eef0;
    --muted: #98a3a8;
    --faint: #5d686d;
    --accent: #4fd1c5;
    --danger: #ff6b6b;
  }
  * { box-sizing: border-box; }
  html { background: var(--bg); }
  body {
    margin: 0;
    padding: 38px 26px 64px;
    background:
      radial-gradient(1100px 520px at 50% -240px, rgba(79,209,197,0.07), transparent 70%),
      var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", Roboto, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .page { max-width: 1120px; margin: 0 auto; }

  .top { display:flex; align-items:flex-start; justify-content:space-between; gap:18px;
         flex-wrap:wrap; margin-bottom:32px; }
  h1 { font-size:19px; font-weight:600; letter-spacing:-0.012em; margin:0 0 7px; }
  .sub { font-size:12px; color:var(--faint); }
  .sep { opacity:0.45; margin:0 2px; }
  .countdown { color:var(--muted); font-variant-numeric:tabular-nums; }

  .last-ping { display:inline-flex; align-items:center; gap:9px; padding:9px 15px 9px 13px;
               border-radius:999px; background:rgba(255,255,255,0.028);
               border:1px solid var(--line); font-size:13px; white-space:nowrap; }
  .last-ping .dot-big { width:8px; height:8px; border-radius:50%; flex:none; }
  .last-ping.ok .dot-big { background:var(--accent); box-shadow:0 0 10px rgba(79,209,197,0.65); }
  .last-ping.down .dot-big { background:var(--danger); box-shadow:0 0 10px rgba(255,107,107,0.6); }
  .last-ping.unknown .dot-big { background:var(--faint); }
  .last-ping .lp-label { color:var(--faint); font-size:10.5px; letter-spacing:0.08em;
                         text-transform:uppercase; }
  .last-ping .lp-val { font-weight:600; font-variant-numeric:tabular-nums; }
  .last-ping.down .lp-val { color:var(--danger); }
  .last-ping .lp-time { color:var(--faint); font-size:11.5px; font-variant-numeric:tabular-nums; }

  .section { margin-bottom:34px; }
  .section-head { display:flex; align-items:center; gap:14px; margin:0 0 12px; }
  .section-head h2 { margin:0; font-size:11px; font-weight:600; letter-spacing:0.13em;
                     text-transform:uppercase; color:var(--muted); white-space:nowrap; }
  .section-head .rule { flex:1; height:1px; background:linear-gradient(90deg, var(--line), transparent); }

  .panel { padding:16px 14px 10px; border:1px solid var(--line); border-radius:16px;
           background:linear-gradient(180deg, rgba(255,255,255,0.025), rgba(255,255,255,0.006)); }
  .chart { min-width:0; }
  .panel svg { display:block; width:100%; height:auto; }

  .legend { display:flex; flex-wrap:wrap; gap:18px; padding:10px 6px 2px;
            font-size:11.5px; color:var(--faint); }
  .legend span { display:inline-flex; align-items:center; gap:7px; }
  .swatch { width:7px; height:7px; border-radius:50%; display:inline-block; }

  .stats { display:grid; grid-template-columns:repeat(6, minmax(0, 1fr)); gap:1px;
           margin-top:12px; border:1px solid var(--line); border-radius:14px;
           overflow:hidden; background:var(--line); }
  .stat { background:var(--panel); padding:14px 16px 13px; }
  .stat .val { font-size:19px; font-weight:600; letter-spacing:-0.015em;
               font-variant-numeric:tabular-nums; }
  .stat .unit { margin-left:3px; font-size:11px; font-weight:500; color:var(--faint); letter-spacing:0; }
  .stat .lbl { margin-top:5px; font-size:10px; letter-spacing:0.08em; text-transform:uppercase;
               color:var(--faint); }
  .stats-empty { color:var(--faint); font-size:12.5px; padding:4px 2px; }

  .live-err { color:var(--danger); font-size:12.5px; margin-bottom:14px; }

  @media (max-width: 700px) {
    /* The chart is a fixed viewBox; let it scroll rather than shrink into
       unreadable 4px axis labels. */
    .chart { overflow-x: auto; }
    .chart svg { min-width: 760px; }
  }

  @media (max-width: 860px) {
    body { padding:26px 16px 48px; }
    .stats { grid-template-columns:repeat(3, minmax(0, 1fr)); }
  }
  @media (max-width: 520px) {
    .stats { grid-template-columns:repeat(2, minmax(0, 1fr)); }
    .last-ping { width:100%; }
  }
"""

# Live-refresh bootstrap. Kept out of an f-string on purpose so the JS braces
# don't need doubling; REFRESH_SECONDS is substituted in.
SHELL_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="Cache-Control" content="no-store">
<link rel="icon" type="image/svg+xml" href="favicon.svg">
<title>LTE Connection Monitor</title>
<style>__STYLE__</style>
</head>
<body>
  <div class="page" id="app">__BODY__</div>
  <script>
  (function () {
    var REFRESH_MS = __REFRESH_MS__;
    // Served over HTTP: PING_URL hits an endpoint that runs a ping before
    // replying, so a page reload is a real measurement. Static file:// copy:
    // both point at the same pre-rendered data file, so reloading only
    // re-reads what the background job last wrote.
    var PING_URL = __PING_URL__;
    var DATA_URL = __DATA_URL__;
    var nextRefreshAt = null;

    function fmtAge(secs) {
      if (secs < 90) return Math.max(0, Math.round(secs)) + 's';
      return (secs / 60).toFixed(1) + 'm';
    }

    // Keep the "Ns ago" next to the last ping honest between data refreshes.
    function tickAges() {
      var now = Date.now();
      var els = document.querySelectorAll('[data-ts]');
      for (var i = 0; i < els.length; i++) {
        var age = (now - Date.parse(els[i].getAttribute('data-ts'))) / 1000;
        if (isFinite(age)) {
          els[i].textContent = 'at ' + els[i].getAttribute('data-clock') +
                               ' (' + fmtAge(age) + ' ago)';
        }
      }
    }

    // "refreshing in Ns": re-armed after every fetch, so the countdown tracks
    // the real cadence instead of a fixed clock that drifts from load time.
    function tickCountdown() {
      if (nextRefreshAt === null) return;
      var el = document.querySelector('[data-countdown]');
      if (!el) return;
      var secs = Math.max(0, Math.ceil((nextRefreshAt - Date.now()) / 1000));
      var text = secs + 's';
      if (el.textContent !== text) el.textContent = text;
    }

    function scheduleNext() {
      nextRefreshAt = Date.now() + REFRESH_MS;
      tickCountdown();
      setTimeout(function () { loadData(DATA_URL, scheduleNext); }, REFRESH_MS);
    }

    // Pull the freshest render. A <script> tag (rather than fetch/XHR) is used
    // so this keeps working when the page is opened as a file:// URL.
    function loadData(url, done) {
      var s = document.createElement('script');
      s.src = url + '?t=' + Date.now();
      s.onload = function () { s.remove(); tickAges(); if (done) done(); };
      s.onerror = function () {
        s.remove();
        var app = document.getElementById('app');
        if (app && !app.querySelector('.live-err')) {
          app.insertAdjacentHTML('afterbegin',
            '<div class="live-err">Live data could not be loaded - ' +
            'is the monitor still running?</div>');
        }
        if (done) done();
      };
      document.body.appendChild(s);
    }

    loadData(PING_URL, scheduleNext);
    setInterval(tickAges, 1000);
    setInterval(tickCountdown, 250);
  })();
  </script>
</body>
</html>
"""


def render_body(rows_short, rows_long, reload_hint=False):
    svg_short = build_svg(rows_short, x_label_step=timedelta(minutes=10),
                          x_label_fmt="%H:%M", grad_id="area-short")
    svg_long = build_svg(rows_long, x_label_step=timedelta(hours=2),
                         x_label_fmt="%H:%M", grad_id="area-long")

    stats_short = build_stats(rows_short, window_label="1h")
    stats_long = build_stats(rows_long, window_label="24h")
    last_ping_html = build_last_ping(rows_long)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hint = f' <span class="sep">·</span> reload to ping now' if reload_hint else ""

    return f"""
  <header class="top">
    <div>
      <h1>LTE Connection Monitor</h1>
      <div class="sub">Updated {now_str} <span class="sep">·</span> refreshing in <span class="countdown" data-countdown>{REFRESH_SECONDS}s</span>{hint}</div>
    </div>
    {last_ping_html}
  </header>

  <section class="section">
    <div class="section-head"><h2>Last hour</h2><span class="rule"></span></div>
    <div class="panel">
      <div class="chart">
        {svg_short}
      </div>
      {LEGEND}
    </div>
    {stats_short}
  </section>

  <section class="section">
    <div class="section-head"><h2>Last 24 hours</h2><span class="rule"></span></div>
    <div class="panel">
      <div class="chart">
        {svg_long}
      </div>
      {LEGEND}
    </div>
    {stats_long}
  </section>
"""


def render_shell(initial_body, ping_url="report_data.js", data_url="report_data.js"):
    return (
        SHELL_TEMPLATE.replace("__STYLE__", STYLE)
        .replace("__REFRESH_MS__", str(REFRESH_SECONDS * 1000))
        .replace("__PING_URL__", json.dumps(ping_url))
        .replace("__DATA_URL__", json.dumps(data_url))
        .replace("__BODY__", initial_body)
    )


def render_data_js(body_html):
    # json.dumps produces a valid JS string literal, so any quotes/backslashes
    # in the generated markup are escaped safely.
    return "document.getElementById('app').innerHTML = " + json.dumps(body_html) + ";\n"


def main():
    rows_short = load_rows(WINDOW_HOURS_SHORT)
    rows_long = load_rows(WINDOW_HOURS_LONG)

    body = render_body(rows_short, rows_long)

    os.makedirs(os.path.dirname(OUTFILE), exist_ok=True)
    atomic_write(OUTFILE, render_shell(body))
    atomic_write(DATA_FILE, render_data_js(body))
    print(
        f"Wrote {OUTFILE} + {DATA_FILE} "
        f"(1h: {len(rows_short)} samples, 24h: {len(rows_long)} samples)"
    )


if __name__ == "__main__":
    main()
