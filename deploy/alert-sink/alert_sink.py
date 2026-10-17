"""Stand-in for Slack/PagerDuty: receives Alertmanager webhooks, logs one JSON
line per alert, and serves the last 500 at GET / for the drill report."""

import json
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RECEIVED: deque = deque(maxlen=500)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        for alert in body.get("alerts", []):
            entry = {
                "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "channel": self.path.strip("/"),
                "status": alert.get("status"),
                "alertname": alert.get("labels", {}).get("alertname"),
                "severity": alert.get("labels", {}).get("severity"),
                "track": alert.get("labels", {}).get("track"),
                "release": alert.get("labels", {}).get("release"),
                "summary": alert.get("annotations", {}).get("summary"),
                "starts_at": alert.get("startsAt"),
            }
            RECEIVED.append(entry)
            print(json.dumps(entry), flush=True)
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        payload = json.dumps(list(RECEIVED), indent=2).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9095), Handler).serve_forever()
