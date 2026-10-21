#!/usr/bin/env python3
"""
report_server.py
Serves the latency report on http://127.0.0.1:8765/ (loopback only).

Why a server instead of just opening report.html: a file:// page cannot cause
a ping, and browsers cache local files, so a manual reload can show an old
snapshot. Here the page asks the server for data, and the first request of a
page load runs a ping before answering - so reloading the report measures a
fresh round trip and plots it.

Endpoints:
    /             page shell
    /ping.js      run one ping now, log it, return the rendered report
    /data.js      return the rendered report without pinging (used by the 10s
                  auto-refresh, so idling on the page doesn't skew the log)
    /favicon.svg  the tab icon

Nothing is exposed beyond 127.0.0.1 and no user input reaches the ping script.
"""

import http.server
import os
import subprocess
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import generate_report as report  # noqa: E402

HOST = "127.0.0.1"
PORT = 8765
PING_SCRIPT = os.path.join(HERE, "ping_monitor.sh")
FAVICON = os.path.join(HERE, "favicon.svg")


def run_ping():
    """One immediate measurement, logged exactly like the background logger."""
    try:
        subprocess.run(
            ["/bin/bash", PING_SCRIPT, "--once"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"ping failed: {exc}", file=sys.stderr)


def render_body(reload_hint=False):
    return report.render_body(
        report.load_rows(report.WINDOW_HOURS_SHORT),
        report.load_rows(report.WINDOW_HOURS_LONG),
        reload_hint=reload_hint,
    )


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "netmon"

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html", "/report.html"):
            body = report.render_shell(
                render_body(reload_hint=True),
                ping_url="/ping.js",
                data_url="/data.js",
            )
            self._send(body, "text/html")
        elif path == "/ping.js":
            run_ping()
            self._send(report.render_data_js(render_body()), "application/javascript")
        elif path == "/data.js":
            self._send(report.render_data_js(render_body()), "application/javascript")
        elif path == "/favicon.svg":
            self._send_file(FAVICON, "image/svg+xml")
        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404)

    def _send(self, text, content_type):
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        # Never let the browser reuse an old copy; a reload must re-measure.
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self._write(data)

    def _send_file(self, filename, content_type):
        try:
            with open(filename, "rb") as f:
                data = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self._write(data)

    def _write(self, data):
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # The browser reloaded or navigated away mid-response. Expected
            # with a page that reloads itself; not worth a traceback in the log.
            pass

    def log_message(self, fmt, *args):
        # The page polls every 10s; logging each request would flood the
        # launchd error log with noise.
        pass


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    try:
        httpd = Server((HOST, PORT), Handler)
    except OSError as exc:
        print(f"cannot bind {HOST}:{PORT}: {exc}", file=sys.stderr)
        return 1
    print(f"netmon report server listening on http://{HOST}:{PORT}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
