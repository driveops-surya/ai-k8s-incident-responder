import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import kubernetes_client, queue
from app.main import app
from app.pagerduty import verify_signature
from app.scope import incident_scope

ROOT = Path(__file__).resolve().parents[1]
EVENT = json.loads((ROOT / "examples" / "pagerduty-webhook.json").read_text())["event"]
ALERTS = json.loads((ROOT / "examples" / "pagerduty-alerts.json").read_text())


@pytest.fixture
def configured(monkeypatch, tmp_path):
    variables = {
        "PAGERDUTY_API_TOKEN": "test-token",
        "PAGERDUTY_FROM_EMAIL": "sre@example.com",
        "PAGERDUTY_WEBHOOK_SECRET": "test-secret",
        "PROMETHEUS_URL": "http://prometheus",
        "LOKI_URL": "http://loki",
        "VLLM_BASE_URL": "http://vllm/v1",
        "VLLM_MODEL": "test-model",
        "DATABASE_PATH": str(tmp_path / "jobs.db"),
    }
    for key, value in variables.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(queue.Worker, "start", lambda self: None)
    monkeypatch.setattr(queue.Worker, "stop", lambda self: None)
    return variables


def signed(event=EVENT):
    body = json.dumps({"event": event}).encode()
    digest = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    return body, {"X-PagerDuty-Signature": f"v1={digest}"}


