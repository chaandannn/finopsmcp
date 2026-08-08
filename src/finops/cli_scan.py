"""
`nable scan` - the terminal front door.

One command, existing local AWS credentials, the recoverable dollars on your
account in under 60 seconds. No MCP client, no config, no LLM call, no secrets
typed, and NO paid API calls: the default scan reads only free AWS APIs
(Describe*, Compute Optimizer, CloudWatch GetMetricStatistics), so a tool we
market as free never puts a charge on the user's own AWS bill.

The spend breakdown (month-to-date total + top services + % of bill) lives
behind the opt-in `--spend` flag, because it needs Cost Explorer, which AWS
meters at $0.01 per request. `--spend` discloses that cost before calling.

Output contract (the design doc is the source of truth):

    nable scan · profile default                       <- first print, <2s, no network
    account 352112345678 · this account only           <- after STS returns
    scanning 4 regions ...
      us-east-1 ......... 3 findings
      eu-west-1 ......... 1 finding
    ────────────────────────────────────────────
    $2,140/mo recoverable
      $1,200/mo  3 idle RDS instances (db.r5.xlarge), us-east-1
      ...

    (with --spend, a headline is added above the recoverable line:)
    $48,210 on AWS this month. Top: Bedrock $19.2k · EC2 $11.4k · S3 $4.1k

Exit codes (pinned contract; argparse owns 2 for usage errors):
    0  success, including partial WITH results (banner shown)
    3  credentials expired (prints the exact refresh command)
    4  permission denied everywhere (prints the IAM actions needed)
    5  partial with no usable results
    6  no credentials found, or AWS rejected the ones it found
    7  local AWS config does not resolve (unknown profile, unparseable config,
       no region). Distinct from 6: the machine HAS a setup, it just is wrong.

Failure states never stack-trace; every one ends with a docs link. Telemetry
events (cli_scan_started / _completed / _failed) carry only event name, error
class and flags: no dollar figures, no account IDs, and they honor
NABLE_NO_TELEMETRY. The terminal event is sent synchronously before exit so
slow-account runs never lose their completion mark.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time

DOCS_LINE = "docs: https://getnable.com/docs/cli"
_FINDING_FLOOR_USD = 25.0  # findings below this monthly value stay out of v1 output
_MAX_FINDINGS_SHOWN = 5
_SCAN_DEADLINE_S = 45.0
_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d+$")  # validates region names; filters CE NoRegion/global

# Exit codes. argparse exits 2 on usage errors; never reuse it here.
EXIT_OK = 0
EXIT_EXPIRED = 3
EXIT_DENIED = 4
EXIT_PARTIAL_EMPTY = 5
EXIT_NO_CREDS = 6
# Local AWS config is wrong (missing profile, unparseable config, no region).
# Distinct from no-creds: the machine HAS a setup, it just does not resolve.
EXIT_CONFIG = 7


# ── tiny ANSI layer (self-contained: importing wizard helpers would be a cycle) ──

def _tty() -> bool:
    return sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m" if _tty() else s


def _dim(s: str) -> str:
    return f"\033[2m{s}\033[0m" if _tty() else s


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m" if _tty() else s


def _usd(v: float) -> str:
    if v >= 1000:
        return f"${v:,.0f}"
    return f"${v:,.2f}" if v < 100 else f"${v:,.0f}"


def _short_usd(v: float) -> str:
    return f"${v / 1000:.1f}k" if v >= 10_000 else _usd(v)


# ── telemetry (name + error class + flags only; never dollars or account IDs) ──

def _emit(event: str, props: dict, wait: bool) -> None:
    try:
        from . import telemetry

        payload = {"command": "scan", **props}
        if wait:
            telemetry._send_event(telemetry._get_install_id(), event, payload)
        else:
            threading.Thread(
                target=telemetry._send_event,
                args=(telemetry._get_install_id(), event, payload),
                daemon=True,
            ).start()
    except Exception:
        pass  # telemetry must never break the scan


# ── failure rendering: problem + cause + exact fix + docs link, never a trace ──

def _fail(out, code: int, lines: list[str], error_class: str, t0: float,
          exc: Exception | None = None, props: dict | None = None) -> int:
    for line in lines:
        print(line, file=out)
    print(_dim(DOCS_LINE), file=out)
    # version + exception CLASS NAME only (never the message: messages carry
    # paths and account details). Without these, a month of real failures was
    # one opaque "other" bucket nobody could diagnose remotely. `site` is the
    # caller's line, stamped here so every _fail call, present and future, is
    # locatable: line numbers drift across releases, but every event also
    # carries the version, and the pair pins the exact statement. `props` is for
    # extra diagnosis a specific site can add; same rule applies: names and
    # version strings only, never a message, never a path.
    from . import __version__ as _v
    _site = ""
    try:
        _site = f"cli_scan:{sys._getframe(1).f_lineno}"
    except Exception:
        pass
    _emit(
        "cli_scan_failed",
        {"error_class": error_class, "duration_s": round(time.time() - t0, 1),
         "version": _v, "exc_type": type(exc).__name__ if exc else "",
         "site": _site, **(props or {})},
        wait=True,
    )
    return code


def _finish(code: int, lingering: bool) -> int:
    """Return normally, or hard-exit when the deadline abandoned live threads.

    A timed-out scan leaves boto3 worker threads blocked in the C layer; they
    are non-daemon, so a normal return hangs at interpreter shutdown waiting for
    them (up to the full per-region duration). Output and telemetry are already
    flushed by the caller before this runs, so os._exit is safe and instant.
    Gated on the engine's real-abandonment flag, so mocked-report unit tests
    (which have no live threads) take the normal return path.
    """
    if lingering:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    return code


def _classify_boto_error(exc: Exception) -> str:
    """Map a botocore exception to one of our typed failure classes.

    Every class here has to earn a distinct FIX line. A class that cannot tell the
    user what to do next is worth nothing, and "other" is the bucket we are trying
    to empty: it was 100% of observed scan failures while covering four unrelated
    causes, which made the telemetry useless for diagnosing any of them.
    """
    name = type(exc).__name__
    # Local config problems. These fail instantly, before any network, and used to
    # fall through to "other" with a raw botocore string and no fix line.
    if name == "ProfileNotFound":
        return "profile-missing"
    if name in ("ConfigParseError", "ConfigNotFound"):
        return "config-broken"
    if name in ("NoRegionError",):
        return "no-region"
    if name in ("NoCredentialsError", "CredentialRetrievalError", "PartialCredentialsError"):
        return "no-creds"
    if name in ("SSOTokenLoadError", "UnauthorizedSSOTokenError", "TokenRetrievalError"):
        return "expired"
    code = ""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = (resp.get("Error") or {}).get("Code", "")
    if code in ("ExpiredToken", "ExpiredTokenException", "RequestExpired"):
        return "expired"
    # NOT expired. InvalidClientTokenId means the access key ID does not exist and
    # SignatureDoesNotMatch means the secret is wrong; neither is fixed by
    # re-authenticating, so sending the user to `aws sso login` wastes their time
    # and hides a typo'd or revoked key.
    if code in ("InvalidClientTokenId", "SignatureDoesNotMatch", "AuthFailure",
                "UnrecognizedClientException", "InvalidAccessKeyId"):
        return "bad-creds"
    if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation"):
        return "denied"
    # Cannot reach AWS at all: proxy, VPN, TLS interception, DNS, offline. These
    # were 97% of real-world scan failures ("other", duration 0s) before they
    # were classified, because a corporate machine fails the first STS call
    # instantly and none of these exception names were mapped.
    if name in ("EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError",
                "ProxyConnectionError", "ConnectionClosedError", "SSLError"):
        return "network"
    return "other"


def _available_profiles() -> list[str]:
    """Profiles boto3 can actually see, for the "you meant one of these" hint.
    Best-effort: a broken config is one of the cases we are reporting on, and it
    makes this raise too."""
    try:
        # Parse the config files directly. boto3.Session().available_profiles
        # cannot be used here: constructing the session honors AWS_PROFILE, so on
        # the exact failure we are reporting (that profile does not exist) it
        # raises ProfileNotFound and we would tell the user they have no profiles
        # while staring at the one they meant.
        import botocore.session
        return sorted(botocore.session.Session().full_config.get("profiles", {}))
    except Exception:
        return []


# ── Cost Explorer: at most 2 queries, one page each (CE bills $0.01/request) ──

def _spend_window(today) -> tuple[str, str, str]:
    """The CE TimePeriod to ask for, plus a label for what it covers.

    Split out of _spend_snapshot so the month boundary is testable on any day of
    the year. It used to live inline and only ever ran correctly on days 2-31:
    on the 1st, month-to-date is an empty window that Cost Explorer rejects, and
    the code returned a hand-made zero. So for one day in thirty nable told
    people their cloud bill was $0.00 and named no services, which is worse than
    an error because it looks like an answer. Ask for the month that just closed
    instead and say so: it is complete, it is the number they want on the 1st,
    and it is never a lie. CE end dates are exclusive, so `end` is safe to leave
    at the first of the current month.
    """
    from datetime import timedelta

    first_of_month = today.replace(day=1)
    if today == first_of_month:
        prev_start = (first_of_month - timedelta(days=1)).replace(day=1)
        return prev_start.isoformat(), first_of_month.isoformat(), "last month"
    return first_of_month.isoformat(), today.isoformat(), "month-to-date"


def _spend_snapshot(session) -> dict | None:
    """Month-to-date total + by-service + by-region from CE. None if denied."""
    from datetime import date

    ce = session.client("ce", region_name="us-east-1")
    start, end, covers = _spend_window(date.today())

    def _grouped(dimension: str) -> list[tuple[str, float]]:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": dimension}],
            # No pagination follow-up: one request per dimension keeps the
            # documented "at most $0.06 per scan" promise true on wide accounts.
        )
        rows: list[tuple[str, float]] = []
        for result in resp.get("ResultsByTime", []):
            for group in result.get("Groups", []):
                key = group["Keys"][0]
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                rows.append((key, amount))
        return rows

    services = _grouped("SERVICE")
    regions = _grouped("REGION")
    total = sum(v for _, v in services)
    services.sort(key=lambda kv: kv[1], reverse=True)
    return {
        "period": f"{start} to {end}",
        "covers": covers,
        "total": total,
        "services": services[:3],
        "regions": dict(regions),
    }


# Regions most AWS accounts concentrate spend in. Absent paid Cost Explorer data
# to rank by, scan these first so a deadline cutoff trims the empty long tail,
# not the region actually holding the waste (usually us-east-1).
_DEFAULT_REGION_PRIORITY = [
    "us-east-1", "us-west-2", "us-east-2", "eu-west-1",
    "eu-central-1", "eu-west-2", "ap-southeast-1", "ap-southeast-2",
    "ap-northeast-1", "ap-south-1", "us-west-1", "ca-central-1",
]


def _pick_regions(spend: dict | None, session) -> list[str]:
    """All opted-in regions, ordered so the caller's deadline trims the tail.

    We do NOT cap or drop regions: without the (paid) CE spend data there is no
    free way to know which regions carry cost, and capping to an arbitrary N
    risks skipping the exact region holding the waste. Empty regions scan fast,
    and run_deep_audit's deadline bounds the worst case. Ordering is what keeps
    that deadline from cutting off the region that matters: with `--spend` CE
    data, scan by spend descending; without it, fall back to a default prior
    (the regions most accounts spend the most in) so the long tail is trimmed,
    never us-east-1.
    """
    from .analyzers.optimizer import _discover_regions

    discovered = _discover_regions(session)
    if spend and spend.get("regions"):
        weight = {r: v for r, v in spend["regions"].items() if _REGION_RE.match(r)}
        discovered.sort(key=lambda r: weight.get(r, 0.0), reverse=True)
    else:
        rank = {r: i for i, r in enumerate(_DEFAULT_REGION_PRIORITY)}
        discovered.sort(key=lambda r: rank.get(r, len(_DEFAULT_REGION_PRIORITY)))
    return discovered


# ── demo: same output path on the StreamCo dataset; the engine is never faked ──

def _demo_payload() -> tuple[dict, dict]:
    from . import demo_data

    cs = demo_data.cost_summary()
    services = sorted(cs["by_service"].items(), key=lambda kv: kv[1], reverse=True)
    spend = {
        "period": cs["period"],
        "total": cs["total_usd"],
        "services": services[:3],
        "regions": {},
    }
    findings = [
        {
            "waste_type": "idle_nat_gateway",
            "description": "4 NAT gateways with no traffic in 30 days",
            "region": "us-east-1",
            "estimated_monthly_savings": 12960.0,
        },
        {
            "waste_type": "unattached_ebs",
            "description": "212 unattached EBS volumes (48 TB, gp2)",
            "region": "us-east-1",
            "estimated_monthly_savings": 4680.0,
        },
        {
            "waste_type": "old_snapshots",
            "description": "1,900 EBS snapshots older than a year",
            "region": "us-west-2",
            "estimated_monthly_savings": 3120.0,
        },
        {
            "waste_type": "idle_rds",
            "description": "3 idle RDS instances (db.r5.xlarge, <2% CPU)",
            "region": "eu-west-1",
            "estimated_monthly_savings": 2840.0,
        },
        {
            "waste_type": "oversized_ec2",
            "description": "9 EC2 instances under 8% peak CPU (m5.2xlarge)",
            "region": "us-east-1",
            "estimated_monthly_savings": 2210.0,
        },
    ]
    total = sum(f["estimated_monthly_savings"] for f in findings)
    report = {
        "account_id": "demo",
        "regions_scanned": ["us-east-1", "us-west-2", "eu-west-1"],
        "regions_timed_out": [],
        "total_findings": len(findings),
        "total_estimated_monthly_savings": total,
        "total_estimated_annual_savings": total * 12,
        "findings": findings,
        "errors": [],
    }
    return spend, report


# ── rendering ──────────────────────────────────────────────────────────────────

def _render_extra(out, b) -> None:
    """Render one AI/GCP/Azure provider block. A non-ok block is a quiet note,
    never a failure of the whole scan."""
    if b.status != "ok":
        print(f"  {_dim(b.label + ':')} {_dim(b.note or b.status)}", file=out)
        return
    bits = []
    if b.spend_usd is not None:
        tag = _dim(" [estimated]") if b.estimated else ""
        bits.append(f"{_usd(b.spend_usd)}/mo" + tag)
    if b.recoverable_usd:
        tag = _dim(" [early]") if b.early_recoverable else ""
        bits.append(_green(f"{_usd(b.recoverable_usd)}/mo recoverable") + tag)
    head = "   ".join(bits) if bits else _dim("connected")
    print(_bold(b.label) + f"   {head}", file=out)
    if b.detail:
        print(f"      {_dim(b.detail)}", file=out)
    if b.note:
        print(f"      {_dim(b.note)}", file=out)


def _render(out, spend, report, *, demo: bool, ce_denied: bool, extra_blocks=None):
    extra_blocks = extra_blocks or []
    demo_tag = _dim(" (demo data)") if demo else ""
    print("─" * 60, file=out)

    total_spend = 0.0
    total_recoverable = 0.0
    providers_ok = 0
    _has_aws = report is not None

    if _has_aws:
        providers_ok += 1
        recoverable = float(report.get("total_estimated_monthly_savings") or 0.0)
        total_recoverable += recoverable

        if spend and spend["total"] > 0:
            total_spend += spend["total"]
            top = " · ".join(f"{name} {_short_usd(v)}" for name, v in spend["services"])
            # "this month" is a lie on the 1st, when the snapshot falls back to the
            # month that just closed. Say which window the number covers.
            when = "last month" if spend.get("covers") == "last month" else "this month"
            print(
                _bold(f"{_usd(spend['total'])} on AWS {when}.") + f" Top: {top}{demo_tag}",
                file=out,
            )
            if recoverable >= _FINDING_FLOOR_USD:
                pct = f" ({recoverable / spend['total'] * 100:.1f}% of spend)" if spend["total"] else ""
                print(_green(_bold(f"{_usd(recoverable)}/mo recoverable{pct}")) + demo_tag, file=out)
        else:
            if ce_denied:
                print(
                    _dim(
                        "spend summary unavailable (missing ce:GetCostAndUsage; "
                        "run `nable iam-template` to fix)"
                    ),
                    file=out,
                )
            if recoverable >= _FINDING_FLOOR_USD:
                print(_green(_bold(f"{_usd(recoverable)}/mo recoverable")) + demo_tag, file=out)

        findings = [
            f
            for f in report.get("findings", [])
            if float(f.get("estimated_monthly_savings") or 0) >= _FINDING_FLOOR_USD
        ][:_MAX_FINDINGS_SHOWN]

        if recoverable < _FINDING_FLOOR_USD:
            # The proud state: a clean account is a result, not an apology.
            print(_green("no material waste found, nice") + demo_tag, file=out)
        else:
            for f in findings:
                monthly = float(f.get("estimated_monthly_savings") or 0)
                desc = f.get("description") or f.get("waste_type", "finding")
                region = f.get("region", "")
                print(f"  {_usd(monthly) + '/mo':>12}  {desc}" + (f", {region}" if region else ""), file=out)

        timed_out = report.get("regions_timed_out") or []
        if timed_out:
            done = len(report.get("regions_scanned") or [])
            print(
                _dim(f"scanned {done} of {done + len(timed_out)} regions "
                     f"(reached the {_SCAN_DEADLINE_S}s time limit; skipped: {', '.join(timed_out)})"),
                file=out,
            )

    # ── extra providers (AI / GCP / Azure), the cross-provider frame ──
    for b in extra_blocks:
        _render_extra(out, b)
        if b.status == "ok":
            providers_ok += 1
            if b.spend_usd:
                total_spend += b.spend_usd
            if b.recoverable_usd:
                total_recoverable += b.recoverable_usd

    # Unified summary only when the scan spans more than the AWS block, so an
    # AWS-only run stays byte-identical to v1.
    if extra_blocks:
        # Dedup cloud-native AI (Bedrock/Vertex): under --spend it is counted in
        # BOTH the AI block and the cloud spend total, so subtract it from the
        # grand total once. On the default path the AI block excludes it, so this
        # is 0 and the total is unchanged.
        cloud_native_ai = sum(
            amt
            for b in extra_blocks if b.family == "ai"
            for prov, amt in b.by_provider.items() if prov in ("bedrock", "vertex")
        )
        if cloud_native_ai and total_spend > cloud_native_ai:
            total_spend -= cloud_native_ai

        print("─" * 60, file=out)
        parts = []
        if total_spend > 0:
            parts.append(f"{_usd(total_spend)}/mo visible")
        parts.append(_green(f"{_usd(total_recoverable)}/mo recoverable"))
        print(
            _bold(" · ".join(parts))
            + _dim(f"  across {providers_ok} provider{'s' if providers_ok != 1 else ''}"),
            file=out,
        )

    if _has_aws and not (spend and spend.get("total")):
        print(_dim("run `nable scan --spend` for the spend breakdown (uses Cost Explorer, ~$0.02)"), file=out)
    print(_dim(DOCS_LINE), file=out)


def _json_payload(spend, report, *, demo, profile, account_id, duration_s, extra_blocks=None):
    extra_blocks = extra_blocks or []
    report = report or {}
    recoverable = float(report.get("total_estimated_monthly_savings") or 0.0)
    return {
        "schema_version": 1,
        "command": "scan",
        "demo": demo,
        "profile": profile,
        "account_id": account_id,
        "spend": (
            {
                "period": spend["period"],
                # On the 1st this is last month's closed total, not month-to-date;
                # `covers` says which, so a consumer never has to guess.
                "covers": spend.get("covers", "month-to-date"),
                "month_to_date_usd": round(spend["total"], 2),
                "top_services": [
                    {"service": name, "usd": round(v, 2)} for name, v in spend["services"]
                ],
            }
            if spend
            else None
        ),
        "recoverable": {
            "monthly_usd": round(recoverable, 2),
            "annual_usd": round(recoverable * 12, 2),
            "pct_of_spend": (
                round(recoverable / spend["total"] * 100, 2)
                if spend and spend["total"]
                else None
            ),
        },
        "findings": report.get("findings", [])[:_MAX_FINDINGS_SHOWN * 4],
        "scan": {
            "regions_scanned": report.get("regions_scanned", []),
            "regions_timed_out": report.get("regions_timed_out", []),
            "errors": report.get("errors", []),
            "duration_s": round(duration_s, 1),
            "partial": bool(report.get("regions_timed_out")),
        },
        "providers": [
            {
                "family": b.family,
                "status": b.status,
                "spend_usd": round(b.spend_usd, 2) if b.spend_usd is not None else None,
                "recoverable_usd": round(b.recoverable_usd, 2) if b.recoverable_usd is not None else None,
                "estimated": b.estimated,
                "note": b.note,
            }
            for b in extra_blocks
        ],
    }


# ── the command ────────────────────────────────────────────────────────────────

def run(args) -> int:
    t0 = time.time()
    as_json = bool(getattr(args, "json", False))
    demo = bool(getattr(args, "demo", False)) or os.getenv("FINOPS_DEMO") == "1"
    want_spend = bool(getattr(args, "spend", False))
    profile = getattr(args, "profile", None) or os.getenv("AWS_PROFILE") or "default"
    if getattr(args, "profile", None):
        os.environ["AWS_PROFILE"] = args.profile
    import logging
    if getattr(args, "debug", False):
        logging.basicConfig(level=logging.DEBUG)
    else:
        # Per-region check failures (a least-privilege user missing ELB/ECR/ECS
        # describe perms, a region with no snapshots API, etc.) are expected and
        # are pure noise in a CLI whose whole value is a clean result. Keep the
        # analyzers' warnings out of stderr unless --debug asked for them.
        logging.getLogger("finops.analyzers").setLevel(logging.ERROR)

    # Human output goes to stdout; in --json mode the progress chrome moves to
    # stderr so stdout stays a single parseable document.
    out = sys.stderr if as_json else sys.stdout

    # First print: no network, within 2s of process start.
    print(f"{_bold('nable scan')} {_dim('· profile ' + profile)}", file=out)
    _emit("cli_scan_started", {"demo": demo}, wait=False)

    if demo:
        demo_spend, report = _demo_payload()
        # Demo mirrors real behavior: the spend headline only appears with --spend.
        spend = demo_spend if want_spend else None
        from .scan_assembler import demo_extra_blocks
        extra = demo_extra_blocks(want_spend)
        print(_dim("account demo · StreamCo demo dataset (demo data)"), file=out)
        _render(out, spend, report, demo=True, ce_denied=False, extra_blocks=extra)
        if as_json:
            print(json.dumps(_json_payload(
                spend, report, demo=True, profile=profile, account_id="demo",
                duration_s=time.time() - t0, extra_blocks=extra,
            ), indent=2))
        _emit("cli_scan_completed", {
            "demo": True, "providers": len(extra) + 1,
            "duration_s": round(time.time() - t0, 1),
        }, wait=True)
        return EXIT_OK

    # ── pre-flight typed probes: these drive exit codes, never engine strings ──
    #
    # This used to be a bare `except ImportError` reporting "boto3 is not
    # installed". It caught EVERY ImportError raised anywhere inside boto3's own
    # import chain (a half-installed wheel, a broken transitive dep, an
    # architecture mismatch on the interpreter) and told all of them to reinstall
    # a package that was already there. It also passed no exception to _fail, so
    # exc_type arrived empty and the failures were undiagnosable: on 2026-08-05,
    # 14 failures across 6 machines on current versions all landed here with
    # nothing to go on. Separate "absent" from "present but will not import", and
    # always hand the exception over so the class name is recorded.
    try:
        import boto3  # noqa: F401
        import botocore.exceptions  # noqa: F401
    except BaseException as exc:            # noqa: BLE001 - a probe reports, never swallows
        import importlib.util

        try:
            installed = importlib.util.find_spec("boto3") is not None
        except BaseException:               # a broken meta-path finder counts as unknown
            installed = False

        # The import chain that can take boto3 down: a version skew anywhere in
        # it produces "cannot import name X". Reproduced 2026-08-06: boto3
        # 1.43.66 over a stale botocore 1.34.0 (a distro-owned copy pip will not
        # upgrade) dies exactly this way. Recording the four version strings
        # turns a remote "ImportError" into a named conflict. Versions only:
        # no paths, no messages.
        deps: dict[str, str] = {}
        try:
            from importlib.metadata import version as _pkg_version
            for _pkg in ("boto3", "botocore", "s3transfer", "urllib3"):
                try:
                    deps[f"{_pkg}_version"] = _pkg_version(_pkg)
                except Exception:
                    deps[f"{_pkg}_version"] = "absent"
        except Exception:
            pass

        if not installed:
            # boto3 is a hard dependency and the published wheel declares it, so
            # "absent" means this environment installed the package without its
            # dependencies. Name that, because "reinstall" alone sends people to
            # repeat the command that already skipped them.
            from .install_health import missing_core_dependencies
            others = [d for d in missing_core_dependencies() if d not in ("boto3", "botocore")]
            lines = ["nable is installed but its dependencies are not."]
            if others:
                lines.append(f"  also missing: {', '.join(others)}")
            lines += [
                "  usually a `pip install --no-deps`, a pruned container layer, "
                "or a partial copy of site-packages",
                "  fix: `pip install --upgrade --force-reinstall finops-mcp`",
                "  or run isolated, no cleanup needed: `uvx --python 3.12 nable scan`",
            ]
            cls = "missing_dep"
        else:
            _b3 = deps.get("boto3_version", "?")
            _bc = deps.get("botocore_version", "?")
            lines = [
                f"boto3 is installed but will not import ({type(exc).__name__}).",
                f"  found: boto3 {_b3} with botocore {_bc}; a mismatched pair "
                "(often a system-owned copy pip cannot upgrade) fails exactly here",
                "  fix: `pip install --upgrade --force-reinstall boto3 botocore`",
                "  or run isolated, no cleanup needed: `uvx --python 3.12 nable scan`",
            ]
            cls = "broken_dep"
        return _fail(out, 1, lines, cls, t0, exc, props=deps)

    # Connection-aware: what else is configured besides AWS? Detection uses the
    # same connected_families() the MCP server reads, so connecting on either
    # surface is immediately visible to the other.
    from .tool_surface import connected_families
    try:
        _fams = connected_families()
    except Exception:
        _fams = frozenset()
    _extra_fams = _fams & {"llm", "gcp", "azure"}

    # No AWS credentials at all, but other providers connected: scan those instead
    # of failing. A pure AI-startup box may have OPENAI_API_KEY and no ~/.aws.
    try:
        _no_aws_creds = boto3.Session().get_credentials() is None
    except Exception:
        _no_aws_creds = False
    if _no_aws_creds and _extra_fams:
        from .scan_assembler import gather_extra_providers
        print(_dim("no AWS credentials found · scanning your other connected providers"), file=out)
        blocks, abandoned = gather_extra_providers(_fams, spend=want_spend)
        _render(out, None, None, demo=False, ce_denied=False, extra_blocks=blocks)
        if as_json:
            print(json.dumps(_json_payload(
                None, None, demo=False, profile=profile, account_id=None,
                duration_s=time.time() - t0, extra_blocks=blocks,
            ), indent=2))
        _emit("cli_scan_completed", {
            "demo": False, "no_aws": True,
            "providers": len([b for b in blocks if b.status == "ok"]),
            "duration_s": round(time.time() - t0, 1),
        }, wait=True)
        return _finish(EXIT_OK, abandoned)

    try:
        session = boto3.Session()
        if session.get_credentials() is None:
            return _fail(out, EXIT_NO_CREDS, [
                "no AWS credentials found on this machine",
                "  looked in: env vars, ~/.aws/credentials, ~/.aws/config (SSO), instance metadata",
                "  fix: `aws configure sso` (company SSO) or `aws configure` (access key)",
                "  then: `nable connect` waits and connects the moment they appear",
            ], "no-creds", t0)
        sts = session.client("sts")
        ident = sts.get_caller_identity()
        account_id = ident["Account"]
    except Exception as exc:
        klass = _classify_boto_error(exc)
        if klass == "expired":
            return _fail(out, EXIT_EXPIRED, [
                "your AWS session has expired",
                f"  fix: `aws sso login --profile {profile}`  (or refresh your temporary credentials)",
            ], "expired", t0, exc=exc)
        if klass == "no-creds":
            return _fail(out, EXIT_NO_CREDS, [
                "no usable AWS credentials found",
                "  fix: `aws configure sso` (company SSO) or `aws configure` (access key)",
                "  then: `nable connect` waits and connects the moment they appear",
            ], "no-creds", t0, exc=exc)
        if klass == "denied":
            return _fail(out, EXIT_DENIED, [
                "this AWS identity cannot call sts:GetCallerIdentity",
                "  fix: `nable iam-template` prints the read-only policy nable needs",
            ], "permission", t0)
        if klass == "profile-missing":
            # The single most common instant failure: AWS_PROFILE is exported in
            # the user's shell (normal for anyone with more than one account) and
            # does not resolve. This used to print a raw botocore string with no
            # fix line at all, which is why people retried and left.
            env_profile = os.environ.get("AWS_PROFILE") or os.environ.get("AWS_DEFAULT_PROFILE")
            found = _available_profiles()
            lines = [f"AWS profile {profile!r} is not configured on this machine"]
            if env_profile == profile:
                lines.append(f"  AWS_PROFILE={env_profile} is set in your environment")
            if found:
                lines.append(f"  profiles nable can see: {', '.join(found)}")
                lines.append(f"  fix: `nable scan --profile {found[0]}`, or unset AWS_PROFILE")
            else:
                lines.append("  nable cannot see any configured profiles")
                lines.append("  fix: `aws configure sso` (company SSO) or `aws configure` (access key)")
            return _fail(out, EXIT_CONFIG, lines, "profile-missing", t0)
        if klass == "config-broken":
            return _fail(out, EXIT_CONFIG, [
                "your AWS config could not be parsed",
                f"  {exc}",
                "  fix: open that file and check for an unclosed [section] header or a stray line",
            ], "config-broken", t0, exc=exc)
        if klass == "no-region":
            return _fail(out, EXIT_CONFIG, [
                "no AWS region is configured",
                "  fix: `export AWS_DEFAULT_REGION=us-east-1` (or set `region` in ~/.aws/config)",
            ], "no-region", t0, exc=exc)
        if klass == "bad-creds":
            return _fail(out, EXIT_NO_CREDS, [
                "AWS rejected these credentials",
                f"  profile {profile!r}: the access key is unknown, revoked, or the secret does not match",
                "  fix: `aws sts get-caller-identity` to confirm, then `aws configure` to replace them",
            ], "bad-creds", t0, exc=exc)
        if klass == "network":
            return _fail(out, 1, [
                "cannot reach AWS from this machine",
                f"  {type(exc).__name__}: the request never got an answer",
                "  fix: check VPN / proxy. Behind a corporate proxy, set HTTPS_PROXY;",
                "  with TLS interception, point AWS_CA_BUNDLE at your company root cert",
            ], "network", t0, exc=exc)
        # Genuinely unclassified. Keep the engine string so it is at least
        # reportable, and say what to do with it.
        return _fail(out, 1, [
            f"could not reach AWS: {exc}",
            "  fix: `nable scan --debug` prints the full traceback",
            "  if that does not explain it, please open an issue with the output",
        ], "other", t0, exc=exc)

    # Scope is always labeled, never detected: no organizations API, no
    # permission trap, never wrong. Org-aware payer detection waits for CUR.
    print(_dim(f"account {account_id} · this account only"), file=out)

    # ── spend snapshot: OPT-IN ONLY ──
    # The default scan makes zero paid API calls, so a free tool never charges
    # the user's own AWS account. `--spend` adds the Cost Explorer breakdown,
    # which AWS bills at ~$0.02 per scan; we disclose that before the call. The
    # flag is the consent, so no interactive prompt (would break --json/CI).
    spend = None
    ce_denied = False
    if want_spend:
        print(
            _dim("spend breakdown: 2 Cost Explorer calls, about $0.02 on your AWS bill"),
            file=out,
        )
        try:
            spend = _spend_snapshot(session)
        except Exception as exc:
            if _classify_boto_error(exc) == "denied":
                ce_denied = True
            elif _classify_boto_error(exc) == "expired":
                return _fail(out, EXIT_EXPIRED, [
                    "your AWS session expired mid-run",
                    f"  fix: `aws sso login --profile {profile}`, then rerun",
                ], "expired", t0, exc=exc)
            # any other CE hiccup: proceed without the spend headline

    override = getattr(args, "regions", None)
    if override:
        bad = [r for r in override if not _REGION_RE.match(r)]
        if bad:
            return _fail(out, 1, [f"not valid region name(s): {', '.join(bad)}"], "other", t0)
        regions = override
    else:
        regions = _pick_regions(spend, session)
    if not regions:
        return _fail(out, EXIT_DENIED, [
            "could not determine any scannable region",
            "  this identity lacks ec2:DescribeRegions",
            "  fix: `nable iam-template` prints the read-only policy nable needs",
        ], "permission", t0)

    print(f"scanning {len(regions)} region{'s' if len(regions) != 1 else ''} ...", file=out)

    from .analyzers.optimizer import run_deep_audit

    def _progress(region: str, count: int, done: int, total: int) -> None:
        # Only surface regions that actually found something; a 17-region account
        # printing a dozen "0 findings" lines is noise, not progress.
        if count:
            print(f"  {region:<18} {count} finding{'s' if count != 1 else ''}", file=out)

    report = run_deep_audit(
        account_id=account_id,
        regions=regions,
        progress_callback=_progress,
        deadline_seconds=_SCAN_DEADLINE_S,
    )

    if report.get("error"):
        return _fail(out, 1, [f"scan failed: {report['error']}"], "other", t0)

    scanned = report.get("regions_scanned") or []
    has_results = bool(scanned)
    lingering = bool(report.get("_threads_abandoned"))

    if not has_results:
        # AWS produced nothing (hit the time limit). Don't blank the whole cross-provider
        # frame: if other providers are connected, still gather and show them, with
        # AWS degraded to a note. AWS failing is one row, not the whole scan.
        extra_blocks, extra_abandoned = ([], False)
        if _extra_fams:
            from .scan_assembler import gather_extra_providers
            extra_blocks, extra_abandoned = gather_extra_providers(_fams, spend=want_spend)
        lingering = lingering or extra_abandoned
        if any(b.status == "ok" for b in extra_blocks):
            print(_dim("AWS: hit the 45s time limit, no regions finished; showing your other providers"), file=out)
            _render(out, None, None, demo=False, ce_denied=False, extra_blocks=extra_blocks)
            if as_json:
                print(json.dumps(_json_payload(
                    None, None, demo=False, profile=profile, account_id=account_id,
                    duration_s=time.time() - t0, extra_blocks=extra_blocks,
                ), indent=2))
            _emit("cli_scan_completed", {
                "demo": False, "aws_timeout": True,
                "providers": len([b for b in extra_blocks if b.status == "ok"]),
                "duration_s": round(time.time() - t0, 1),
            }, wait=True)
            return _finish(EXIT_OK, lingering)
        # truly nothing usable anywhere
        code = _fail(out, EXIT_PARTIAL_EMPTY, [
            "the scan hit its 45s time limit before any region finished",
            "  try a narrower run: `nable scan --regions us-east-1`",
        ], "timeout", t0)
        return _finish(code, lingering)

    # Cross-provider frame: gather AI/GCP/Azure alongside the AWS block. Each has
    # its own timeout and degrades to a note; free-by-default holds (only --spend
    # touches Cost Explorer / the BigQuery export / cloud-native AI).
    extra_blocks, extra_abandoned = ([], False)
    if _extra_fams:
        from .scan_assembler import gather_extra_providers
        extra_blocks, extra_abandoned = gather_extra_providers(_fams, spend=want_spend)
    lingering = lingering or extra_abandoned

    _render(out, spend, report, demo=False, ce_denied=ce_denied, extra_blocks=extra_blocks)
    if as_json:
        print(json.dumps(_json_payload(
            spend, report, demo=False, profile=profile, account_id=account_id,
            duration_s=time.time() - t0, extra_blocks=extra_blocks,
        ), indent=2))

    _emit("cli_scan_completed", {
        "demo": False,
        "spend": want_spend,
        "duration_s": round(time.time() - t0, 1),
        "partial": bool(report.get("regions_timed_out")),
        "ce_denied": ce_denied,
    }, wait=True)
    return _finish(EXIT_OK, lingering)


def add_parser(sub) -> None:
    """Register the scan subcommand on the wizard's argparse tree."""
    p = sub.add_parser(
        "scan",
        help="Find spend and recoverable waste across your connected cloud and AI providers, free",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output on stdout")
    p.add_argument("--demo", action="store_true", help="run on the StreamCo sample dataset")
    p.add_argument(
        "--spend", action="store_true",
        help="add a month-to-date spend breakdown (uses Cost Explorer, ~$0.02 on your AWS bill)",
    )
    p.add_argument("--debug", action="store_true", help="full tracebacks and per-check timing")
    p.add_argument("--profile", help="AWS profile to use (default: $AWS_PROFILE or 'default')")
    p.add_argument(
        "--regions", nargs="+", metavar="REGION",
        help="scan exactly these regions instead of the auto-discovered set",
    )
