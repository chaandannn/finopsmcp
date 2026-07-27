"""Tests for finops.connectors.saas.n8n."""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from finops.connectors.saas.n8n import N8nConnector


def _run(coro):
    return asyncio.run(coro)


# ── is_configured ──────────────────────────────────────────────────────────────

def test_is_configured_false_when_env_not_set(monkeypatch):
    monkeypatch.delenv("N8N_WEBHOOK_URL", raising=False)
    connector = N8nConnector()
    assert _run(connector.is_configured()) is False


def test_is_configured_true_when_env_set(monkeypatch):
    monkeypatch.setenv("N8N_WEBHOOK_URL", "https://example.com/webhook/abc")
    connector = N8nConnector()
    assert _run(connector.is_configured()) is True


# ── send_event payload structure ───────────────────────────────────────────────

def test_send_event_payload_structure(monkeypatch):
    """Payload sent to n8n must include event, timestamp, source, data."""
    monkeypatch.setenv("N8N_WEBHOOK_URL", "https://example.com/webhook/abc")

    captured = {}

    async def _fake_post(url, json=None, **kwargs):
        captured["url"] = url
        captured["body"] = json
        resp = MagicMock()
        resp.status_code = 200
        return resp

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = _fake_post

    connector = N8nConnector()

    with patch("finops.connectors.saas.n8n.httpx.AsyncClient", return_value=mock_client):
        result = _run(connector.send_event("test_event", {"key": "value"}))

    assert result is True
    body = captured["body"]
    assert body["event"] == "test_event"
    assert body["source"] == "nable"
    assert "timestamp" in body
    assert body["data"] == {"key": "value"}


# ── send_anomaly required fields ───────────────────────────────────────────────

def test_send_anomaly_includes_required_fields(monkeypatch):
    monkeypatch.setenv("N8N_WEBHOOK_URL", "https://example.com/webhook/abc")

    captured = {}

    async def _fake_post(url, json=None, **kwargs):
        captured["body"] = json
        resp = MagicMock()
        resp.status_code = 200
        return resp

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = _fake_post

    anomaly = {
        "provider": "aws",
        "service": "Amazon Textract",
        "account_id": "123456789012",
        "severity": "high",
        "direction": "spike",
        "pct_change": 127.0,
        "z_score": 4.8,
        "baseline_mean": 1810.0,
        "current_amount": 4100.0,
        "detected_at": "2026-05-30",
    }

    connector = N8nConnector()
    with patch("finops.connectors.saas.n8n.httpx.AsyncClient", return_value=mock_client):
        result = _run(connector.send_anomaly(anomaly))

    assert result is True
    data = captured["body"]["data"]
    assert data["service"] == "Amazon Textract"
    assert data["spike_pct"] == 127
    assert data["delta_usd"] == pytest.approx(2290.0, abs=1)
    assert "recommended_action" in data
    assert captured["body"]["event"] == "anomaly_detected"


# ── send_audit_summary includes top_opportunities ─────────────────────────────

def test_send_audit_summary_includes_top_opportunities(monkeypatch):
    monkeypatch.setenv("N8N_WEBHOOK_URL", "https://example.com/webhook/abc")

    captured = {}

    async def _fake_post(url, json=None, **kwargs):
        captured["body"] = json
        resp = MagicMock()
        resp.status_code = 200
        return resp

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = _fake_post

    findings = [
        {"title": "Delete unattached EBS volumes", "category": "ebs", "estimated_monthly_savings": 320.0},
        {"title": "Release unused Elastic IPs", "category": "eips", "estimated_monthly_savings": 18.0},
    ]

    connector = N8nConnector()
    with patch("finops.connectors.saas.n8n.httpx.AsyncClient", return_value=mock_client):
        result = _run(connector.send_audit_summary(
            findings=findings,
            total_savings=338.0,
            account="123456789012",
            scan_duration_s=28.3,
        ))

    assert result is True
    data = captured["body"]["data"]
    assert data["total_monthly_savings"] == pytest.approx(338.0)
    assert data["total_annual_savings"] == pytest.approx(338.0 * 12, rel=0.01)
    assert len(data["top_opportunities"]) == 2
    assert data["top_opportunities"][0]["rank"] == 1
    assert data["top_opportunities"][0]["title"] == "Delete unattached EBS volumes"
    assert data["account"] == "123456789012"


# ── HTTP failures are caught and return False ──────────────────────────────────

def test_send_event_returns_false_on_http_error(monkeypatch):
    monkeypatch.setenv("N8N_WEBHOOK_URL", "https://example.com/webhook/abc")

    async def _fail_post(url, json=None, **kwargs):
        raise Exception("Connection refused")

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = _fail_post

    connector = N8nConnector()
    with patch("finops.connectors.saas.n8n.httpx.AsyncClient", return_value=mock_client):
        result = _run(connector.send_event("anomaly_detected", {"service": "EC2"}))

    assert result is False


def test_send_event_returns_false_on_non_2xx(monkeypatch):
    monkeypatch.setenv("N8N_WEBHOOK_URL", "https://example.com/webhook/abc")

    async def _bad_post(url, json=None, **kwargs):
        resp = MagicMock()
        resp.status_code = 500
        return resp

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = _bad_post

    connector = N8nConnector()
    with patch("finops.connectors.saas.n8n.httpx.AsyncClient", return_value=mock_client):
        result = _run(connector.send_event("audit_complete", {}))

    assert result is False


def test_send_anomaly_never_raises_on_failure(monkeypatch):
    """send_anomaly must catch all errors and return False, never raise."""
    monkeypatch.setenv("N8N_WEBHOOK_URL", "https://bad-host.invalid/webhook/x")

    async def _raise(*args, **kwargs):
        raise RuntimeError("network down")

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = _raise

    connector = N8nConnector()
    with patch("finops.connectors.saas.n8n.httpx.AsyncClient", return_value=mock_client):
        result = _run(connector.send_anomaly({
            "provider": "aws",
            "service": "EC2",
            "account_id": "123",
            "severity": "high",
            "direction": "spike",
            "pct_change": 200.0,
            "z_score": 5.0,
            "baseline_mean": 100.0,
            "current_amount": 300.0,
        }))

    assert result is False