def test_webhook_to_pagerduty_note(configured, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/alerts"):
            return httpx.Response(200, json=ALERTS)
        if request.url.path == "/api/v1/query":
            return httpx.Response(200, json={"status": "success", "data": {"result": [{"value": [1, "2"]}]}})
        if request.url.path == "/loki/api/v1/query_range":
            return httpx.Response(200, json={"status": "success", "data": {"result": [{"values": [["123", "crash"]]}]}})
        if request.url.path == "/v1/chat/completions":
            evidence = json.loads(request.content)["messages"][1]["content"]
            assert "crash" in evidence and "cpu_cores" in evidence and "recent_deployments" in evidence
            return httpx.Response(200, json={"choices": [{"message": {"content": "Incident summary\nProbable root cause: crash"}}]})
        if request.url.path.endswith("/notes"):
            return httpx.Response(201, json={"note": {}})
        raise AssertionError(str(request.url))

    transport = httpx.MockTransport(handler)
    original_client = httpx.Client
    with TestClient(app) as client:
        body, headers = signed()
        assert client.post("/webhooks/pagerduty", content=body, headers=headers).json() == {"status": "queued"}
        assert client.post("/webhooks/pagerduty", content=body, headers=headers).json() == {"status": "duplicate"}
        monkeypatch.setattr(httpx, "Client", lambda **kwargs: original_client(transport=transport, **kwargs))
        monkeypatch.setattr(httpx, "get", lambda url, **kwargs: original_client(transport=transport).get(url, **kwargs))
        monkeypatch.setattr(httpx, "post", lambda url, **kwargs: original_client(transport=transport).post(url, **kwargs))
        monkeypatch.setattr(kubernetes_client, "collect", lambda namespace, pod: {"recent_deployments": [], "pod": pod})
        assert app.state.queue.run_one(app.state.settings)
        assert not app.state.queue.run_one(app.state.settings)
        with app.state.queue.connect() as db:
            assert db.execute("SELECT status FROM jobs").fetchone()[0] == "completed"
    note = json.loads(next(r for r in calls if r.url.path.endswith("/notes")).content)["note"]["content"]
    assert "human review required" in note
    assert "Probable root cause" in note
    assert any(r.url.path == "/api/v1/query" for r in calls)
    assert any(r.url.path == "/loki/api/v1/query_range" for r in calls)


def test_authentication_validation_and_ignored_events(configured):
    with TestClient(app) as client:
        body, headers = signed()
        assert client.post("/webhooks/pagerduty", content=body).status_code == 401
        assert client.post("/webhooks/pagerduty", content=body + b" ", headers=headers).status_code == 401
        ignored = {**EVENT, "event_type": "incident.resolved"}
        body, headers = signed(ignored)
        assert client.post("/webhooks/pagerduty", content=body, headers=headers).json() == {"status": "ignored"}
        bad = {**EVENT, "data": {"id": "../other"}}
        body, headers = signed(bad)
        assert client.post("/webhooks/pagerduty", content=body, headers=headers).status_code == 400
        assert client.post("/webhooks/pagerduty", content=b"x" * (1024 * 1024 + 1)).status_code == 413


def test_signature_rotation_and_non_ascii_header():
    body, headers = signed()
    assert verify_signature(body, f"v1={'0' * 64}, {headers['X-PagerDuty-Signature']}", "test-secret")
    assert not verify_signature(body, "v1=\u00e9", "test-secret")


def test_scope_rejects_ambiguous_and_unsafe_labels():
    alerts = ALERTS["alerts"]
    assert incident_scope(alerts) == ("production", "checkout-7f88886d8d-abcde")
    assert incident_scope(alerts + [{"body": {"details": {"namespace": "other", "pod": "a"}}}]) == (None, None)
    assert incident_scope(alerts + [{"body": {"details": {}}}]) == (None, None)
    assert incident_scope([{"body": {"details": {"namespace": 'ns"} |= "x"', "pod": "a"}}}]) == (None, None)


def test_retry_failure_and_recovery(configured, monkeypatch):
    with TestClient(app) as client:
        body, headers = signed()
        client.post("/webhooks/pagerduty", content=body, headers=headers)
        monkeypatch.setattr(queue, "process", lambda settings, event: (_ for _ in ()).throw(RuntimeError("failure")))
        app.state.queue.max_attempts = 2
        assert app.state.queue.run_one(app.state.settings)
        with app.state.queue.connect() as db:
            assert db.execute("SELECT status, attempts FROM jobs").fetchone() == ("pending", 1)
            db.execute("UPDATE jobs SET next_attempt = 0")
        assert app.state.queue.run_one(app.state.settings)
        with app.state.queue.connect() as db:
            assert db.execute("SELECT status, attempts FROM jobs").fetchone() == ("failed", 2)
            db.execute("UPDATE jobs SET status = 'running'")
        recovered = queue.JobQueue(configured["DATABASE_PATH"], 2)
        with recovered.connect() as db:
            assert db.execute("SELECT status FROM jobs").fetchone()[0] == "pending"


def test_evidence_errors_are_included_in_note(configured, monkeypatch):
    from app import pipeline

    captured = {}
    monkeypatch.setattr(pipeline.PagerDuty, "alerts", lambda self, incident_id: ALERTS["alerts"])
    monkeypatch.setattr(pipeline.PagerDuty, "add_note", lambda self, incident_id, text: captured.update(note=text))
    monkeypatch.setattr(kubernetes_client, "collect", lambda *args: (_ for _ in ()).throw(RuntimeError("unavailable")))
    monkeypatch.setattr(pipeline.prometheus, "collect", lambda *args: {})
    monkeypatch.setattr(pipeline.loki, "collect", lambda *args: [])
    monkeypatch.setattr(pipeline.vllm, "analyze", lambda settings, evidence: captured.update(evidence=evidence) or "Analysis")
    with TestClient(app):
        pipeline.process(app.state.settings, EVENT)
    assert "kubernetes collection failed" in captured["note"]
    assert "kubernetes collection failed" in captured["evidence"]["collection_warnings"][0]


def test_missing_scope_skips_cluster_queries(configured, monkeypatch):
    from app import pipeline

    captured = {}
    monkeypatch.setattr(pipeline.PagerDuty, "alerts", lambda self, incident_id: [{"body": {"details": {}}}])
    monkeypatch.setattr(pipeline.PagerDuty, "add_note", lambda self, incident_id, text: captured.update(note=text))
    monkeypatch.setattr(kubernetes_client, "collect", lambda *args: pytest.fail("unscoped Kubernetes query"))
    monkeypatch.setattr(pipeline.vllm, "analyze", lambda settings, evidence: captured.update(evidence=evidence) or "Analysis")
    with TestClient(app):
        pipeline.process(app.state.settings, EVENT)
    assert "No unambiguous" in captured["note"]
    assert captured["evidence"]["scope"] == {"namespace": None, "pod": None}


def test_kubernetes_pod_failure_does_not_discard_other_evidence(monkeypatch):
    def missing_pod(*args, **kwargs):
        raise RuntimeError("pod missing")

    core = SimpleNamespace(
        read_namespaced_pod=missing_pod,
        list_namespaced_event=lambda *args, **kwargs: SimpleNamespace(items=[]),
    )
    apps = SimpleNamespace(list_namespaced_deployment=lambda *args, **kwargs: SimpleNamespace(items=[]))
    monkeypatch.setattr(kubernetes_client.config, "load_incluster_config", lambda: None)
    monkeypatch.setattr(kubernetes_client.client, "CoreV1Api", lambda: core)
    monkeypatch.setattr(kubernetes_client.client, "AppsV1Api", lambda: apps)
    result = kubernetes_client.collect("production", "gone-pod")
    assert result["events"] == []
    assert result["recent_deployments"] == []
    assert "Kubernetes pod collection failed" in result["warnings"][0]


def test_large_evidence_is_bounded_and_reported(configured, monkeypatch):
    from app import pipeline

    captured = {}
    monkeypatch.setattr(pipeline.PagerDuty, "alerts", lambda self, incident_id: ALERTS["alerts"])
    monkeypatch.setattr(pipeline.PagerDuty, "add_note", lambda self, incident_id, text: captured.update(note=text))
    monkeypatch.setattr(kubernetes_client, "collect", lambda *args: {"events": ["x" * 10000]})
    monkeypatch.setattr(pipeline.prometheus, "collect", lambda *args: {"cpu": 1})
    monkeypatch.setattr(pipeline.loki, "collect", lambda *args: ["important log"])
    monkeypatch.setattr(pipeline.vllm, "analyze", lambda settings, evidence: captured.update(evidence=evidence) or "Analysis")
    with TestClient(app):
        pipeline.process(app.state.settings, EVENT)
    assert captured["evidence"]["kubernetes"]["truncated"]
    assert captured["evidence"]["loki"] == ["important log"]
    assert "kubernetes evidence truncated" in captured["note"]
