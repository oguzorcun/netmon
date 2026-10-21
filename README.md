# LTE Connection Monitor — Setup

Four pieces:
- `ping_monitor.sh` — pings 1.1.1.1 every 5s, logs to `~/Library/Logs/netmon/latency.csv`.
  It also supports `--once` (one measurement, same logging) for the report server.
- `generate_report.py` — reads the last 24h and writes `~/netmon/report.html` plus
  `~/netmon/report_data.js` (a dark-mode graph, no internet/CDN required to view it — important
  since the whole point is your connection may be flaky). The page re-pulls the data every 10s
  and redraws itself.
- `report_server.py` — a loopback-only server (`http://127.0.0.1:8765/`) that renders the report
  on demand and runs an immediate ping whenever you load or reload the page, so a reload is a
  real measurement instead of a re-read of a cached snapshot.
- `favicon.svg` — the tab icon (a teal latency trace, matching the graph).
- Three `launchd` plists so all of it runs automatically in the background, survives
  reboots/sleep, and restarts if killed

## 1. Move the files into place

```bash
mkdir -p ~/netmon
cp ping_monitor.sh generate_report.py report_server.py favicon.svg ~/netmon/
chmod +x ~/netmon/ping_monitor.sh
```

## 2. Install the launchd agents

The plists have a placeholder `REPLACE_WITH_HOME` — swap it for your actual home path (run `echo $HOME`
to check, it's usually `/Users/yourname`):

```bash
sed -i '' "s|REPLACE_WITH_HOME|$HOME|g" com.netmon.pinglogger.plist
sed -i '' "s|REPLACE_WITH_HOME|$HOME|g" com.netmon.reportgen.plist
sed -i '' "s|REPLACE_WITH_HOME|$HOME|g" com.netmon.reportserver.plist

cp com.netmon.pinglogger.plist com.netmon.reportgen.plist com.netmon.reportserver.plist \
   ~/Library/LaunchAgents/

launchctl load ~/Library/LaunchAgents/com.netmon.pinglogger.plist
launchctl load ~/Library/LaunchAgents/com.netmon.reportgen.plist
launchctl load ~/Library/LaunchAgents/com.netmon.reportserver.plist
```

That's it — the logger starts pinging immediately, the report file regenerates every 5 seconds,
and the local report server comes up on port 8765.

## 3. View it

```bash
open http://127.0.0.1:8765/
```

Open it through the local server, not as a file. The page updates itself every 10 seconds, and
**loading or reloading it runs a fresh ping first**, then plots that ping — so what you see after a
reload is an actual measurement taken at that moment.

`open ~/netmon/report.html` still works (useful if the server is stopped), but a `file://` page
cannot run a ping, and browsers — Firefox especially — may serve a cached copy on reload. So that
copy only ever shows what the background job last wrote.

Give it a few minutes to collect data before the graph looks meaningful, and a few hours before
it's genuinely useful for spotting your LTE reception patterns (e.g. does latency spike at certain
times of day, or correlate with when you know signal is weak in a particular room).

## Reading the graph
- **Teal line** — latency in ms. Gaps in the line = the ping actually failed (grouped with the
  red ticks below).
- **Red ticks along the bottom** — timeouts / dropped pings. On a poor-reception LTE modem, a
  cluster of these is often more telling than the latency number itself.
- **Orange triangles** — a spike that went off the top of the chart. The y-axis is capped at
  **300 ms** (below that it auto-scales to keep normal variation readable), so anything above
  300 ms is flagged with a triangle instead of squashing everything else.
- **Stat cards** — avg, jitter (standard deviation — a big number here means inconsistent ping
  times even if the average looks fine, which is typical of marginal cellular signal), p95, worst
  ping, and packet loss %.

## Stopping it later
```bash
launchctl unload ~/Library/LaunchAgents/com.netmon.pinglogger.plist
launchctl unload ~/Library/LaunchAgents/com.netmon.reportgen.plist
launchctl unload ~/Library/LaunchAgents/com.netmon.reportserver.plist
```

## Tuning
- Change `TARGET` in `ping_monitor.sh` if you'd rather ping your router or ISP gateway instead of
  1.1.1.1 (useful for isolating "is it the LTE modem or something further upstream").
- Change `INTERVAL` (default 5s) for finer/coarser granularity.
- Change `StartInterval` in `com.netmon.reportgen.plist` (default 5s) for how often the static
  `report.html` fallback is rewritten. The server renders on demand, so this only matters for the
  `file://` copy. The page polls every 10s — that's `REFRESH_SECONDS` in `generate_report.py`.
- Change `PORT` in `report_server.py` (default 8765) if it clashes with something else; update the
  URL you bookmark accordingly.
