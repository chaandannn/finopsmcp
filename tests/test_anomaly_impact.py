"""An anomaly alert has to carry dollars, not just a percentage.

The alert used to say "+180% vs 28-day avg" with a z-score and no dollar delta at
all. "+180%" on a $12/day service is noise; the same percentage on a $6,500/day
service is a budget event, and the reader had to do the subtraction to tell which
one they were looking at.

This must stay pure arithmetic. It runs in the scheduler for every alerted
anomaly, so a provider API call or an LLM hop here would put a per-alert cost on
the alerting path.
"""
from __future__ import annotations

from finops.anomaly.impact import enrich, impact
from finops.notifications.slack import anomaly_blocks
from finops.notifications.teams import anomaly_card


def _anomaly(**kw):
    base = {
        "provider": "aws", "service": "Amazon CloudFront", "account_id": "1234",
        "severity": "high", "direction": "spike", "pct_change": 180.0,
        "z_score": 4.21, "baseline_mean": 2300.0, "current_amount": 6540.0,
        "detected_at": "2026-07-25",
    }
    base.update(kw)
    return base


def test_spike_reports_the_daily_delta_and_the_run_rate():
    out = impact(_anomaly())
    assert out["delta_usd"] == 4240.0
    assert out["monthly_run_rate_usd"] == 127200.0
    assert "$4,240.00/day above its baseline" in out["impact_summary"]
    assert "$127,200.00 over the next 30 days" in out["impact_summary"]


def test_run_rate_is_conditional_not_a_prediction():
    """A one-day spike that self-corrects costs the delta once. The wording has to
    carry the "if it holds", or we are claiming a forecast we did not make."""
    assert "If it holds" in impact(_anomaly())["impact_summary"]


def test_a_drop_is_reported_as_a_drop_worth_checking():
    out = impact(_anomaly(direction="drop", current_amount=400.0, baseline_mean=2300.0))
    assert out["delta_usd"] == -1900.0
    assert "BELOW its baseline" in out["impact_summary"]
    assert "nothing broke" in out["impact_summary"]


def test_percentage_alone_cannot_distinguish_these_two():
    """Same +180%, wildly different consequence. This is the whole point."""
    noise = impact(_anomaly(baseline_mean=12.0, current_amount=33.6))
    real = impact(_anomaly(baseline_mean=2300.0, current_amount=6440.0))
    assert noise["delta_usd"] < 25
    assert real["delta_usd"] > 4000


def test_next_step_names_the_service_and_provider():
    out = impact(_anomaly())
    assert "Amazon CloudFront" in out["next_step"]
    assert "AWS" in out["next_step"]


def test_impact_survives_missing_and_junk_amounts():
    for bad in ({}, {"current_amount": None, "baseline_mean": None},
                {"current_amount": "n/a", "baseline_mean": "?"}):
        out = impact(bad)
        assert out["delta_usd"] == 0.0
        assert out["impact_summary"]
        assert out["next_step"]


def test_enrich_never_drops_the_original_fields():
    a = _anomaly()
    out = enrich(a)
    for k, v in a.items():
        assert out[k] == v
    assert "impact_summary" in out


# ── the renderers ───────────────────────────────────────────────────────────


def test_slack_alert_shows_the_impact_and_next_step():
    blocks = anomaly_blocks(enrich(_anomaly()))
    rendered = str(blocks)
    assert "$4,240.00/day above its baseline" in rendered, (
        "the Slack alert still ships a percentage with no dollar figure"
    )
    assert "Next" in rendered
    assert "Amazon CloudFront" in rendered


def test_teams_alert_shows_the_impact_and_next_step():
    rendered = str(anomaly_card(enrich(_anomaly())))
    assert "$4,240.00/day above its baseline" in rendered
    assert "Next" in rendered


def test_renderers_still_work_on_an_unenriched_anomaly():
    """Callers outside the scheduler build blocks from a bare anomaly. The alert
    must render rather than KeyError."""
    for rendered in (str(anomaly_blocks(_anomaly())), str(anomaly_card(_anomaly()))):
        assert "Cost Anomaly" in rendered
        assert "impact_summary" not in rendered


# ── the scheduler wiring ────────────────────────────────────────────────────
#
# Everything above calls enrich() directly, so none of it covers the call inside
# _detect_and_alert. Deleting that line left all 10 tests green while every real
# alert silently went back to shipping a percentage and no dollars. This drives
# the scheduler and inspects the payload actually handed to the notifier.


def test_scheduler_enriches_the_payload_it_dispatches(monkeypatch, tmp_path):
    import asyncio
    from datetime import date, datetime, timedelta, timezone

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "sched.db"))
    from finops.storage import db as _db
    monkeypatch.setattr(_db, "_ENGINE", None, raising=False)

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    with _db.get_engine().begin() as conn:
        conn.execute(_db.cost_snapshots.insert().values(
            provider="aws", service="Amazon CloudFront", account_id="1234",
            region="us-east-1", snapshot_date=yesterday, amount_usd=6540.0,
            granularity="DAILY", captured_at=datetime.now(timezone.utc),
        ))

    from finops.anomaly import detector as _det
    from finops.anomaly import seasonality as _seas
    from finops.notifications import slack as _slack
    from finops.notifications import teams as _teams
    from finops.scheduler import jobs as _jobs

    class _Anom:
        provider, service, account_id = "aws", "Amazon CloudFront", "1234"
        severity, direction = "high", "spike"
        pct_change, z_score = 180.0, 4.21
        baseline_mean, current_amount = 2300.0, 6540.0

        def summary(self):
            return "CloudFront +180%"

    monkeypatch.setattr(_seas, "detect_with_seasonality", lambda **kw: _Anom())
    monkeypatch.setattr(_det, "persist_anomaly", lambda a: (1, True))
    monkeypatch.setattr(_det, "mark_notified", lambda i: True)
    monkeypatch.setattr(_teams, "is_configured", lambda: False)
    monkeypatch.setattr(_slack, "is_configured", lambda: True)

    sent: dict = {}

    async def _capture(payload):
        sent.update(payload)
        return True

    monkeypatch.setattr(_slack, "send_anomaly_alert", _capture)

    asyncio.run(_jobs._detect_and_alert())

    assert sent, "the scheduler never dispatched an alert"
    assert sent["delta_usd"] == 4240.0, (
        "the dispatched alert carries no dollar delta; anomaly.impact.enrich is "
        "not wired into _detect_and_alert"
    )
    assert "$4,240.00/day above its baseline" in sent["impact_summary"]
    assert sent["next_step"]
