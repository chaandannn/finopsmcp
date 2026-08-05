"""The overnight run: what it must never do unattended, and what must survive.

Two properties matter more than the rest, because both are ways an unattended
loop quietly spends the customer's money:

  - it must not call Cost Explorer (every request bills their account)
  - it must not call a language model (the critique pass has an LLM tier that
    spends the operator's own Anthropic tokens)

After that: delivery is opt-in per channel, a failing channel never blocks
another, and a provider that dies contributes a gap rather than an empty brief
that looks like good news.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from finops.briefing import run as brun

TODAY = date(2026, 8, 3)
NOW = datetime(2026, 8, 3, 6, 0, tzinfo=timezone.utc)

FINDINGS = [
    {"title": "Unattached volume", "resource_type": "ebs_volume", "resource_id": "vol-1",
     "estimated_monthly_savings_usd": 212.0, "evidence": "measured",
     "metadata": {"region": "us-east-1", "attached_to": [None], "snapshot_ids": [],
                  "iac_references": [], "age_days": 610}},
]


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    # data_dir() memoises into a module global, so setting the env var alone
    # leaves every test after the first writing into the first test's tmp_path.
    import finops.storage.db as _db
    monkeypatch.setattr(_db, "_DATA_DIR", None)
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path))
    for var in (brun.DELIVER_ENV, brun.EMAIL_TO_ENV, brun.URL_ENV, brun.KEEP_DAYS_ENV):
        monkeypatch.delenv(var, raising=False)
    yield


# ── what it must never do unattended ─────────────────────────────────────────

def test_the_overnight_run_never_calls_cost_explorer(monkeypatch):
    """Every Cost Explorer request bills the customer's own account. A nightly
    job that adds a recurring line to their bill is not something to opt anyone
    into silently."""
    import boto3

    def no_ce(service, *a, **k):
        assert service not in ("ce", "cost-explorer"), \
            "the overnight run reached Cost Explorer"
        raise RuntimeError("no network in tests")

    monkeypatch.setattr(boto3, "client", no_ce, raising=False)
    brun.run_overnight(findings=FINDINGS, today=TODAY, now=NOW)


def test_the_overnight_run_never_calls_a_model_by_default(monkeypatch):
    import finops.recommendations.critique as crit

    monkeypatch.setattr(crit, "_llm_objections",
                        lambda *a, **k: pytest.fail("the overnight run called the model"))
    monkeypatch.setenv("NABLE_CRITIC_LLM", "1")          # even when the env asks
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    brun.run_overnight(findings=FINDINGS, today=TODAY, now=NOW)


def test_it_delivers_nowhere_unless_a_channel_is_opted_in(monkeypatch):
    called = []
    for name in ("slack", "teams", "email"):
        monkeypatch.setitem(brun._DELIVERERS, name,
                            lambda b, u, n=name: called.append(n) or True)
    out = brun.run_overnight(findings=FINDINGS, today=TODAY, now=NOW)
    assert out["delivered"] == {}
    assert called == [], f"delivered to {called} without being asked"


# ── delivery ─────────────────────────────────────────────────────────────────

def test_only_the_named_channels_receive(monkeypatch):
    got = []
    for name in ("slack", "teams", "email"):
        monkeypatch.setitem(brun._DELIVERERS, name,
                            lambda b, u, n=name: got.append(n) or True)
    monkeypatch.setenv(brun.DELIVER_ENV, "slack, email")
    out = brun.run_overnight(findings=FINDINGS, today=TODAY, now=NOW)
    assert sorted(got) == ["email", "slack"]
    assert out["delivered"] == {"slack": True, "email": True}


def test_an_unknown_channel_name_is_dropped_and_warned(monkeypatch, caplog):
    monkeypatch.setenv(brun.DELIVER_ENV, "slcak,email")   # typo
    with caplog.at_level("WARNING"):
        assert brun.channels() == {"email"}
    assert "slcak" in caplog.text, "a typo must not fail silently"


def test_one_channel_failing_does_not_stop_another(monkeypatch):
    def boom(brief, url):
        raise RuntimeError("webhook 500")

    monkeypatch.setitem(brun._DELIVERERS, "slack", boom)
    monkeypatch.setitem(brun._DELIVERERS, "email", lambda b, u: True)
    monkeypatch.setenv(brun.DELIVER_ENV, "slack,email")
    out = brun.run_overnight(findings=FINDINGS, today=TODAY, now=NOW)
    assert out["delivered"] == {"slack": False, "email": True}


def test_email_delivery_without_recipients_reports_failure_not_success(monkeypatch):
    monkeypatch.setenv(brun.DELIVER_ENV, "email")
    sent = []
    monkeypatch.setattr("finops.notifications.email_digest.send_message",
                        lambda *a, **k: sent.append(a) or True)
    out = brun.run_overnight(findings=FINDINGS, today=TODAY, now=NOW)
    assert out["delivered"] == {"email": False}
    assert sent == []


def test_email_goes_to_every_configured_recipient(monkeypatch):
    monkeypatch.setenv(brun.DELIVER_ENV, "email")
    monkeypatch.setenv(brun.EMAIL_TO_ENV, "a@example.invalid, b@example.invalid")
    sent = []
    monkeypatch.setattr("finops.notifications.email_digest.send_message",
                        lambda addr, subj, text, html=None: sent.append(addr) or True)
    brun.run_overnight(findings=FINDINGS, today=TODAY, now=NOW)
    assert sent == ["a@example.invalid", "b@example.invalid"]


def test_the_email_html_is_the_dashboard_document_not_escaped_text(monkeypatch):
    """send_custom_digest escapes its body into a <pre>. The brief renders its
    own HTML, so it must go through the path that delivers HTML as HTML."""
    monkeypatch.setenv(brun.DELIVER_ENV, "email")
    monkeypatch.setenv(brun.EMAIL_TO_ENV, "a@example.invalid")
    captured = {}
    monkeypatch.setattr("finops.notifications.email_digest.send_message",
                        lambda addr, subj, text, html=None: captured.update(
                            html=html, subj=subj) or True)
    brun.run_overnight(findings=FINDINGS, today=TODAY, now=NOW)
    assert "<!doctype html>" in captured["html"].lower()
    assert "&lt;div" not in captured["html"]
    assert "$212/mo" in captured["subj"]


# ── persistence ──────────────────────────────────────────────────────────────

def test_the_brief_is_saved_where_the_dashboard_reads_it(tmp_path):
    out = brun.run_overnight(findings=FINDINGS, today=TODAY, now=NOW)
    assert out["path"] and out["path"].endswith("2026-08-03.json")
    saved = brun.latest()
    assert saved["actionable_count"] == 1
    assert saved["total_monthly_usd"] == 212.0
    assert (brun.brief_dir() / "2026-08-03.html").exists()


def test_latest_survives_a_torn_write(monkeypatch):
    """The dashboard may read latest.json while the job writes it. Half a JSON
    document must not raise; it reads as "no brief yet"."""
    brun.run_overnight(findings=FINDINGS, today=TODAY, now=NOW)
    (brun.brief_dir() / "latest.json").write_text('{"headline": "trunc')
    assert brun.latest() is None


def test_old_briefs_are_pruned_but_the_retention_window_is_kept(monkeypatch):
    monkeypatch.setenv(brun.KEEP_DAYS_ENV, "3")
    for d in range(1, 8):
        brun.run_overnight(findings=FINDINGS, today=date(2026, 8, d), now=NOW)
    kept = sorted(p.name for p in brun.brief_dir().glob("2026-*.json"))
    assert kept == ["2026-08-05.json", "2026-08-06.json", "2026-08-07.json"]
    assert (brun.brief_dir() / "latest.json").exists()


def test_persistence_can_be_turned_off():
    out = brun.run_overnight(findings=FINDINGS, today=TODAY, now=NOW, do_persist=False)
    assert out["path"] is None
    assert brun.latest() is None


# ── gathering failures ───────────────────────────────────────────────────────

def test_a_failing_provider_becomes_a_gap_not_an_empty_brief(monkeypatch):
    """An empty brief reads as good news. A brief that says the audit died reads
    as what it is."""
    monkeypatch.setattr("finops.analyzers.optimizer.run_deep_audit",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = brun.run_overnight(today=TODAY, now=NOW)
    assert out["summary"]["gaps"], "a dead provider produced a silent empty brief"
    assert any("RuntimeError" in g for g in out["summary"]["gaps"])


def test_a_provider_failure_never_leaks_the_exception_message(monkeypatch):
    """Exception text carries account ids and ARNs, and gaps reach Slack and
    email. Only the type is safe to forward."""
    # Named `sensitive`, not `secret`: detect-secrets flags the keyword itself,
    # and this is a fake ARN on AWS's own documentation account id.
    sensitive = "arn:aws:iam::123456789012:role/prod-admin"
    monkeypatch.setattr("finops.analyzers.optimizer.run_deep_audit",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError(sensitive)))
    out = brun.run_overnight(today=TODAY, now=NOW)
    assert sensitive not in json.dumps(out["summary"])
    assert "123456789012" not in json.dumps(out["summary"])


def test_timed_out_regions_are_reported(monkeypatch):
    monkeypatch.setattr("finops.analyzers.optimizer.run_deep_audit", lambda *a, **k: {
        "findings": [], "regions_scanned": ["us-east-1"], "checks_run": ["ebs"],
        "regions_timed_out": ["ap-southeast-2", "sa-east-1"], "errors": []})
    out = brun.run_overnight(today=TODAY, now=NOW)
    gaps = " ".join(out["summary"]["gaps"])
    assert "2 region(s) timed out" in gaps and "ap-southeast-2" in gaps


def test_the_open_core_ships_no_schedule_for_the_brief():
    """The brief OBJECT is open; the SCHEDULE is not.

    0.8.205 briefly shipped an APScheduler job here. That put the always-on loop
    on the wrong side of the open/closed line (BOUNDARY.md: the watch loop, the
    scheduler and the hosted surfaces are the closed layer), and it grew a second
    loop beside the one nable-enterprise already had. The schedule now lives in
    nable_enterprise/brief.py, riding the existing watch loop and its
    push-on-change dedup.

    What stays open is everything a person can run on demand: `nable brief`,
    build_brief, the resource map, the renderers.
    """
    import inspect

    from finops.scheduler import jobs

    src = inspect.getsource(jobs)
    assert "morning_brief" not in src, (
        "the brief schedule belongs in nable-enterprise, not the open core"
    )
    assert not hasattr(jobs, "job_morning_brief")


def test_the_brief_is_still_fully_available_on_demand():
    """Removing the schedule must not remove the product. Everything the CLI and
    an enterprise tick need is still importable from the open package."""
    from finops.briefing import build_brief, to_html, to_markdown, to_slack_blocks
    from finops.briefing.run import gather_findings, latest, run_overnight

    for fn in (build_brief, to_html, to_markdown, to_slack_blocks,
               gather_findings, latest, run_overnight):
        assert callable(fn)


def test_run_overnight_still_works_without_any_scheduler():
    out = brun.run_overnight(findings=FINDINGS, today=TODAY, now=NOW)
    assert out["summary"]["actionable_count"] == 1
    assert out["delivered"] == {}
