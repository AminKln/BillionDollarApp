"""
Minimal "bad app" for demoing Prometheus anomaly detection.

Exposes /metrics in Prometheus format, generates its own steady baseline
traffic in the background, and lets you flip latency/error chaos on and
off live via HTTP — no separate load generator needed.

Run:
    pip install flask prometheus_client
    python bad_app.py

Then during the demo:
    curl http://localhost:8000/chaos/latency/on
    curl http://localhost:8000/chaos/errors/on
    curl http://localhost:8000/chaos/latency/off
    curl http://localhost:8000/chaos/errors/off
"""

import random
import threading
import time
import urllib.request

from flask import Flask, Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

app = Flask(__name__)

REQUEST_COUNT = Counter(
    "http_requests_total", "Total HTTP requests", ["method", "path", "status"]
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds", "Request duration in seconds", ["path"]
)

# Flip these live during the demo — see the /chaos/<kind>/<state> route below.
chaos = {"latency": False, "errors": False}


@app.route("/")
def index():
    start = time.time()
    status = 200

    if chaos["latency"]:
        time.sleep(random.uniform(1.5, 3.0))  # simulate a slow downstream call
    else:
        time.sleep(random.uniform(0.01, 0.05))  # normal, healthy latency

    if chaos["errors"] and random.random() < 0.4:
        status = 500

    REQUEST_LATENCY.labels(path="/").observe(time.time() - start)
    REQUEST_COUNT.labels(method="GET", path="/", status=str(status)).inc()
    return ("boom", status) if status == 500 else ("ok", status)


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


@app.route("/chaos/<kind>/<state>")
def toggle_chaos(kind, state):
    if kind not in chaos:
        return f"unknown chaos kind: {kind} (use 'latency' or 'errors')", 404
    chaos[kind] = state == "on"
    return f"{kind} chaos is now {'ON' if chaos[kind] else 'off'}"


@app.route("/chaos/status")
def chaos_status():
    return chaos


def generate_traffic():
    """Keeps a steady stream of requests hitting the app so Prometheus
    always has fresh data, without needing a separate load-testing tool."""
    while True:
        try:
            urllib.request.urlopen("http://localhost:8000/", timeout=5)
        except Exception:
            pass
        time.sleep(0.2)


if __name__ == "__main__":
    threading.Thread(target=generate_traffic, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)