"""The overnight run: scan, review, rank, draft, deliver.

Runs on a schedule, writes the brief where the dashboard can serve it, and
pushes to Slack, Teams and email ONLY where someone opted in. The dashboard is
the primary surface and always gets the brief; the push channels are a
subscription, because a cost tool that starts messaging a channel nobody asked
it to message gets muted once and never read again.

TWO THINGS THIS DELIBERATELY WILL NOT DO WITHOUT BEING ASKED:

  - Call Cost Explorer. Every CE request bills the account it runs in. An
    overnight job that quietly adds a recurring line to the customer's own bill
    is not something to opt people into. The resource audit uses describe calls,
    which are free.
  - Call a language model. The critique pass has an LLM tier that spends the
    operator's own Anthropic tokens. Overnight, unattended, on a loop, that is a
    bill rather than a feature, so it stays off unless explicitly enabled.

Failure posture: a brief that half-ran is still worth reading, so a provider
that dies contributes a line to `gaps` rather than killing the run. A brief that
silently omitted a region is the thing to avoid.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .brief import Brief, build_brief
from .render import to_email, to_html, to_markdown, to_slack_blocks, to_slack_text

log = logging.getLogger("finops.briefing")

# Explicit, comma-separated. Empty (the default) means dashboard only.
DELIVER_ENV = "NABLE_BRIEF_DELIVER"
EMAIL_TO_ENV = "NABLE_BRIEF_EMAIL_TO"
URL_ENV = "NABLE_BRIEF_URL"
KEEP_DAYS_ENV = "NABLE_BRIEF_KEEP_DAYS"

_VALID_CHANNELS = ("slack", "teams", "email")
_DEFAULT_KEEP = 90


def channels() -> set[str]:
    """Which push channels are opted in. Unknown names are logged and dropped
    rather than silently ignored, so a typo in NABLE_BRIEF_DELIVER does not read
    as "delivery is on" for a quarter."""
    raw = os.getenv(DELIVER_ENV, "")
    wanted = {c.strip().lower() for c in raw.split(",") if c.strip()}
    unknown = wanted - set(_VALID_CHANNELS)
    if unknown:
        log.warning("%s lists unknown channel(s) %s; valid: %s",
                    DELIVER_ENV, ", ".join(sorted(unknown)), ", ".join(_VALID_CHANNELS))
    return wanted & set(_VALID_CHANNELS)


def brief_dir() -> Path:
    from ..storage.db import data_dir
    d = Path(data_dir()) / "briefs"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── gathering ─────────────────────────────────────────────────────────────────

def _gather_aws(gaps: list[str], scanned: dict) -> list[dict]:
    """The free, read-only resource audit. No Cost Explorer."""
    try:
        from ..analyzers.optimizer import run_deep_audit
    except Exception as exc:            # pragma: no cover - import guard
        gaps.append(f"AWS audit unavailable: {type(exc).__name__}")
        return []

    try:
        report = run_deep_audit()
    except Exception as exc:
        # Never the message: it can carry account ids and ARNs, and this string
        # reaches Slack and email.
        gaps.append(f"The AWS audit failed with {type(exc).__name__} and was skipped.")
        return []

    if not isinstance(report, dict) or report.get("error"):
        gaps.append(f"The AWS audit could not run: {report.get('error') if isinstance(report, dict) else 'no report'}")
        return []

    for err in report.get("errors") or []:
        gaps.append(str(err))
    timed_out = report.get("regions_timed_out") or []
    if timed_out:
        gaps.append(f"{len(timed_out)} region(s) timed out and were not scanned: "
                    f"{', '.join(map(str, timed_out[:5]))}.")

    scanned["aws_regions"] = len(report.get("regions_scanned") or [])
    scanned["checks"] = list(report.get("checks_run") or [])
    return list(report.get("findings") or [])


def gather_findings(gaps: list[str], scanned: dict) -> list[dict]:
    """Everything the overnight run looks at. One provider today; the shape is
    ready for more, and a provider that fails lands in `gaps`."""
    return _gather_aws(gaps, scanned)


# ── persistence ───────────────────────────────────────────────────────────────

def persist(brief: Brief, *, on: date | None = None) -> Path:
    """Write the brief where the dashboard reads it. JSON is the record; the
    HTML sits next to it so a brief can be opened straight off disk."""
    day = (on or date.today()).isoformat()
    d = brief_dir()
    payload = brief.to_dict()
    (d / f"{day}.json").write_text(json.dumps(payload, indent=2))
    (d / f"{day}.html").write_text(to_html(brief))
    latest = d / "latest.json"
    tmp = d / ".latest.json.tmp"
    # Write-then-rename: the dashboard may read this while the job writes it,
    # and half a JSON document renders as an empty brief rather than an error.
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(latest)
    _prune(d)
    return d / f"{day}.json"


def _prune(d: Path) -> None:
    try:
        keep = int(os.getenv(KEEP_DAYS_ENV, str(_DEFAULT_KEEP)))
    except ValueError:
        keep = _DEFAULT_KEEP
    if keep <= 0:
        return
    dated = sorted(p for p in d.glob("*.json") if p.name != "latest.json")
    for old in dated[:-keep]:
        for suffix in (".json", ".html"):
            try:
                old.with_suffix(suffix).unlink(missing_ok=True)
            except OSError:
                pass


def latest() -> dict[str, Any] | None:
    p = brief_dir() / "latest.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


# ── delivery ──────────────────────────────────────────────────────────────────

def _deliver_slack(brief: Brief, url: str | None) -> bool:
    from ..notifications import slack
    import asyncio

    blocks = to_slack_blocks(brief, url=url)
    text = to_slack_text(brief)
    send = slack.send_bot if os.getenv("SLACK_BOT_TOKEN") else slack.send_webhook
    try:
        return bool(asyncio.run(send(blocks, text)))
    except RuntimeError:                       # already inside a loop
        loop = asyncio.new_event_loop()
        try:
            return bool(loop.run_until_complete(send(blocks, text)))
        finally:
            loop.close()


def _deliver_teams(brief: Brief, url: str | None) -> bool:
    from ..notifications import teams
    import asyncio

    if not teams.is_configured():
        return False
    text = to_markdown(brief)
    if url:
        text += f"\n\n[Open the full brief]({url})"
    try:
        return bool(asyncio.run(teams.send_to_webhook(teams._webhook_url(), text)))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return bool(loop.run_until_complete(
                teams.send_to_webhook(teams._webhook_url(), text)))
        finally:
            loop.close()


def _deliver_email(brief: Brief, url: str | None) -> bool:
    from ..notifications.email_digest import send_message

    to = [a.strip() for a in os.getenv(EMAIL_TO_ENV, "").split(",") if a.strip()]
    if not to:
        log.warning("email delivery is on but %s is empty", EMAIL_TO_ENV)
        return False
    msg = to_email(brief, url=url)
    # Send to every address, then report. `any(...)` over a generator would
    # short-circuit on the first success and silently skip everyone after the
    # first recipient. One bad address must not drop the rest of the list.
    sent = [send_message(addr, msg["subject"], msg["text"], msg["html"])
            for addr in to]
    if not all(sent):
        log.warning("brief email reached %s of %s recipients", sum(sent), len(sent))
    return any(sent)


_DELIVERERS: dict[str, Callable[[Brief, str | None], bool]] = {
    "slack": _deliver_slack,
    "teams": _deliver_teams,
    "email": _deliver_email,
}


def deliver(brief: Brief, *, to: Iterable[str] | None = None,
            url: str | None = None) -> dict[str, bool]:
    """Push to the opted-in channels. One channel failing never stops another,
    and the result says which landed rather than reporting a bare success."""
    wanted = set(to) if to is not None else channels()
    url = url if url is not None else (os.getenv(URL_ENV) or None)
    results: dict[str, bool] = {}
    for name in _VALID_CHANNELS:
        if name not in wanted:
            continue
        try:
            results[name] = _DELIVERERS[name](brief, url)
        except Exception:
            log.exception("brief delivery to %s failed", name)
            results[name] = False
    return results


# ── the run ───────────────────────────────────────────────────────────────────

def run_overnight(
    *,
    findings: list[dict] | None = None,
    deliver_to: Iterable[str] | None = None,
    do_persist: bool = True,
    use_llm: bool = False,
    limit: int = 10,
    today: date | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Scan, review, rank, draft, save, deliver. Returns the brief plus what
    happened, so a caller (or a test) can assert on delivery without reading
    logs.

    use_llm defaults to False on purpose: see the module docstring. A caller who
    wants the model tier has to say so.
    """
    gaps: list[str] = []
    scanned: dict[str, Any] = {}
    raw = findings if findings is not None else gather_findings(gaps, scanned)

    brief = build_brief(raw, today=today, use_llm=use_llm, gaps=gaps,
                        scanned=scanned, limit=limit, now=now)

    path = str(persist(brief, on=today)) if do_persist else None
    delivered = deliver(brief, to=deliver_to)

    log.info("overnight brief: %s actionable, %s unconfirmed, delivered=%s",
             len(brief.actionable), len(brief.investigations), delivered or "none")
    return {"brief": brief, "path": path, "delivered": delivered,
            "summary": brief.to_dict()}
