#!/bin/bash
# ping_monitor.sh
# Continuously pings a target host and logs latency to a CSV file.
# Designed to run forever as a launchd background agent.

TARGET="1.1.1.1"          # Cloudflare DNS - fast, reliable, good for latency baselining
INTERVAL=5                # seconds between pings
LOGDIR="$HOME/Library/Logs/netmon"
LOGFILE="$LOGDIR/latency.csv"

mkdir -p "$LOGDIR"

if [ ! -f "$LOGFILE" ]; then
    echo "timestamp,latency_ms,status" > "$LOGFILE"
fi

# One measurement, logged exactly the same way as the background loop below.
ping_once() {
    TS=$(date -u +"%Y-%m-%dT%H:%M:%S")
    # -c 1: one packet, -t 2: give up after 2s (macOS/BSD ping's -t is an overall timeout, not TTL)
    RESULT=$(ping -c 1 -t 2 "$TARGET" 2>/dev/null | grep "time=")

    if [ -n "$RESULT" ]; then
        LATENCY=$(echo "$RESULT" | sed -E 's/.*time=([0-9.]+).*/\1/')
        # Sanity cap: nothing above ~5000ms can be a real reply given -t 2.
        # A larger value means the process was frozen mid-ping by system sleep
        # and woke up later - discard it as a sleep/wake artifact, not a spike.
        IS_SANE=$(awk -v v="$LATENCY" 'BEGIN{print (v <= 5000) ? 1 : 0}')
        if [ "$IS_SANE" -eq 1 ]; then
            echo "$TS,$LATENCY,ok" >> "$LOGFILE"
        else
            echo "$TS,,sleep_gap" >> "$LOGFILE"
        fi
    else
        echo "$TS,,timeout" >> "$LOGFILE"
    fi
}

# Trim the file so it doesn't grow forever - keep last ~48h worth of rows
# (24h at 15s interval = 5760 rows; keep a bit extra as buffer)
trim_log() {
    LINE_COUNT=$(wc -l < "$LOGFILE")
    if [ "$LINE_COUNT" -gt 40000 ]; then
        tail -n 20000 "$LOGFILE" > "$LOGFILE.tmp"
        echo "timestamp,latency_ms,status" > "$LOGFILE"
        cat "$LOGFILE.tmp" >> "$LOGFILE"
        rm "$LOGFILE.tmp"
    fi
}

# Single-shot mode, used by report_server.py so that reloading the report page
# forces an immediate measurement instead of waiting for the next tick.
if [ "${1:-}" = "--once" ]; then
    ping_once
    trim_log
    exit 0
fi

while true; do
    ping_once
    trim_log
    sleep "$INTERVAL"
done
