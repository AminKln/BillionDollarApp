"""
Minimal "bad app" for demoing CloudWatch anomaly detection.

Generates its own steady baseline traffic in the background, tracks request
latency/errors in memory, and flushes them to CloudWatch every 10s as custom
metrics (namespace "HackathonDemo") — batched rather than one API call per
request, to stay well inside the free tier and avoid PutMetricData
throttling.

Chaos toggles let you induce the anomaly live during the demo:
    curl http://localhost:8000/chaos/latency/on
    curl http://localhost:8000/chaos/errors/on
    curl http://localhost:8000/chaos/latency/off
    curl http://localhost:8000/chaos/errors/off

Requires cloudwatch:PutMetricData — grant it via the EC2 instance's IAM role,
no hardcoded keys needed (boto3 picks up the instance profile automatically).

Run:
    pip install flask boto3
    python bad_app.py
"""

import random
import threading
import time
import urllib.request

import boto3
from flask import Flask, jsonify

app = Flask(__name__)
cloudwatch = boto3.client("cloudwatch")

NAMESPACE = "HackathonDemo"
FLUSH_INTERVAL_SECONDS = 10

# Flip these live during the demo — see the /chaos/<kind>/<state> route below.
chaos = {"latency": False, "errors": False}

_lock = threading.Lock()
_latency_samples = []
_request_count = 0
_error_count = 0


@app.route("/")
def index():
    global _request_count, _error_count
    start = time.time()
    status = 200

    if chaos["latency"]:
        time.sleep(random.uniform(1.5, 3.0))  # simulate a slow downstream call
    else:
        time.sleep(random.uniform(0.01, 0.05))  # normal, healthy latency

    if chaos["errors"] and random.random() < 0.4:
        status = 500

    elapsed = time.time() - start
    with _lock:
        _latency_samples.append(elapsed)
        _request_count += 1
        if status == 500:
            _error_count += 1

    return ("boom", status) if status == 500 else ("ok", status)


@app.route("/chaos/<kind>/<state>")
def toggle_chaos(kind, state):
    if kind not in chaos:
        return f"unknown chaos kind: {kind} (use 'latency' or 'errors')", 404
    chaos[kind] = state == "on"
    return f"{kind} chaos is now {'ON' if chaos[kind] else 'off'}"


@app.route("/chaos/status")
def chaos_status():
    return jsonify(chaos)


def generate_traffic():
    """Keeps a steady stream of requests hitting the app so CloudWatch always
    has fresh data, without needing a separate load-testing tool."""
    while True:
        try:
            urllib.request.urlopen("http://localhost:8000/", timeout=5)
        except Exception:
            pass
        time.sleep(0.2)


def flush_metrics_to_cloudwatch():
    """Every FLUSH_INTERVAL_SECONDS, push one batched RequestLatency data
    point (average of samples since the last flush) and one ErrorRate data
    point, instead of calling PutMetricData per request."""
    global _latency_samples, _request_count, _error_count
    while True:
        time.sleep(FLUSH_INTERVAL_SECONDS)
        with _lock:
            samples, count, errors = _latency_samples, _request_count, _error_count
            _latency_samples, _request_count, _error_count = [], 0, 0

        if not samples:
            continue

        avg_latency = sum(samples) / len(samples)
        error_rate = (errors / count) if count else 0.0

        try:
            cloudwatch.put_metric_data(
                Namespace=NAMESPACE,
                MetricData=[
                    {"MetricName": "RequestLatency", "Value": avg_latency, "Unit": "Seconds"},
                    {"MetricName": "ErrorRate", "Value": error_rate, "Unit": "Percent"},
                ],
            )
        except Exception as exc:
            print(f"CloudWatch PutMetricData failed: {exc}")


if __name__ == "__main__":
    threading.Thread(target=generate_traffic, daemon=True).start()
    threading.Thread(target=flush_metrics_to_cloudwatch, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)
