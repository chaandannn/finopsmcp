#!/usr/bin/env python3
"""
finops setup: interactive CLI wizard for configuring providers securely.

Usage:
  finops setup                        # walk through all providers
  finops setup aws                    # configure AWS only
  finops setup aws --iam-template     # print least-privilege IAM CloudFormation
  finops setup aws --iam-terraform    # print least-privilege IAM Terraform snippet
  finops setup aws --check-scope      # verify your AWS key is read-only
  finops setup azure                  # configure Azure only
  finops setup gcp                    # configure GCP only
  finops setup slack                  # configure Slack notifications
  finops setup teams                  # configure Teams notifications
  finops setup sso                    # configure enterprise SSO (OIDC / Okta / Azure AD)
  finops setup vault list             # list stored credential keys
  finops setup vault delete KEY       # delete a stored credential
  finops setup vault rotate           # rotate the master encryption key
"""
from __future__ import annotations

from ._preflight import require_python
require_python()

import getpass
import os
import re
import sys
from pathlib import Path


def _parse_combined_aws_paste(combined: str) -> tuple[str, str] | None:
    """Parse the CloudFormation "NableSetupPaste" output ("AccessKeyId:SecretAccessKey")
    into its two halves, or None if it does not look like a valid pair. Colons never
    appear in either an AWS access key ID or secret key, so a plain split is safe."""
    if not combined or ":" not in combined:
        return None
    key, _, secret = combined.partition(":")
    if key.startswith("AK") and len(key) >= 16 and len(secret) >= 20:
        return key, secret
    return None


def _prompt(msg: str, secret: bool = False, default: str = "") -> str:
    # Avoid a doubled default marker. Some callers embed a yes/no hint like
    # "[Y/n]" or "(y/N)" in the message themselves; only append "[default]"
    # when there is not already a choice hint there, so prompts never read
    # "Write config? [Y/n] [y]:".
    _has_hint = any(h in msg for h in ("[Y/n]", "[y/N]", "[y/n]", "(y/N)", "(Y/n)", "(y/n)"))
    if default and not _has_hint:
        msg = f"{msg} [{default}]"
    msg += ": "
    try:
        val = getpass.getpass(msg) if secret else input(msg)
    except (KeyboardInterrupt, EOFError):
        print()
        return default
    return val.strip() or default


def _section(title: str) -> None:
    from .welcome import bold, _rule
    print(f"\n  {_rule()}")
    print(f"  {bold(title)}")
    print(f"  {_rule()}")


def _ok(msg: str) -> None:
    from .welcome import green
    print(f"  {green('✓')} {msg}")


def _warn(msg: str) -> None:
    from .welcome import amber
    print(f"  {amber('⚠')}  {msg}")


def _err(msg: str) -> None:
    from .welcome import red
    print(f"  {red('✗')} {msg}", file=sys.stderr)


# ── AWS account listing ───────────────────────────────────────────────────────

def _print_aws_accounts() -> None:
    """Print all configured AWS accounts in a readable table."""
    from .accounts import list_accounts, _load_yaml

    accounts = list_accounts()
    if not accounts:
        print("  No AWS accounts configured yet. Run: finops setup aws")
        return

    data = _load_yaml()

    default_name = data.get("default_account", "")

    print(f"\n  AWS accounts ({len(accounts)}):\n")
    for acct in accounts:
        is_default = acct.name == default_name
        marker = "*" if is_default else " "
        acct_id = f"  [{acct.account_id}]" if acct.account_id else ""
        auth = ""
        if acct.role_arn:
            auth = f"  role: {acct.role_arn}"
        elif acct.profile:
            auth = f"  profile: {acct.profile}"
        else:
            auth = "  IAM key"
        print(f"   {marker} {acct.name}{acct_id}{auth}")

    print("\n  * = default account")
    print("  Add another: finops setup aws --add")
    print("  Org import:  finops setup aws --org")


# ── Provider wizards ──────────────────────────────────────────────────────────

_VALID_AWS_REGIONS = {
    # US
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    # Europe
    "eu-west-1", "eu-west-2", "eu-west-3", "eu-central-1", "eu-central-2",
    "eu-north-1", "eu-south-1", "eu-south-2",
    # Asia Pacific
    "ap-southeast-1", "ap-southeast-2", "ap-southeast-3", "ap-southeast-4",
    "ap-northeast-1", "ap-northeast-2", "ap-northeast-3",
    "ap-south-1", "ap-south-2", "ap-east-1",
    # Other
    "ca-central-1", "ca-west-1",
    "sa-east-1",
    "me-south-1", "me-central-1",
    "af-south-1",
    "il-central-1",
    # GovCloud
    "us-gov-west-1", "us-gov-east-1",
}

_GOVCLOUD_REGIONS = {"us-gov-west-1", "us-gov-east-1"}


def setup_aws() -> None:
    _section("AWS: Cost Explorer")

    print("""  Before entering credentials, make sure your IAM user or role has
  these permissions. Without them, cost queries will fail.

  Required IAM permissions:
    ce:GetCostAndUsage
    ce:GetCostForecast
    ce:GetDimensionValues
    sts:GetCallerIdentity

  Quickest way to add them:
    1. Open https://console.aws.amazon.com/iam
    2. Go to Users → your user → Add permissions → Attach policies
    3. Search for "AWSBillingReadOnlyAccess" and attach it
       (or attach a custom policy with just the ce:* actions above)

  Already done? Press Enter to continue.
""")
    try:
        input("  → ")
    except (KeyboardInterrupt, EOFError):
        print()

    from .security import iam_setup as _iam
    _oneclick = _iam.quick_create_available()
    print("  Choose how to connect:")
    if _oneclick:
        print("  1) One-click CloudFormation  (easiest: opens AWS, creates a read-only key for you)")
        print("  2) Paste an existing IAM access key")
        print("  3) IAM Identity Center / SSO (for teams)")
        choice = _prompt("  Choice", default="1")
        _method = "sso" if choice == "3" else ("key" if choice == "2" else "oneclick")
    else:
        print("  1) IAM Access Key (simple, works for personal accounts)")
        print("  2) IAM Identity Center / SSO (recommended for teams)")
        choice = _prompt("  Choice", default="1")
        _method = "sso" if choice == "2" else "key"

    # The credential step is the activation cliff; record which path users pick so
    # we can see which one converts. Threaded so the POST never blocks the prompt.
    try:
        from . import telemetry as _tel
        import threading as _th
        _th.Thread(
            target=_tel._send_event,
            args=(_tel._get_install_id(), "aws_connect_method", {"method": _method}),
            daemon=True,
        ).start()
    except Exception:
        pass

    from .security.vault import Vault
    vault = Vault.default()

    if _method == "sso":
        start_url = _prompt("  SSO Start URL (e.g. https://myco.awsapps.com/start)")
        region = _prompt("  SSO region", default="us-east-1")
        account_id = _prompt("  AWS account ID to use")
        role_name = _prompt("  IAM role name (e.g. ReadOnlyAccess)")
        from .security.oauth.aws import start_device_flow, poll_for_token, store_sso_credentials
        print("\n  Starting device authorization flow...")
        try:
            flow = start_device_flow(start_url, region)
        except Exception as e:
            _err(f"Failed to start SSO device flow: {e}")
            _warn("Check that your SSO Start URL and region are correct.")
            return
        try:
            tokens = poll_for_token(flow)
        except KeyboardInterrupt:
            print("\n  SSO authorization cancelled.")
            return
        except TimeoutError:
            _err("SSO authorization timed out (5 minutes). Run 'finops setup aws' to try again.")
            return
        except Exception as e:
            _err(f"SSO token exchange failed: {e}")
            return
        try:
            store_sso_credentials(tokens, region, account_id, role_name)
        except Exception as e:
            _err(f"Failed to store SSO credentials: {e}")
            return
    else:
        access_key = None
        secret_key = None
        if _method == "oneclick":
            url = _iam.quick_create_url()
            print("""
  One-click read-only key via CloudFormation:
    1. We open the AWS console with a read-only stack pre-loaded.
    2. Tick the "I acknowledge that AWS CloudFormation might create IAM
       resources" box, click "Create stack", and wait ~30 seconds.
    3. Open the stack's "Outputs" tab. Copy the "NableSetupPaste" value
       (one combined value, so there is only one thing to copy).
    4. Paste it below. Delete the stack any time to revoke the key.
""")
            try:
                import webbrowser
                _opened = webbrowser.open(url)
            except Exception:
                _opened = False
            from .welcome import link as _link
            if _opened:
                print(f"  Opened AWS in your browser. If nothing opened, use this link:\n    {_link(url)}\n")
            else:
                print(f"  Open this link in your browser:\n    {_link(url)}\n")
            try:
                input("  Press Enter once the stack shows CREATE_COMPLETE → ")
            except (KeyboardInterrupt, EOFError):
                print()

            # Single combined paste (NableSetupPaste = "AccessKeyId:SecretAccessKey")
            # replaces two separate copy-pastes with their own validation loops.
            # An empty answer, or one that doesn't parse, falls through to the
            # original two-prompt flow below rather than failing the setup.
            combined = _prompt("  Paste the NableSetupPaste value (or press Enter to enter the two values separately)", secret=True)
            parsed = _parse_combined_aws_paste(combined)
            if parsed:
                access_key, secret_key = parsed
            elif combined:
                _warn("That didn't look like a valid combined paste, falling back to entering the two values.")
        else:
            print("""
  Create an access key:
    1. IAM → Users → your user → Security credentials
    2. Access keys → Create access key → choose "Other" → Create
    3. Copy both values below (the secret is only shown once)
""")

        if access_key is None:
            access_key = _prompt("  AWS Access Key ID (starts with AKIA...)")
            while not access_key.startswith("AK") or len(access_key) < 16:
                if not access_key:
                    # User hit Enter/Ctrl-C with no input; abort rather than loop forever
                    _warn("No Access Key ID entered. Run 'finops setup aws' to try again.")
                    return
                _warn("That doesn't look like a valid Access Key ID (should start with AKIA and be 20 chars)")
                access_key = _prompt("  AWS Access Key ID")

            secret_key = _prompt("  AWS Secret Access Key", secret=True)
            while len(secret_key) < 20:
                if not secret_key:
                    _warn("No Secret Access Key entered. Run 'finops setup aws' to try again.")
                    return
                _warn("That doesn't look like a valid Secret Access Key")
                secret_key = _prompt("  AWS Secret Access Key", secret=True)

        # Region with validation
        while True:
            region = _prompt("  AWS region (press Enter for us-east-1)", default="us-east-1")
            if region in _VALID_AWS_REGIONS:
                if region in _GOVCLOUD_REGIONS:
                    print("  GovCloud region detected. nable works with AWS GovCloud out of the box. All data stays on your machine.")
                break
            _warn(f"'{region}' is not a valid AWS region. Examples: us-east-1, us-west-2, eu-west-1, us-gov-west-1")

        role_arns = _prompt("  Role ARNs for additional accounts (comma-separated, or blank)")
        vault.store("AWS_ACCESS_KEY_ID", access_key)
        vault.store("AWS_SECRET_ACCESS_KEY", secret_key)
        vault.store("AWS_DEFAULT_REGION", region)
        if role_arns:
            vault.store("AWS_ROLE_ARNS", role_arns)
        _ok("AWS credentials stored in vault")

    # Test connection + Cost Explorer permissions
    try:
        import boto3
        from datetime import date, timedelta
        vault.load_to_env()
        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

        print("  Verifying credentials with AWS...", flush=True)
        sts = boto3.client("sts", region_name=region)
        identity = sts.get_caller_identity()
        _ok(f"Connection verified: account {identity['Account']}")

        # Check Cost Explorer access specifically, this is the core permission
        ce_ok = False
        try:
            ce = boto3.client("ce", region_name="us-east-1")
            end = date.today()
            start = end - timedelta(days=1)
            ce.get_cost_and_usage(
                TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
                Granularity="DAILY",
                Metrics=["UnblendedCost"],
            )
            _ok("Cost Explorer access confirmed")
            ce_ok = True
        except Exception as ce_err:
            err = str(ce_err)
            if "AccessDenied" in err or "AuthFailure" in err:
                print()
                _warn("This key is missing ce:GetCostAndUsage. Cost queries will fail.")
                print("""
  Add this inline policy to your IAM user or role:

  {
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": [
        "ce:GetCostAndUsage",
        "ce:GetCostForecast",
        "ce:GetDimensionValues"
      ],
      "Resource": "*"
    }]
  }

  Or run: finops setup aws --iam-template
  to generate a full least-privilege CloudFormation template.
""")
            elif "DataUnavailableException" in err:
                _warn("Cost Explorer enabled but data not ready yet. AWS takes up to 24h to backfill. Try again tomorrow.")
                ce_ok = True  # credentials work, data just not ready yet

        try:
            from . import telemetry as _tel
            _tel._send_event(_tel._get_install_id(), "provider_connected", {
                "provider": "aws",
                "ce_access": ce_ok,
                # _method ("oneclick"/"key"/"sso") is the menu-renumber-safe value;
                # deriving from the raw choice broke after the one-click menu shift.
                "auth_method": _method,
            })
        except Exception:
            pass

    except Exception as e:
        error_code = getattr(e, "response", {}).get("Error", {}).get("Code", "") if hasattr(e, "response") else ""
        if error_code == "InvalidClientTokenId":
            _err("The Access Key ID looks wrong. Double-check the key ID in IAM Console.")
        elif error_code in ("AuthFailure", "SignatureDoesNotMatch"):
            _err("Secret key mismatch. Re-generate the access key pair in IAM Console.")
        elif error_code == "AccessDenied":
            _err("Access denied. Ensure the IAM user has sts:GetCallerIdentity permission.")
        else:
            _warn(f"Connection test failed: {e}")
        try:
            from . import telemetry as _tel
            # error_code computed above; for plain exceptions fall back to class name
            _tel._send_event(_tel._get_install_id(), "provider_connect_failed", {
                "provider": "aws",
                "error_type": type(e).__name__,
                "error_code": error_code or type(e).__name__,
            })
        except Exception:
            pass


def _aws_account_alias(session) -> str:
    """Best-effort account alias for auto-naming. Empty if no iam permission."""
    try:
        aliases = session.client("iam").list_account_aliases().get("AccountAliases", [])
        return aliases[0] if aliases else ""
    except Exception:
        return ""


def _active_profile_name(explicit_profile: str | None) -> str:
    """The profile label to record for a probe. A named profile wins; otherwise
    the AWS_PROFILE / AWS_DEFAULT_PROFILE the default chain resolved through, so
    `AWS_PROFILE=crosstx-mfa finops setup aws` names the account crosstx-mfa and
    stores that reference rather than the account id."""
    import os as _os
    return (
        explicit_profile
        or _os.environ.get("AWS_PROFILE")
        or _os.environ.get("AWS_DEFAULT_PROFILE")
        or ""
    )


def _detect_aws_candidates() -> list:
    """Probe the machine for working AWS credentials. No prompts, no writes.

    Returns dicts {label, profile, account_id, alias, region}, each already
    verified via STS get_caller_identity. Named profiles are preferred over the
    ambient default chain because the MCP server can reproduce a profile but may
    not inherit shell env vars when Claude Desktop spawns it.
    """
    import boto3
    import queue as _queue
    import threading
    import time as _time

    def _probe(profile):
        try:
            from botocore.config import Config as _BotoConfig
            _cfg = _BotoConfig(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1})
            session = boto3.Session(profile_name=profile) if profile else boto3.Session()
            ident = session.client("sts", config=_cfg).get_caller_identity()
            return {
                # When creds resolve through the default chain, the AWS_PROFILE the
                # user exported IS the profile they chose; record it. That names the
                # account crosstx-mfa instead of aws-<id>, and makes us store a
                # profile REFERENCE (never the expiring session keys) that boto3
                # re-resolves next run, so tomorrow it just asks for a fresh login.
                "profile": _active_profile_name(profile),
                "account_id": ident["Account"],
                "alias": _aws_account_alias(session),
                "region": session.region_name or "us-east-1",
            }
        except Exception:
            return None  # no creds / expired SSO token / no permission, just skip

    try:
        profiles = boto3.Session().available_profiles
    except Exception:
        profiles = []

    # Probe every named profile plus the ambient default chain (None) concurrently
    # under a hard wall-clock deadline that is ACTUALLY honored.
    #
    # This used to run in a ThreadPoolExecutor `with` block. That bounded only how
    # long we COLLECTED results: the context manager's exit calls shutdown(wait=True),
    # which then blocked on every straggler anyway. Measured in the wild: probes took
    # 36-72s on machines with no credentials (boto3 sitting on the EC2 metadata
    # endpoint through its retries), all of it silent, and 19 of 24 machines that
    # opened AWS connect quit during that wait without ever finishing the probe.
    #
    # Now: plain DAEMON threads that are simply abandoned at the deadline (they also
    # cannot hold up interpreter exit, which an executor's atexit join can), plus a
    # hard cap on the metadata service so a firewalled IMDS cannot stall at all.
    _DEADLINE = 4.0
    targets = list(profiles) + [None]
    results: "_queue.Queue" = _queue.Queue()

    def _worker(p):
        try:
            results.put((p, _probe(p)))
        except Exception:
            results.put((p, None))

    # Only set what the user has not: never override a deliberate IMDS config.
    _meta_keys = ("AWS_METADATA_SERVICE_TIMEOUT", "AWS_METADATA_SERVICE_NUM_ATTEMPTS")
    _restore = {k: os.environ.get(k) for k in _meta_keys}
    for k, v in (("AWS_METADATA_SERVICE_TIMEOUT", "1"),
                 ("AWS_METADATA_SERVICE_NUM_ATTEMPTS", "1")):
        os.environ.setdefault(k, v)

    resolved: list = []
    try:
        for p in targets:
            threading.Thread(target=_worker, args=(p,), daemon=True).start()
        end = _time.time() + _DEADLINE
        for _ in targets:
            remaining = end - _time.time()
            if remaining <= 0:
                break
            try:
                p, r = results.get(timeout=remaining)
            except _queue.Empty:
                break  # deadline hit; abandon whatever is still running
            if r:
                r["label"] = f"profile '{p}'" if p else "default credentials"
                resolved.append(r)
    finally:
        for k, v in _restore.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # Prefer named profiles; include the default chain only for accounts a named
    # profile didn't already cover. Dedupe by account_id.
    named = [r for r in resolved if r["profile"]]
    default = [r for r in resolved if not r["profile"]]
    candidates, seen = [], set()
    for r in named + default:
        if r["account_id"] in seen:
            continue
        if not r["profile"] and any(n["account_id"] == r["account_id"] for n in named):
            continue
        candidates.append(r)
        seen.add(r["account_id"])
    return candidates


def _detect_sso_profiles_needing_login() -> list[dict]:
    """AWS Identity Center (SSO) profiles in ~/.aws/config that are configured
    but not currently logged in.

    A plain STS probe (see _detect_aws_candidates) silently skips these: an SSO
    profile with no valid cached token throws before it can call GetCallerIdentity,
    so the profile never surfaces. To an Identity-Center-only user that reads as
    "nable ignores ~/.aws/config", when really the profile just needs an
    `aws sso login` first. This finds those profiles and hands back the exact
    command, so nothing is dropped in silence.

    Returns [{profile, account_id, region, login_command}]; account_id comes from
    the profile's sso_account_id in config, so we can name the account without a
    live token. Profiles that ARE logged in resolve fine and are omitted here
    (they already show up as normal candidates).
    """
    import boto3
    import concurrent.futures

    _SSO_KEYS = ("sso_account_id", "sso_session", "sso_start_url", "sso_role_name")

    def _probe_sso(p):
        try:
            session = boto3.Session(profile_name=p)
            cfg = session._session.get_scoped_config()
        except Exception:
            return None
        if not any(k in cfg for k in _SSO_KEYS):
            return None  # not an SSO profile
        # Already logged in? Then it resolves and is a normal working candidate;
        # do not also list it as "needs login". A missing/expired token throws
        # locally (fast, no network), which is exactly the case we want to catch.
        try:
            creds = session.get_credentials()
            if creds is not None:
                creds.get_frozen_credentials()
                return None
        except Exception:
            pass
        return {
            "profile": p,
            "account_id": cfg.get("sso_account_id", ""),
            "region": cfg.get("region") or cfg.get("sso_region") or "us-east-1",
            "login_command": f"aws sso login --profile {p}",
        }

    try:
        profiles = list(boto3.Session().available_profiles)
    except Exception:
        return []
    if not profiles:
        return []

    out: list[dict] = []
    # Same bounded-deadline pattern as _detect_aws_candidates: a logged-in SSO
    # profile's token exchange can touch the network, so cap the whole sweep.
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(profiles))) as pool:
        futs = [pool.submit(_probe_sso, p) for p in profiles]
        try:
            for fut in concurrent.futures.as_completed(futs, timeout=4.0):
                r = fut.result()
                if r:
                    out.append(r)
        except concurrent.futures.TimeoutError:
            pass
    out.sort(key=lambda d: d["profile"])
    return out


def _auto_aws_name(candidate: dict, taken: set) -> str:
    """Human label for the account, kept unique.

    Prefer the account's own alias (what the org named it), then the SSO / CLI
    profile name (what the user named it locally, the label they actually
    recognize), then aws-<account_id> as a last resort. A bare account id is the
    worst option: an SSO role usually lacks iam:ListAccountAliases, so the alias
    comes back empty and every account would otherwise read as aws-822638974044.
    The profile the user picked is right there and means something to them."""
    base = (
        candidate.get("alias")
        or (candidate.get("profile") or "").strip()
        or f"aws-{candidate['account_id']}"
    )
    name, i = base, 2
    while name in taken:
        name = f"{base}-{i}"
        i += 1
    return name


def _confirm_cost_explorer(session) -> None:
    """Best-effort Cost Explorer check. Never blocks; names cause + fix on failure."""
    try:
        from datetime import date, timedelta
        ce = session.client("ce", region_name="us-east-1")
        end = date.today()
        ce.get_cost_and_usage(
            TimePeriod={"Start": (end - timedelta(days=1)).isoformat(), "End": end.isoformat()},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
        )
        _ok("Cost Explorer access confirmed")
    except Exception as ce_err:
        s = str(ce_err)
        if "AccessDenied" in s or "AuthFailure" in s:
            _warn("Connected, but this role lacks ce:GetCostAndUsage.")
            _warn("Run 'finops setup aws --iam-template' for a read-only policy to hand your platform team.")
        elif "DataUnavailableException" in s:
            _warn("Cost Explorer enabled but AWS has not backfilled data yet (up to 24h).")
        else:
            _warn(f"Connected, but could not confirm Cost Explorer: {ce_err}")


def _emit_provider_connected(auth_method: str) -> None:
    try:
        from . import telemetry as _tel
        _tel._send_event(_tel._get_install_id(), "provider_connected", {
            "provider": "aws",
            "auth_method": auth_method,
            "multi_account": True,
        })
    except Exception:
        pass


def _emit_step(step: str, **props) -> None:
    """Fire a granular onboarding-funnel event so we can see exactly which step a
    user abandons at. Opt-out, CI, and air-gap are enforced inside telemetry, so
    this is safe to call unconditionally. provider defaults to aws (the only
    instrumented connect flow today); pass provider=... to override."""
    try:
        from . import telemetry as _tel
        payload = {"step": step, "provider": "aws"}
        payload.update(props)
        _tel._send_event(_tel._get_install_id(), "setup_step", payload)
    except Exception:
        pass


def _finalize_aws_account(name: str) -> None:
    """Set default (auto if first), print the summary, offer to add another."""
    from .accounts import list_accounts, set_default_account

    all_accounts = list_accounts()
    if len(all_accounts) == 1:
        set_default_account(name)
        _ok(f"'{name}' set as default account")
    else:
        if _prompt(f"  Set '{name}' as the default account? (y/N)", default="n").lower() in ("y", "yes"):
            set_default_account(name)
            _ok(f"'{name}' set as default account")

    print(f"\n  Accounts configured ({len(all_accounts)}):")
    for acct in all_accounts:
        marker = " *" if acct.name == name else "  "
        acct_id = f"  [{acct.account_id}]" if acct.account_id else ""
        print(f"   {marker} {acct.name}{acct_id}")

    if _prompt("\n  Add another AWS account? (y/N)", default="n").lower() in ("y", "yes"):
        setup_aws_account()


def setup_aws_account() -> None:
    """
    Add an AWS account to ~/.finops-mcp/accounts.yaml.

    Detect-then-confirm: probe the machine for working credentials and connect
    the one the user confirms in a single keystroke. Region is not asked (Cost
    Explorer is global and scanners auto-discover regions). Falls back to manual
    entry only when nothing usable is found. Called by: finops setup aws
    """
    from .accounts import AccountConfig, add_account, get_boto3_session, list_accounts

    _section("AWS: Add Account")
    _emit_step("aws_connect_opened")

    existing = list_accounts()
    taken = {a.name for a in existing}
    have_ids = {a.account_id for a in existing if a.account_id}
    if existing:
        print(f"  Currently configured accounts: {', '.join(a.name for a in existing)}\n")

    print("  Checking for AWS credentials on this machine...")
    candidates = [c for c in _detect_aws_candidates() if c["account_id"] not in have_ids]
    _emit_step("ambient_probe_done", candidates=len(candidates))

    chosen = None
    if candidates:
        if len(candidates) == 1:
            c = candidates[0]
            extra = f" ({c['alias']})" if c["alias"] else ""
            _ok(f"Found working credentials: {c['label']} -> account {c['account_id']}{extra}")
            ans = _prompt("  Connect this account? [Y/n]  (or 'm' to enter manually)", default="y").lower()
            if ans in ("y", "yes", ""):
                chosen = c
            elif ans != "m":
                _emit_step("ambient_declined")
                print("  Skipped.")
                return
        else:
            print("\n  Found working credentials for multiple accounts:")
            for i, c in enumerate(candidates, 1):
                extra = f" ({c['alias']})" if c["alias"] else ""
                print(f"   {i}) {c['label']} -> account {c['account_id']}{extra}")
            print("   m) Enter credentials manually")
            pick = _prompt("  Which one?", default="1").lower()
            if pick.isdigit() and 1 <= int(pick) <= len(candidates):
                chosen = candidates[int(pick) - 1]
            elif pick != "m":
                _warn("Invalid choice. Run 'finops setup aws' to try again.")
                return
    else:
        _emit_step("no_ambient_creds")
        # Guide to the one command that fits this machine, then watch for the
        # credentials to land instead of making them re-run the whole wizard.
        chosen = _guide_and_watch_for_creds(have_ids)

    if chosen:
        _emit_step("ambient_confirmed", auth_method="profile" if chosen["profile"] else "default_chain")
        name = _auto_aws_name(chosen, taken)
        cfg = AccountConfig(
            name=name,
            account_id=chosen["account_id"],
            region=chosen["region"],
            profile=chosen["profile"],
        )
        _ok(f"Connected: account {chosen['account_id']} (saved as '{name}')")
        try:
            _confirm_cost_explorer(get_boto3_session(cfg))
        except Exception:
            pass
        add_account(cfg)
        _emit_provider_connected("profile" if chosen["profile"] else "default_chain")
        # Wire the MCP server in the same flow so there is no second command.
        _configure_claude_desktop()
        _finalize_aws_account(name)
        return

    _setup_aws_manual(taken)


def _aws_cred_fingerprint() -> tuple:
    """Cheap snapshot of anything that could become AWS credentials.

    Polled by the watcher so a real STS probe only happens when something actually
    changed on disk or in the environment. Covers env vars, the two ~/.aws files, and
    the SSO/CLI caches that `aws sso login` writes."""
    env = tuple(bool(os.getenv(k)) for k in (
        "AWS_ACCESS_KEY_ID", "AWS_PROFILE", "AWS_SESSION_TOKEN",
        "AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE"))
    stamps: list = []
    home = Path.home() / ".aws"
    for name in ("credentials", "config"):
        try:
            st = (home / name).stat()
            stamps.append((round(st.st_mtime, 2), st.st_size))
        except OSError:
            stamps.append(None)
    for sub in ("sso/cache", "cli/cache"):
        try:
            stamps.append(len(list((home / sub).iterdir())))
        except OSError:
            stamps.append(None)
    return (env, tuple(stamps))


def _watch_for_aws_creds(have_ids: set, timeout_s: float = 900.0) -> list:
    """Wait for AWS credentials to appear, then return the detected candidates.

    The only user who ever completed this path did it by leaving, running `aws
    configure` themselves, and re-running the wizard, five times over four hours.
    Watching removes the re-running: setting credentials up in another terminal is
    now enough, nable notices and connects. Ctrl-C drops to the other options."""
    import time as _t

    start = _t.time()
    last = _aws_cred_fingerprint()
    spin, i = "|/-\\", 0
    tty = sys.stdout.isatty()
    try:
        while _t.time() - start < timeout_s:
            cur = _aws_cred_fingerprint()
            if cur != last:
                last = cur
                if tty:
                    sys.stdout.write("\r  checking new credentials..." + " " * 30 + "\r")
                    sys.stdout.flush()
                found = [c for c in _detect_aws_candidates()
                         if c["account_id"] not in have_ids]
                if found:
                    if tty:
                        sys.stdout.write("\r" + " " * 72 + "\r")
                        sys.stdout.flush()
                    return found
            if tty:
                mins = int((_t.time() - start) // 60)
                sys.stdout.write(
                    f"\r  {spin[i % 4]} waiting for credentials... "
                    f"({mins}m, Ctrl-C for other options)   ")
                sys.stdout.flush()
            i += 1
            _t.sleep(1.5)
    except KeyboardInterrupt:
        pass
    if tty:
        sys.stdout.write("\r" + " " * 72 + "\r")
        sys.stdout.flush()
    return []


def _aws_cli_major() -> int | None:
    """Major version of the `aws` CLI on PATH, or None if it cannot be determined.

    `aws configure sso` exists only in v2. v1 is still widely installed (it is what
    pip and older Homebrew formulas give you), and there `aws configure sso` exits
    non-zero with "Invalid choice". Recommending it to a v1 user sends them into a
    login that cannot succeed, and the watcher then waits 15 minutes for
    credentials that will never arrive. Found by walking the flow on a machine
    with aws-cli/1.38.38."""
    import subprocess
    try:
        r = subprocess.run(["aws", "--version"], capture_output=True, text=True, timeout=5)
        blob = (getattr(r, "stdout", "") or "") + (getattr(r, "stderr", "") or "")
        m = re.search(r"aws-cli/(\d+)\.", blob)
        return int(m.group(1)) if m else None
    except Exception:
        # Never let version probing break onboarding. Unknown reads as "not v2",
        # which routes to the path that works on every version.
        return None


def _offer_run_aws_login(cmd: list[str]) -> bool:
    """gh-style: offer to run the AWS login command right here instead of sending the
    user to a second terminal. Runs it sharing this process's stdio, so `aws configure
    sso`'s prompts and its browser device-flow work exactly as if they ran it by hand.

    Returns True only when the command actually ran and exited 0. The caller needs
    that: it used to ignore the exit code, so a login that failed instantly still
    dropped the user into "waiting for credentials..." for the full timeout with no
    hint that anything had gone wrong."""
    import subprocess

    shown = " ".join(cmd)
    ans = _prompt(f"  Run `{shown}` for you now? [Y/n]", default="y").lower()
    if ans not in ("y", "yes", ""):
        return False
    _emit_step("ran_aws_login_inline", cmd=cmd[0] + " " + (cmd[1] if len(cmd) > 1 else ""))
    print()
    ok = False
    try:
        rc = subprocess.run(cmd).returncode  # inherits stdio: prompts + SSO browser flow
        ok = rc == 0
        if not ok:
            _emit_step("aws_login_failed", cmd=cmd[0] + " " + (cmd[1] if len(cmd) > 1 else ""))
            _warn(f"`{shown}` exited with status {rc}, so no credentials were created.")
    except KeyboardInterrupt:
        pass
    except (FileNotFoundError, OSError) as e:
        _emit_step("aws_login_failed", cmd=cmd[0])
        _warn(f"Could not run it ({e}).")
    print()
    return ok


def _guide_and_watch_for_creds(have_ids: set):
    """No credentials on this machine: give ONE tailored next step, then watch.

    The old path printed three competing options (CloudShell, mint a key, one-click
    CloudFormation) and then blocked on an access-key prompt the user could not
    satisfy without leaving for the AWS console. Every observed session abandoned
    right here. Now: detect what they actually have, recommend the single command
    that fits, and connect automatically when it lands. Returns a candidate or None."""
    import shutil

    from .welcome import bold, dim  # module-scope names; import locally

    print("  No AWS credentials found on this machine.\n")
    try:
        sso_pending = [s for s in _detect_sso_profiles_needing_login()
                       if s["account_id"] not in have_ids]
    except Exception:
        sso_pending = []

    run_cmd: list[str] | None = None
    _cli_major = _aws_cli_major() if shutil.which("aws") else None
    if sso_pending:
        _emit_step("guided_sso_login")
        print("  You already have an AWS Identity Center (SSO) profile. It just needs a login.")
        run_cmd = str(sso_pending[0]["login_command"]).split()
    elif _cli_major is not None and _cli_major >= 2:
        _emit_step("guided_aws_configure")
        print("  Set up AWS access, whichever fits:")
        print(f"      {bold('aws configure sso')}     company SSO / Identity Center (recommended)")
        print( "      aws configure         an IAM access key")
        run_cmd = ["aws", "configure", "sso"]
    elif _cli_major is not None:
        # v1 has no `configure sso`. Offering it here ran a command that exits
        # "Invalid choice" and then left the user watching for credentials that
        # could never appear. Offer what v1 can actually do.
        _emit_step("guided_aws_configure_v1", cli_major=_cli_major)
        print(f"  Your AWS CLI is v{_cli_major}, which has no `aws configure sso`.")
        print("  Set up AWS access, whichever fits:")
        print(f"      {bold('aws configure')}        an IAM access key (works on v1)")
        print( "      brew install awscli   upgrade to v2 for company SSO")
        run_cmd = ["aws", "configure"]
    else:
        _emit_step("guided_install_cli")
        print("  The AWS CLI is not installed. Fastest path:\n")
        print("      brew install awscli && aws configure\n")
        print("  (no CLI? Ctrl-C and pick 'paste an access key' instead)\n")

    # Never watch without a human at the keyboard: a piped or CI invocation would
    # otherwise block for the full timeout with nobody able to Ctrl-C it.
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("  Then re-run this command.\n")
        return None

    # gh-style: offer to run the login right here rather than making them open a
    # second terminal. Sharing this terminal lets the interactive prompts and the
    # SSO browser device-flow work; the watcher below connects the instant it lands.
    login_failed = False
    if run_cmd:
        login_failed = not _offer_run_aws_login(run_cmd)

    # A login that failed produces no credentials, so "waiting for credentials"
    # is a lie the user cannot see through: they sit at a spinner until the 15
    # minute timeout. Say what happened and give a path that does not depend on
    # the CLI at all before starting the watch.
    if login_failed:
        print("  That did not produce credentials. Two ways through:\n")
        print("    1. AWS CloudShell (already signed in, no key to create):")
        print("         open the >_ icon in the AWS console, then run")
        print("         pip install finops-mcp && finops welcome\n")
        try:
            from .security.iam_setup import quick_create_available, quick_create_url
            if quick_create_available():
                print("    2. Create a read-only key in your browser:")
                print(f"         {quick_create_url(region='us-east-1')}\n")
        except Exception:
            pass
        print(dim("  Or press Ctrl-C to paste an access key instead.\n"))

    # Ctrl-C is the expected way out of this screen, so it must exit clean. A stray
    # interrupt landing between the prints (not inside the watch loop) used to raise
    # and dump a traceback at the exact moment the user was already bailing.
    try:
        print(dim("  Leave this running. nable connects the moment credentials appear.\n"))
        found = _watch_for_aws_creds(have_ids)
    except KeyboardInterrupt:
        print()
        found = []
    if not found:
        _emit_step("watch_abandoned")
        print()
        return None

    if len(found) == 1:
        c = found[0]
        extra = f" ({c['alias']})" if c.get("alias") else ""
        _ok(f"Found credentials: {c['label']} -> account {c['account_id']}{extra}")
        _emit_step("watch_connected")
        return c

    print("\n  Found credentials for several accounts:")
    for i, c in enumerate(found, 1):
        extra = f" ({c['alias']})" if c.get("alias") else ""
        print(f"   {i}) {c['label']} -> account {c['account_id']}{extra}")
    pick = _prompt("  Which one?", default="1")
    if pick.isdigit() and 1 <= int(pick) <= len(found):
        _emit_step("watch_connected")
        return found[int(pick) - 1]
    return None


def _print_one_click_key_offer(region: str = "us-east-1") -> None:
    """Print how to get a read-only AWS key for people with no creds locally.

    Local console steps are always the default: nothing in this path touches
    nable's servers. When the template is published, a one-click CloudFormation
    stack is offered as an OPTIONAL convenience below the local steps, never as
    the required path, so the connect flow stays local-first by default.
    """
    from .security.iam_setup import quick_create_url, quick_create_available

    # Fastest path for someone with no local key: AWS CloudShell is already
    # authenticated with their console session, so nable's ambient-credential
    # detection fires there and shows a real bill in seconds, no key to mint, no
    # console clicking, nothing hosted by nable. This is the single biggest lever
    # for getting a no-creds user to first value inside the 5-10 minute window.
    print(
        "\n  Fastest with no local key, AWS CloudShell (already signed in):\n"
        "    1. Open AWS CloudShell (the >_ terminal icon, top of the console).\n"
        "    2. Run:  pip install finops-mcp && finops welcome\n"
        "    nable uses CloudShell's own credentials and shows your real bill on the spot.\n"
    )
    # Default path for a local/editor key: fully local. No nable-hosted step,
    # nothing leaves the machine or the user's AWS account. This stays the connect
    # flow's default to keep the local-first claim clean.
    print(
        "  Or create a read-only access key (about a minute, your own account):\n\n"
        "  1. AWS console -> IAM -> Users -> your user -> Security credentials\n"
        '  2. Create access key -> "Other" -> Create. The secret shows once.\n'
        "  3. Paste the Access Key ID and Secret below.\n"
        "  For a least-privilege policy to attach: finops setup aws --iam-template\n"
    )
    # Optional, opt-in convenience: a prefilled CloudFormation stack. It loads a
    # public, auditable read-only template, and the keys it creates never leave the
    # user's account. Shown only when the template is published, and only as an
    # alternative below the local steps, never as the required path.
    if quick_create_available():
        print(
            "  Optional one-click: opens a prefilled read-only stack (public,\n"
            "  auditable template; your keys stay in your account):\n"
        )
        print(f"     {quick_create_url(region=region)}\n")


def _setup_aws_manual(taken: set) -> None:
    """Manual credential entry. Reached only when auto-detect finds nothing, or
    the user explicitly chooses to enter credentials by hand. Verifies before
    saving so a bad input never persists a broken account."""
    from .accounts import AccountConfig, add_account, get_boto3_session
    from .security.vault import Vault
    import boto3

    region = "us-east-1"  # CE is global; scanners auto-discover regions. No prompt.
    _emit_step("manual_opened")

    print("\n  How should nable authenticate?")
    print("  1) IAM access key  (most common)")
    print("  2) Cross-account IAM role ARN")
    print("  3) AWS CLI profile  (~/.aws/config)")
    # Validate the pick instead of silently treating any typo as "enter an access
    # key", a wrong fork dumped no-creds users onto a key prompt they could not
    # satisfy, a confirmed quit point.
    while True:
        cred_choice = _prompt("  Choice", default="1")
        if cred_choice in ("1", "2", "3", ""):
            break
        _warn("Please pick 1, 2, or 3.")
    _emit_step("manual_method_selected",
               method={"2": "role_arn", "3": "profile"}.get(cred_choice, "access_key"))

    access_key = secret_key = role_arn = profile = ""
    auth_method = "access_key"

    if cred_choice == "2":
        role_arn = _prompt("  Role ARN (arn:aws:iam::ACCOUNT_ID:role/ROLE_NAME)")
        if not role_arn:
            _warn("No role ARN entered. Run 'finops setup aws' to try again.")
            return
        auth_method = "role_arn"

    elif cred_choice == "3":
        try:
            avail = boto3.Session().available_profiles
        except Exception:
            avail = []
        if not avail:
            _warn("No AWS CLI profiles found in ~/.aws/config.")
            _warn("Create one with 'aws configure' or 'aws configure sso', then re-run.")
            return
        print("\n  Profiles found in ~/.aws/config:")
        for i, p in enumerate(avail, 1):
            print(f"   {i}) {p}")
        pick = _prompt("  Pick a profile (number or name)", default="1")
        if pick.isdigit() and 1 <= int(pick) <= len(avail):
            profile = avail[int(pick) - 1]
        elif pick in avail:
            profile = pick
        else:
            _warn(f"'{pick}' is not one of your profiles. Run 'finops setup aws' to try again.")
            return
        auth_method = "profile"

    else:
        _emit_step("oneclick_offered")
        _print_one_click_key_offer(region)
        access_key = _prompt("  AWS Access Key ID (starts with AKIA or ASIA)")
        while not (access_key.startswith("AKIA") or access_key.startswith("ASIA")) or len(access_key) < 16:
            if not access_key:
                _warn("No key entered. Create one with the steps above, then re-run 'finops setup aws'.")
                return
            _warn("That doesn't look like an Access Key ID (should start with AKIA or ASIA, ~20 chars).")
            access_key = _prompt("  AWS Access Key ID")
        secret_key = _prompt("  AWS Secret Access Key", secret=True)
        while len(secret_key) < 20:
            if not secret_key:
                _warn("No Secret Access Key entered. Run 'finops setup aws' to try again.")
                return
            _warn("That doesn't look like a valid Secret Access Key")
            secret_key = _prompt("  AWS Secret Access Key", secret=True)

    cfg = AccountConfig(name="__pending__", region=region, role_arn=role_arn, profile=profile)
    if access_key:
        # Stage in env so the verify below can use them. Persist to the vault
        # only after verification succeeds.
        os.environ["AWS_ACCESS_KEY_ID"] = access_key
        os.environ["AWS_SECRET_ACCESS_KEY"] = secret_key
        os.environ["AWS_DEFAULT_REGION"] = region

    # Verify BEFORE saving. Never persist a broken config.
    _emit_step("connect_attempted", auth_method=auth_method)
    try:
        session = get_boto3_session(cfg)
        account_id = session.client("sts").get_caller_identity()["Account"]
    except Exception as e:
        _emit_step("verify_failed", auth_method=auth_method)
        try:
            from . import telemetry as _tel
            _tel._send_event(_tel._get_install_id(), "provider_connect_failed", {
                "provider": "aws", "auth_method": auth_method, "error_type": type(e).__name__,
            })
        except Exception:
            pass
        _warn(f"Could not verify credentials: {e}")
        if profile:
            _warn("That profile did not authenticate. If it is an SSO profile, run 'aws sso login' first.")
        elif role_arn:
            _warn("Check the role ARN and that your base credentials can assume it.")
        else:
            _warn("Check the access key and secret, then re-run 'finops setup aws'.")
        if access_key:
            os.environ.pop("AWS_ACCESS_KEY_ID", None)
            os.environ.pop("AWS_SECRET_ACCESS_KEY", None)
        return

    name = _auto_aws_name({"alias": _aws_account_alias(session), "account_id": account_id}, taken)
    cfg.name = name
    cfg.account_id = account_id

    if access_key:
        vault = Vault.default()
        vault.store("AWS_ACCESS_KEY_ID", access_key)
        vault.store("AWS_SECRET_ACCESS_KEY", secret_key)
        vault.store("AWS_DEFAULT_REGION", region)
        _ok("AWS credentials stored in vault")

    _ok(f"Connected: account {account_id} (saved as '{name}')")
    _confirm_cost_explorer(session)
    add_account(cfg)
    # Register the MCP server first so the entry exists, then inject region for
    # the key path. One continuous flow; no separate 'finops setup claude' step.
    _configure_claude_desktop()
    if access_key:
        _inject_aws_into_claude_config(access_key, secret_key, region)
    _emit_provider_connected(auth_method)
    _finalize_aws_account(name)


def setup_aws_org() -> None:
    """
    Onboard an entire AWS Organization: deploy one read-only role across every
    member account via a StackSet, then auto-discover and register all accounts.
    Called by: finops setup aws --org
    """
    import boto3
    from pathlib import Path
    from .accounts import discover_org_accounts, add_account, AccountConfig
    from .security.iam_setup import org_stackset_template

    _section("AWS Organizations: whole-org setup")

    # 1. Identify the management (payer) account we're running from.
    try:
        mgmt = boto3.client("sts").get_caller_identity()["Account"]
    except Exception as e:
        _err(f"Could not read your AWS identity: {e}")
        _warn("Run this from the Organizations management (payer) account.")
        return

    role_name = os.environ.get("FINOPS_ORG_ROLE_NAME", "FinOpsReadOnly")

    # 2. Write the StackSet template locally and print the one-time deploy. Cost
    #    across all accounts needs none of this (the payer's Cost Explorer already
    #    sees every linked account); the role is only for per-account resource scans.
    tpl_path = Path.home() / ".finops-mcp" / "aws-org-finops-role.yaml"
    try:
        import yaml
        tpl_path.parent.mkdir(parents=True, exist_ok=True)
        with open(tpl_path, "w") as f:
            yaml.safe_dump(org_stackset_template(), f, sort_keys=False)
    except Exception:
        tpl_path = None

    root_id = ""
    try:
        root_id = boto3.client("organizations", region_name="us-east-1").list_roots()["Roots"][0]["Id"]
    except Exception:
        pass

    _ok("Org-wide cost already works from this payer account. No per-account role needed for spend.")
    print("\n  For per-account waste scans (idle, rightsizing), deploy one read-only")
    print(f"  role ('{role_name}') to every account at once, this covers future accounts too:\n")
    if tpl_path:
        print(f"    Template written to  {tpl_path}\n")
    print("    aws cloudformation create-stack-set \\")
    print("      --stack-set-name nable-finops-readonly \\")
    print(f"      --template-body file://{tpl_path or '<template>.yaml'} \\")
    print(f"      --parameters ParameterKey=ManagementAccountId,ParameterValue={mgmt} \\")
    print("      --permission-model SERVICE_MANAGED \\")
    print("      --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false \\")
    print("      --capabilities CAPABILITY_NAMED_IAM\n")
    print("    aws cloudformation create-stack-instances \\")
    print("      --stack-set-name nable-finops-readonly \\")
    print(f"      --deployment-targets OrganizationalUnitIds={root_id or '<org-root-id, r-xxxx>'} \\")
    print("      --regions us-east-1\n")
    print("  IAM is global, so us-east-1 is fine. Already have a read-only role in every")
    print("  account? Set FINOPS_ORG_ROLE_NAME to its name and skip the deploy.\n")

    ans = _prompt("  Role deployed (or already present)? Discover and register accounts now? [Y/n]",
                  default="y").lower()
    if ans not in ("y", "yes", ""):
        print("  Run 'finops setup aws --org' again once the StackSet finishes.")
        return

    # 3. Discover + register every active account with role assumption.
    try:
        accounts = discover_org_accounts(add_all=False, role_name=role_name)
    except Exception as e:
        _err(f"Could not list org accounts: {e}")
        _warn("Your credentials need organizations:ListAccounts (present in the payer account).")
        return
    if not accounts:
        _warn("No active accounts found in your organization.")
        return

    added: list[AccountConfig] = []
    for acct in accounts:
        cfg = AccountConfig(
            name=(acct["account_name"].lower().replace(" ", "-") or acct["account_id"]),
            account_id=acct["account_id"],
            region="us-east-1",
            role_arn=f"arn:aws:iam::{acct['account_id']}:role/{role_name}",
        )
        add_account(cfg)
        added.append(cfg)
    _ok(f"Registered {len(added)} account(s) with the '{role_name}' role.")

    # 4. Verify the role actually assumes in one member account, so a missing or
    #    still-provisioning StackSet surfaces now, not as silent empty scans later.
    sample = next((c for c in added if c.account_id != mgmt), None)
    if sample:
        try:
            boto3.client("sts").assume_role(RoleArn=sample.role_arn, RoleSessionName="finops-verify")
            _ok(f"Verified: assumed '{role_name}' in {sample.account_id}. Org-wide scans are live.")
        except Exception:
            _warn(f"Could not assume '{role_name}' in {sample.account_id} yet. If you just deployed")
            _warn("the StackSet, give it a minute and re-run. Org cost rollups work regardless.")

    _configure_claude_desktop()
    print("\n  Accounts saved to ~/.finops-mcp/accounts.yaml.")
    print("  Ask in your editor: \"give me an org-wide cost breakdown by account.\"")


def setup_azure() -> None:
    _section("Azure: Cost Management")
    print("  Choose authentication method:")
    print("  1) Cloud Shell one-paste (recommended, no local az CLI needed)")
    print("  2) Service Principal (enter tenant/client/secret manually)")
    print("  3) Device code flow (browser required)")
    choice = _prompt("  Choice", default="1")

    from .security.oauth.azure import store_service_principal, start_device_flow, poll_for_token, store_credentials

    if choice == "1":
        from .security.azure_cloudshell import CLOUDSHELL_URL, generate_cloudshell_script, parse_combined_azure_paste
        script = generate_cloudshell_script()
        print(f"""
  1. Open Azure Cloud Shell (already signed in as you, no install needed):
       {CLOUDSHELL_URL}
  2. Paste the script below into it, and wait for it to finish (~30 seconds).
  3. Paste the ONE line it prints back here.

  ----- copy from here -----
{script}  ----- to here -----
""")
        try:
            import webbrowser
            webbrowser.open(CLOUDSHELL_URL)
        except Exception:
            pass
        combined = _prompt("  Paste the line Cloud Shell printed (or press Enter for manual entry)", secret=True)
        parsed = parse_combined_azure_paste(combined)
        if parsed:
            tenant_id, client_id, client_secret, sub_ids = parsed
            try:
                store_service_principal(tenant_id, client_id, client_secret, sub_ids)
                _ok("Azure credentials stored (Cost Management Reader, Reader, Monitoring Reader already assigned)")
                try:
                    from . import telemetry as _tel
                    _tel._send_event(_tel._get_install_id(), "provider_connected", {
                        "provider": "azure", "auth_method": "cloudshell_paste",
                        "subscription_count": len(sub_ids),
                    })
                except Exception:
                    pass
                return
            except Exception as e:
                _err(f"Failed: {e}")
                try:
                    from . import telemetry as _tel
                    _tel._send_event(_tel._get_install_id(), "provider_connect_failed", {
                        "provider": "azure", "error_type": type(e).__name__,
                    })
                except Exception:
                    pass
                return
        elif combined:
            _warn("That didn't look like a valid Cloud Shell output line, falling back to manual entry.")
        choice = "2"  # fall through to manual service-principal entry below

    sub_ids_raw = _prompt("  Subscription IDs (comma-separated)")
    sub_ids = [s.strip() for s in sub_ids_raw.split(",") if s.strip()]
    tenant_id = _prompt("  Tenant ID")

    if choice == "2":
        client_id = _prompt("  Client (App) ID")
        client_secret = _prompt("  Client Secret", secret=True)
        try:
            store_service_principal(tenant_id, client_id, client_secret, sub_ids)
        except Exception as e:
            _err(f"Failed: {e}")
            try:
                from . import telemetry as _tel
                _tel._send_event(_tel._get_install_id(), "provider_connect_failed", {
                    "provider": "azure",
                    "error_type": type(e).__name__,
                })
            except Exception:
                pass
            return
    else:
        try:
            state = start_device_flow(tenant_id)
        except Exception as e:
            _err(f"Failed to start Azure device flow: {e}")
            _warn("Check that your Tenant ID is correct.")
            return
        try:
            result = poll_for_token(state)
        except KeyboardInterrupt:
            print("\n  Azure authorization cancelled.")
            return
        except TimeoutError:
            _err("Azure authorization timed out. Run 'finops setup azure' to try again.")
            return
        except Exception as e:
            _err(f"Azure token exchange failed: {e}")
            return
        try:
            store_credentials(result, tenant_id, sub_ids)
        except Exception as e:
            _err(f"Failed to store Azure credentials: {e}")
            return

    _ok("Azure credentials stored")
    print()
    print("  Grant these roles to the service principal on each subscription so all")
    print("  the Azure tools work (cost, Advisor, and VM rightsizing):")
    print("    - Cost Management Reader   (cost queries, budgets, forecast)")
    print("    - Reader                   (Azure Advisor recs + VM list)")
    print("    - Monitoring Reader        (VM CPU for rightsizing)")
    print("  Example (repeat per subscription):")
    print("    az role assignment create --assignee <client-id> \\")
    print("      --role 'Monitoring Reader' --scope /subscriptions/<sub-id>")
    print("  Run `finops doctor` to verify the roles are in place.")
    try:
        from . import telemetry as _tel
        _tel._send_event(_tel._get_install_id(), "provider_connected", {
            "provider": "azure",
            "auth_method": "service_principal" if choice == "1" else "device_flow",
            "subscription_count": len(sub_ids),
        })
    except Exception:
        pass


_GCP_AMBIENT_TIMEOUT = 10.0  # detect + billing-account listing, hard cap


def _detect_gcp_ambient(timeout: float = _GCP_AMBIENT_TIMEOUT) -> dict | None:
    """Probe for working Google Cloud credentials without asking for anything.

    Sources, in google.auth's own order: GOOGLE_APPLICATION_CREDENTIALS, then
    gcloud Application Default Credentials. When found, list the open billing
    accounts so the connect is one keystroke instead of an ID scavenger hunt.
    Returns {source, project, billing: [(id, display_name)], billing_error} or
    None when there are no usable credentials. Runs in a worker thread with a
    hard cap so a hung metadata probe can never stall setup.
    """
    import concurrent.futures

    def _probe() -> dict | None:
        try:
            import google.auth
            creds, project = google.auth.default()
        except Exception:
            return None
        source = ("GOOGLE_APPLICATION_CREDENTIALS"
                  if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") else "gcloud ADC")
        billing: list[tuple[str, str]] = []
        billing_error = None
        try:
            from google.cloud import billing_v1
            client = billing_v1.CloudBillingClient(credentials=creds)
            for a in client.list_billing_accounts(timeout=6):
                if getattr(a, "open", getattr(a, "open_", True)):
                    billing.append((a.name.split("/")[-1], a.display_name or ""))
        except Exception as e:
            billing_error = f"{type(e).__name__}: {e}"
        return {"source": source, "project": project or "",
                "billing": billing, "billing_error": billing_error}

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        try:
            return ex.submit(_probe).result(timeout=timeout)
        except Exception:
            return None


def _discover_bq_export(project: str, timeout: float = 8.0) -> str | None:
    """Best-effort hunt for the standard billing-export table
    (gcp_billing_export_v1_*) in the default project's datasets. Saves the one
    prompt users reliably cannot answer from memory."""
    if not project:
        return None
    import concurrent.futures
    import itertools

    def _probe() -> str | None:
        from google.cloud import bigquery
        client = bigquery.Client(project=project)
        for ds in itertools.islice(client.list_datasets(), 25):
            for t in itertools.islice(client.list_tables(ds.reference), 50):
                if t.table_id.startswith("gcp_billing_export"):
                    return f"{project}.{ds.dataset_id}.{t.table_id}"
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        try:
            return ex.submit(_probe).result(timeout=timeout)
        except Exception:
            return None


def _gcp_emit_connected(billing_ids: list[str], bq_table: str | None, auth_method: str) -> None:
    try:
        from . import telemetry as _tel
        _tel._send_event(_tel._get_install_id(), "provider_connected", {
            "provider": "gcp",
            "billing_account_count": len(billing_ids),
            "has_bq_export": bool(bq_table),
            "auth_method": auth_method,
        })
    except Exception:
        pass


def setup_gcp() -> None:
    """Detect-then-confirm, same contract as AWS: probe for working credentials
    (env key or gcloud ADC), auto-discover the billing accounts and the BigQuery
    export table, and connect on one keystroke. Manual entry is the fallback,
    never the default."""
    from .security.oauth.gcp import import_service_account_key, store_billing_accounts

    _section("GCP: Cloud Billing")
    _emit_step("gcp_connect_opened")

    print("  Checking for Google Cloud credentials on this machine...")
    amb = _detect_gcp_ambient()

    if amb:
        proj = f", project {amb['project']}" if amb["project"] else ""
        _ok(f"Found working credentials: {amb['source']}{proj}")

        if amb["billing"]:
            if len(amb["billing"]) == 1:
                bid, bname = amb["billing"][0]
                label = f"{bid}" + (f" ({bname})" if bname else "")
                ans = _prompt(f"  Connect billing account {label}? [Y/n]", default="y").lower()
                chosen = [bid] if ans in ("y", "yes", "") else []
            else:
                print("\n  Open billing accounts on these credentials:")
                for i, (bid, bname) in enumerate(amb["billing"], 1):
                    print(f"   {i}) {bid}" + (f"  ({bname})" if bname else ""))
                pick = _prompt("  Connect which? [all]", default="all").lower()
                if pick in ("all", ""):
                    chosen = [b for b, _ in amb["billing"]]
                else:
                    idx = {int(x) for x in re.findall(r"\d+", pick)}
                    chosen = [b for i, (b, _) in enumerate(amb["billing"], 1) if i in idx]
            if chosen:
                # Persist the credential pointer so the MCP server (editor
                # context) resolves the same creds the CLI just used. For ADC
                # that is the well-known gcloud file; google.auth accepts an
                # authorized_user JSON via GOOGLE_APPLICATION_CREDENTIALS.
                from .security.vault import Vault
                from .setup_scan import gcloud_adc_path
                vault = Vault.default()
                gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
                cred_path = gac if (gac and Path(gac).expanduser().exists()) else None
                if not cred_path:
                    adc = gcloud_adc_path()
                    cred_path = str(adc) if adc else None
                if cred_path:
                    vault.store("GCP_SERVICE_ACCOUNT_KEY_PATH", cred_path)

                print("  Looking for a BigQuery billing export (detailed costs)...")
                bq_table = _discover_bq_export(amb["project"])
                if bq_table:
                    _ok(f"Found billing export: {bq_table}")
                else:
                    print("  None found. Cost data still works via the Billing API;")
                    print("  for per-SKU detail, enable the export (takes ~2 min):")
                    print("    https://console.cloud.google.com/billing/export")
                    bq_table = _prompt("  Export table if you have one (project.dataset.table, optional)",
                                       default="") or None

                projects = [amb["project"]] if amb["project"] else []
                store_billing_accounts(chosen, bq_table, projects or None)
                _ok(f"Connected: GCP billing account(s) {', '.join(chosen)}")
                _gcp_emit_connected(chosen, bq_table, "ambient_" + ("env" if amb["source"].startswith("GOOGLE") else "adc"))
                _configure_claude_desktop()
                return
            print("  Skipped.")
            return

        # Credentials work but billing accounts are not listable: keep the
        # creds, ask only for the one thing we could not discover.
        _warn("Credentials found, but they cannot list billing accounts "
              "(needs the Billing Account Viewer role).")
        print("  Your billing account IDs are here: https://console.cloud.google.com/billing")
        billing_ids_raw = _prompt("  Billing account IDs (comma-separated, XXXXXX-XXXXXX-XXXXXX)")
        billing_ids = [b.strip() for b in billing_ids_raw.split(",") if b.strip()]
        if billing_ids:
            store_billing_accounts(billing_ids, None, [amb["project"]] if amb["project"] else None)
            _ok(f"Connected: GCP billing account(s) {', '.join(billing_ids)}")
            _gcp_emit_connected(billing_ids, None, "ambient_manual_billing")
            _configure_claude_desktop()
        return

    # Nothing ambient: manual path, with the two fastest ways to fix that.
    print("  No Google Cloud credentials found on this machine.\n")
    print("  Fastest: run  gcloud auth application-default login  then re-run this.")
    print("  Or create a service account key (Billing Account Viewer role):")
    print("    https://console.cloud.google.com/iam-admin/serviceaccounts\n")
    key_path = _prompt("  Path to service account JSON key file (or Enter to cancel)", default="")
    if not key_path:
        print("  Skipped.")
        return
    if not Path(key_path).expanduser().exists():
        _err(f"File not found: {key_path}")
        return
    billing_ids_raw = _prompt("  Billing account IDs (comma-separated, format: XXXXXX-XXXXXX-XXXXXX)")
    billing_ids = [b.strip() for b in billing_ids_raw.split(",") if b.strip()]
    bq_table = _prompt("  BigQuery billing export table (optional, e.g. project.dataset.table)", default="")
    project_ids_raw = _prompt(
        "  Project IDs for resource audits (comma-separated, optional; enables the "
        "GCP waste scan for idle disks, IPs and VMs)", default="")
    project_ids = [p.strip() for p in project_ids_raw.split(",") if p.strip()]

    try:
        import_service_account_key(key_path)
        store_billing_accounts(billing_ids, bq_table or None, project_ids or None)
        _ok("GCP credentials stored")
        _gcp_emit_connected(billing_ids, bq_table or None, "service_account_key")
    except Exception as e:
        _err(f"Failed: {e}")
        try:
            from . import telemetry as _tel
            _tel._send_event(_tel._get_install_id(), "provider_connect_failed", {
                "provider": "gcp",
                "error_type": type(e).__name__,
            })
        except Exception:
            pass


# Expected prefixes for known API-key env vars. Used to catch the common paste
# mistakes (wrong provider's key, an org id in the key field) at entry time
# instead of letting a bad key turn into a confusing empty bill later.
_KEY_PREFIX_HINTS: dict[str, tuple[str, str]] = {
    "OPENAI_API_KEY":           ("sk-",     "OpenAI keys start with 'sk-'"),
    "OPENAI_ADMIN_KEY":         ("sk-",     "OpenAI admin keys start with 'sk-admin-' (or 'sk-')"),
    "ANTHROPIC_API_KEY":        ("sk-ant-", "Anthropic keys start with 'sk-ant-'"),
    "ANTHROPIC_ADMIN_KEY":      ("sk-ant-", "Anthropic admin keys start with 'sk-ant-'"),
    "OPENROUTER_API_KEY":       ("sk-or-",  "OpenRouter keys start with 'sk-or-'"),
    "OPENROUTER_PROVISIONING_KEY": ("sk-or-", "OpenRouter keys start with 'sk-or-'"),
    "REPLICATE_API_TOKEN":      ("r8_",     "Replicate tokens start with 'r8_'"),
}


def _normalize_and_check_key(env_key: str, val: str) -> tuple[str, str | None]:
    """Strip whitespace and a wrapping pair of quotes from a pasted credential, and
    return (clean_value, warning_or_None). A quoted or wrong-prefix paste is the most
    common reason a connected provider then shows an empty bill. Returns a warning
    rather than rejecting, so an evolving key format is never hard-blocked."""
    clean = (val or "").strip()
    if len(clean) >= 2 and clean[0] == clean[-1] and clean[0] in ("'", '"'):
        clean = clean[1:-1].strip()
    hint = _KEY_PREFIX_HINTS.get(env_key)
    warn = None
    if hint and clean and not clean.startswith(hint[0]):
        warn = f"{hint[1]}, double-check you pasted the right value."
    return clean, warn


def setup_saas_api_key(
    provider_name: str,
    env_vars: list[tuple[str, str, bool]],
    note: str | None = None,
) -> None:
    """Generic wizard for API-key SaaS providers.

    note: printed up front. Use it for the providers that report usage but not
    dollars unless you supply a contract rate (or that have no billing API at
    all), so a user learns why the cost shows $0 at connect time, not later.
    """
    _section(f"{provider_name}")
    if note:
        print(f"  {note}\n")
    # Deep-link the exact key page. "Where do I even get this key" is the
    # single biggest stall on the paste-a-key path; kill it before prompting.
    from .setup_scan import KEY_HELP
    _url, _hint = KEY_HELP.get(provider_name, (None, ""))
    if _url:
        print(f"  Get your key:  {_url}")
    if _hint:
        print(f"  {_hint}")
    if _url or _hint:
        print()
    from .security.vault import Vault
    vault = Vault.default()
    stored_any = False
    for env_key, label, is_secret in env_vars:
        val = _prompt(f"  {label}", secret=is_secret)
        val, warn = _normalize_and_check_key(env_key, val)
        if warn:
            _warn(warn)
        if val:
            vault.store(env_key, val)
            stored_any = True
    if stored_any:
        _ok(f"{provider_name} credentials stored in vault")
    else:
        _warn(f"No {provider_name} credentials entered. Nothing stored.")
    if stored_any:
        try:
            from . import telemetry as _tel
            _tel._send_event(_tel._get_install_id(), "provider_connected", {
                "provider": provider_name.lower().replace(" ", "_"),
            })
        except Exception:
            pass


def setup_sso() -> None:
    """Enterprise SSO wizard, configures OIDC for Okta, Azure AD, Google Workspace, etc."""
    _section("Enterprise SSO: OIDC Configuration")
    print("""
  nable supports SSO via OIDC (OpenID Connect).
  Compatible with: Okta, Azure AD / Entra ID, Google Workspace, Auth0, Ping Identity.

  Before running this wizard:
    1. Create an OAuth2 app in your IdP
    2. Set the redirect URI to: https://getnable.com/api/sso/oidc-callback
    3. Enable the "groups" claim in the ID token (steps vary by IdP, see docs)
""")
    from .security.vault import Vault
    vault = Vault.default()

    issuer = _prompt("  OIDC Issuer URL (e.g. https://company.okta.com, https://login.microsoftonline.com/<tenant-id>/v2.0)")
    client_id = _prompt("  Client ID")
    client_secret = _prompt("  Client Secret", secret=True)
    redirect_uri = _prompt(
        "  Redirect URI",
        default="https://getnable.com/api/sso/oidc-callback",
    )
    groups_claim = _prompt("  Groups claim name in JWT", default="groups")
    default_role = _prompt("  Default role for users not in any mapped group (viewer/analyst/admin)", default="viewer")

    print("""
  Map IdP groups to nable roles. Enter group names exactly as they appear in your IdP.
  Press Enter to skip a role (those users will get the default role above).
""")
    admin_groups = _prompt("  Group names for admin role (comma-separated, or blank)")
    analyst_groups = _prompt("  Group names for analyst role (comma-separated, or blank)")
    viewer_groups = _prompt("  Group names for viewer role (comma-separated, or blank)")

    # Build OIDC_ROLE_MAP JSON
    import json
    role_map: dict[str, str] = {}
    for g in [x.strip() for x in admin_groups.split(",") if x.strip()]:
        role_map[g] = "admin"
    for g in [x.strip() for x in analyst_groups.split(",") if x.strip()]:
        role_map[g] = "analyst"
    for g in [x.strip() for x in viewer_groups.split(",") if x.strip()]:
        role_map[g] = "viewer"

    if issuer:
        vault.store("OIDC_ISSUER", issuer)
    if client_id:
        vault.store("OIDC_CLIENT_ID", client_id)
    if client_secret:
        vault.store("OIDC_CLIENT_SECRET", client_secret)
    if redirect_uri:
        vault.store("OIDC_REDIRECT_URI", redirect_uri)
    if groups_claim:
        vault.store("OIDC_GROUPS_CLAIM", groups_claim)
    if default_role:
        vault.store("OIDC_DEFAULT_ROLE", default_role)
    if role_map:
        vault.store("OIDC_ROLE_MAP", json.dumps(role_map))
    _ok("SSO configuration stored in vault")
    print(f"""
  Role map configured: {json.dumps(role_map, indent=4) if role_map else "(none: all SSO users → {default_role})"}

  Next steps:
    1. Export these env vars to your Vercel project:
         OIDC_ISSUER, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET
         OIDC_REDIRECT_URI, OIDC_GROUPS_CLAIM, OIDC_ROLE_MAP, OIDC_DEFAULT_ROLE
    2. Test the flow: https://getnable.com/api/sso/oidc-start
    3. Users in your IdP will automatically receive a Pro license key on first login.

  For Azure AD: set OIDC_GROUPS_CLAIM=roles (not "groups") and assign app roles in the manifest.
  For Okta: set OIDC_GROUPS_CLAIM=groups and add the Groups claim to your authorization server.
""")


_SLACK_APP_MANIFEST = """\
display_information:
  name: nable
  description: Cloud cost intelligence. Ask nable about your cloud and AI spend.
  background_color: "#0d0f10"
features:
  bot_user:
    display_name: nable
    always_online: true
oauth_config:
  scopes:
    bot:
      - app_mentions:read
      - chat:write
      - im:history
      - im:read
      - im:write
      - reactions:read
      - reactions:write
      - users:read
      - users:read.email
settings:
  event_subscriptions:
    bot_events:
      - app_mention
      - message.im
  interactivity:
    is_enabled: true
  org_deploy_enabled: false
  socket_mode_enabled: true
  token_rotation_enabled: false
"""


def setup_slack_bot() -> None:
    """Configure the conversational Slack bot (Socket Mode, two-way)."""
    _section("Slack: Conversational Bot (questions, RCA, draft PRs and tickets)")

    # Surface the tier up front so the user learns it BEFORE the 2-minute OAuth
    # setup, not on the first @mention. Cost queries, anomalies and one-way
    # alerts are free; the two-way @nable bot is Team-only (trial passes).
    try:
        from .license import check_license
        st = check_license()
        if st.is_team:
            if getattr(st, "mode", "") == "trial" and st.days_remaining > 0:
                _ok(f"Team trial active ({st.days_remaining} day"
                    f"{'s' if st.days_remaining != 1 else ''} left). The @nable bot is unlocked.")
            else:
                _ok("Pro plan active. The @nable bot is unlocked.")
        else:
            _warn("The conversational @nable bot is a nable Team feature "
                  "($1,000/mo flat, unlimited seats), with a 7-day free trial.")
            print("    Cost queries, anomalies and one-way alerts are free. The two-way bot is not.")
            print("    Start a trial or activate a key:  finops setup license")
            print("    Plans:  https://getnable.com/#pricing")
        print()
    except Exception:
        pass

    print("""  One-time Slack app creation (about 2 minutes):
    1. Open https://api.slack.com/apps  ->  Create New App  ->  From a manifest
    2. Pick your workspace, paste the manifest below, click Create
    3. Basic Information -> App-Level Tokens -> Generate (scope: connections:write)
       That is your App Token (xapp-...)
    4. Install App to workspace. OAuth & Permissions shows your Bot Token (xoxb-...)

  Manifest to paste:
""")
    for line in _SLACK_APP_MANIFEST.splitlines():
        print(f"    {line}")
    print()

    from .security.vault import Vault
    vault = Vault.default()

    bot_token = _prompt("  Bot Token (xoxb-...)", secret=True)
    if bot_token and not bot_token.startswith("xoxb-"):
        _warn("That doesn't look like a bot token (should start with xoxb-).")
    app_token = _prompt("  App Token (xapp-...)", secret=True)
    if app_token and not app_token.startswith("xapp-"):
        _warn("That doesn't look like an app token (should start with xapp-).")
    anthropic_key = _prompt("  Anthropic API key (sk-ant-..., powers the answers)", secret=True)
    alert_channel = _prompt("  Alert channel (e.g. #finops-alerts)", default="#finops-alerts")

    # Validate the bot token before storing anything
    try:
        import httpx
        r = httpx.post(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {bot_token}"},
            timeout=10,
        ).json()
        if r.get("ok"):
            _ok(f"Token valid: workspace '{r.get('team', '?')}' as {r.get('user', '?')}")
        else:
            _warn(f"Slack rejected the bot token ({r.get('error', 'unknown')}). "
                  "Storing anyway; fix and re-run if the bot can't start.")
    except Exception as e:
        _warn(f"Couldn't validate token ({e}). Storing anyway.")

    if bot_token:
        vault.store("SLACK_BOT_TOKEN", bot_token)
    if app_token:
        vault.store("SLACK_APP_TOKEN", app_token)
    if anthropic_key:
        vault.store("ANTHROPIC_API_KEY", anthropic_key)
    vault.store("SLACK_ALERT_CHANNEL", alert_channel)

    _ok("Slack bot configured")
    print("""
  Start it:    finops-slack
  Then in Slack:
    @nable what did we spend last week?
    @nable why did our AWS bill spike?
    @nable draft a ticket for the top rightsizing recommendation

  Optional, for Terraform rightsizing PRs from Slack:
    FINOPS_TF_DIR=/path/to/terraform   GITHUB_FINOPS_TF_REPO=org/infra-repo

  Access control: set FINOPS_REQUIRE_AUTH=1 to map Slack users to nable
  roles (viewer/analyst/admin) by email. Drafting and approving PRs or
  tickets requires analyst or above.""")

    # The bot won't start without the [slack] extra, and configuring tokens does
    # not install it. Warn now instead of letting finops-slack crash on import.
    import importlib.util
    if importlib.util.find_spec("slack_bolt") is None:
        _warn('The bot needs the [slack] extra, which is not installed in this '
              'environment.')
        print('    Install it before running finops-slack:  pip install "finops-mcp[slack]"')


def setup_slack() -> None:
    _section("Slack: Cost Alerts and Daily Digest")
    print("  Choose method:")
    print("  1) Incoming Webhook (simpler)")
    print("  2) Bot Token (richer, supports buttons)")
    print("  3) Conversational bot (two-way: questions, RCA, draft PRs and tickets)")
    choice = _prompt("  Choice", default="1")
    from .security.vault import Vault
    vault = Vault.default()
    if choice == "3":
        setup_slack_bot()
        return
    if choice == "1":
        url = _prompt("  Webhook URL (from Slack App → Incoming Webhooks)", secret=True)
        vault.store("SLACK_WEBHOOK_URL", url)
    else:
        token = _prompt("  Bot Token (xoxb-...)", secret=True)
        channel = _prompt("  Channel (e.g. #finops-alerts)", default="#finops-alerts")
        vault.store("SLACK_BOT_TOKEN", token)
        vault.store("SLACK_CHANNEL", channel)
    while True:
        digest_time = _prompt("  Daily digest time (UTC, HH:MM)", default="09:00")
        try:
            parts = digest_time.split(":")
            hour_int = int(parts[0].strip())
            minute_int = int(parts[1].strip()) if len(parts) > 1 else 0
            if 0 <= hour_int <= 23 and 0 <= minute_int <= 59:
                hour, minute = str(hour_int), str(minute_int)
                break
            _warn(f"Invalid time '{digest_time}'. Hour must be 0-23 and minute 0-59.")
        except (ValueError, IndexError):
            _warn(f"Invalid time '{digest_time}'. Use HH:MM format, e.g. 09:00.")
    vault.store("FINOPS_DIGEST_CRON", f"{minute} {hour} * * *")
    _ok("Slack configured")


def setup_n8n() -> None:
    _section("n8n: Workflow Automation Webhook")
    print("""  In n8n:
    1. Create a new workflow
    2. Add a Webhook node as the trigger
    3. Set HTTP Method to POST
    4. Copy the Test URL or Production URL below

  nable will POST structured JSON to this URL when anomalies are detected,
  audits complete, or budgets are exceeded. You can then route events to
  Jira, PagerDuty, Slack, email, or any other tool in your stack.

  Optional: import the ready-made template from docs/n8n-workflow-template.json
  in your n8n instance (File > Import from file) to get a working flow in minutes.
""")
    from .security.vault import Vault
    vault = Vault.default()
    url = _prompt("  n8n Webhook URL (https://your-n8n-instance/webhook/...)", secret=False)
    if not url:
        _warn("No URL entered. Run 'finops setup n8n' to try again.")
        return
    if not url.startswith("http"):
        _warn("URL should start with http:// or https://")
        return
    vault.store("N8N_WEBHOOK_URL", url)
    _ok("n8n webhook URL stored")

    # Send a test ping
    import asyncio
    import httpx
    from datetime import datetime, timezone

    test_payload = {
        "event": "test",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "nable",
        "data": {"message": "nable connected successfully. Cost events will POST to this URL."},
    }
    print("\n  Sending test ping to n8n...")
    try:
        async def _ping() -> int:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(url, json=test_payload)
                return r.status_code

        status = asyncio.run(_ping())
        if status < 300:
            _ok(f"Test ping delivered (HTTP {status}). n8n is ready to receive nable events.")
        else:
            _warn(
                f"n8n returned HTTP {status}. "
                "Make sure the webhook node is active and the URL is correct."
            )
    except Exception as exc:
        _warn(f"Test ping failed: {exc}. Check the URL and your n8n instance.")


def setup_teams() -> None:
    _section("Microsoft Teams: Cost Alerts and Daily Digest")
    from .security.vault import Vault
    vault = Vault.default()
    url = _prompt("  Incoming Webhook URL (from Teams channel → Connectors)", secret=True)
    vault.store("TEAMS_WEBHOOK_URL", url)
    while True:
        digest_time = _prompt("  Daily digest time (UTC, HH:MM)", default="09:00")
        try:
            parts = digest_time.split(":")
            hour_int = int(parts[0].strip())
            minute_int = int(parts[1].strip()) if len(parts) > 1 else 0
            if 0 <= hour_int <= 23 and 0 <= minute_int <= 59:
                hour, minute = str(hour_int), str(minute_int)
                break
            _warn(f"Invalid time '{digest_time}'. Hour must be 0-23 and minute 0-59.")
        except (ValueError, IndexError):
            _warn(f"Invalid time '{digest_time}'. Use HH:MM format, e.g. 09:00.")
    vault.store("FINOPS_DIGEST_CRON", f"{minute} {hour} * * *")
    _ok("Teams configured")


# ── Vault management ──────────────────────────────────────────────────────────

def vault_list() -> None:
    from .security.vault import Vault
    vault = Vault.default()
    keys = vault.list_keys()
    if not keys:
        print("  Vault is empty")
        return
    print(f"\n  {len(keys)} stored credential(s):\n")
    for k in keys:
        print(f"    • {k}")


def vault_delete(key_name: str) -> None:
    from .security.vault import Vault
    vault = Vault.default()
    if vault.delete(key_name):
        _ok(f"Deleted: {key_name}")
    else:
        _err(f"Key not found: {key_name}")


def vault_rotate() -> None:
    _section("Vault Key Rotation")
    print("  This re-encrypts all credentials with a new master key.")
    print("  The old key will be replaced in the OS keyring and/or key file.")
    confirm = _prompt("  Type 'rotate' to confirm")
    if confirm != "rotate":
        print("  Cancelled.")
        return
    from cryptography.fernet import Fernet
    from .security.vault import Vault
    new_key = Fernet.generate_key()
    vault = Vault.default()
    try:
        vault.rotate_key(new_key)
    except Exception as e:
        _err(f"Key rotation failed: {e}")
        _warn("Credentials have NOT been re-encrypted. The old key is still active.")
        return
    # Save new key only after rotation succeeded
    if not Vault._save_keyring(new_key):
        import stat
        key_path = Path(os.environ.get("FINOPS_DATA_DIR", Path.home() / ".finops")) / "vault.key"
        key_path.write_bytes(new_key)
        key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    _ok("Key rotation complete")


# ── Entrypoint ────────────────────────────────────────────────────────────────

def _check_path_warning() -> None:
    """
    Warn the user if the directory containing the `finops` executable is not in PATH.
    This is the #1 silent failure after `pip install finops-mcp` in a user install.
    """
    import shutil
    if shutil.which("finops") is None:
        # The command is running (we got here), so find our own location
        finops_bin_dir = Path(sys.executable).parent
        _warn(
            f"The 'finops' command may not be in your PATH after install.\n"
            f"  Your scripts directory: {finops_bin_dir}\n"
            f"  Add it to PATH with:\n"
            f"    export PATH=\"{finops_bin_dir}:$PATH\"\n"
            f"  Or add that line to your ~/.zshrc / ~/.bashrc"
        )


def _wizard_select_persona() -> None:
    """Interactive persona selection shown during the full setup wizard."""
    from .persona import PERSONAS, set_persona, get_persona

    _section("Your role")
    print("  What best describes your role?\n")

    persona_keys = list(PERSONAS.keys())
    for i, key in enumerate(persona_keys, 1):
        p = PERSONAS[key]
        label = p["label"].ljust(28)
        print(f"  [{i}] {label}{p['description']}")

    current = get_persona()
    print("\n  (You can change this later with: finops config --persona <role>)")

    raw = _prompt("\n  Choice", default="1")
    chosen, matched = _resolve_persona_choice(raw, persona_keys, current)
    if not matched:
        _warn(f"'{raw.strip()}' is not a role. Keeping {PERSONAS[current]['label']}. "
              f"Change anytime with: finops config --persona <role>")

    try:
        set_persona(chosen)
        _ok(f"Persona set to: {PERSONAS[chosen]['label']}")
    except Exception as e:
        _warn(f"Could not save persona: {e}")


def _resolve_persona_choice(raw: str, persona_keys: list, current: str) -> tuple:
    """Map a role-prompt answer to a persona key. Accepts a number ("2"), the
    role key itself ("finops"), or a keyword found in the key or label ("ops").
    Returns (chosen_key, matched). matched is False when nothing matched, so the
    caller can warn instead of silently falling back, the bug where a FinOps
    analyst typed "finops" and got silently set to Engineer."""
    from .persona import PERSONAS
    s = (raw or "").strip().lower()
    if s.isdigit() and 1 <= int(s) <= len(persona_keys):
        return persona_keys[int(s) - 1], True
    if s in persona_keys:
        return s, True
    for k in persona_keys:
        if s and (s in k or s in PERSONAS[k]["label"].lower()):
            return k, True
    return current, False


def _handle_config_cmd(parsed: object) -> None:
    """Handle: finops config --persona <role>"""
    from .persona import PERSONAS, set_persona, get_persona

    persona_val = getattr(parsed, "persona", "")

    if persona_val:
        if persona_val not in PERSONAS:
            valid = ", ".join(PERSONAS.keys())
            _err(f"Unknown persona '{persona_val}'. Valid options: {valid}")
            return
        try:
            set_persona(persona_val)
            _ok(f"Persona set to: {PERSONAS[persona_val]['label']}")
            print(f"\n  Responses will now be formatted for: {PERSONAS[persona_val]['description']}")
            print("  Restart your MCP client to apply the change.\n")
        except Exception as e:
            _err(f"Could not save persona: {e}")
    else:
        current = get_persona()
        print(f"\n  Current persona: {PERSONAS[current]['label']} ({current})")
        print("\n  Available personas:")
        for key, p in PERSONAS.items():
            marker = "*" if key == current else " "
            print(f"    {marker} {key:<12} {p['label']}")
        print("\n  Change with: finops config --persona <role>\n")


def _handle_profile_cmd(parsed: object) -> None:
    """Handle: finops profile [list|create|use|current] [name]"""
    from pathlib import Path

    profiles_dir = Path.home() / ".finops" / "profiles"
    action = getattr(parsed, "profile_action", "list") or "list"
    name = getattr(parsed, "profile_name", "") or ""

    if action == "current":
        active = os.environ.get("FINOPS_PROFILE", "").strip()
        if active:
            print(f"\n  Active profile: {active}\n")
        else:
            print("\n  Active profile: default (no FINOPS_PROFILE set)\n")
        return

    if action == "list":
        if not profiles_dir.exists() or not any(profiles_dir.iterdir()):
            print("\n  No profiles found. Create one with: finops profile create <name>\n")
            return
        active = os.environ.get("FINOPS_PROFILE", "").strip()
        print("\n  Configured profiles:\n")
        for p in sorted(profiles_dir.iterdir()):
            if p.is_dir():
                marker = "*" if p.name == active else " "
                db_exists = (p / "finops.db").exists()
                db_note = "  (db exists)" if db_exists else ""
                print(f"    {marker} {p.name}{db_note}")
        if active:
            print(f"\n  Active: {active} (set via FINOPS_PROFILE)")
        else:
            print("\n  No active profile. Set with: finops profile use <name>")
        print()
        return

    if action == "create":
        if not name:
            _err("Specify a profile name: finops profile create <name>")
            return
        profile_dir = profiles_dir / name
        if profile_dir.exists():
            _warn(f"Profile '{name}' already exists at {profile_dir}")
            return
        import stat as _stat
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_dir.chmod(_stat.S_IRWXU)
        _ok(f"Profile '{name}' created at {profile_dir}")
        print(f"\n  Activate with: finops profile use {name}")
        print(f"  Or set: export FINOPS_PROFILE={name}\n")
        return

    if action == "use":
        if not name:
            _err("Specify a profile name: finops profile use <name>")
            return
        # Print the export command and write a hint
        print(f"\n  To activate profile '{name}', run:\n")
        print(f"    export FINOPS_PROFILE={name}\n")
        print("  Add this to your shell config (~/.zshrc or ~/.bashrc) to make it permanent.")
        print(f"  Or prefix any command:  FINOPS_PROFILE={name} finops-mcp\n")
        return


def _run_agents() -> None:
    """`finops agents`: the agent team and each agent's setup status."""
    from .agent_controls import agent_team_status
    from .welcome import bold, cyan, dim, green

    team = agent_team_status()
    icon = {"active": green("*"), "needs_setup": cyan("~"), "pro_required": dim("-")}
    print()
    print(f"  {bold('The nable agent team')}   plan: {team['plan']}")
    print(dim("  Propose-only: agents advise and draft; a human approves every change."))
    print()
    for a in team["agents"]:
        print(f"  {icon.get(a['status'], ' ')} {bold(a['agent'])}  ({a['status'].replace('_', ' ')})")
        print(f"      {a['does']}")
        if a.get("learning"):
            lrn = a["learning"]
            print(dim(f"      learning: {lrn['state']} ({lrn['decisions_recorded']} decisions recorded)"))
        for step in a.get("setup", []):
            print(f"      {cyan('->')} {step}")
        print(dim(f"      {a['try']}"))
        print()


def _run_guard(parsed) -> None:
    """`finops guard`: install / manage the seamless agent cost guardrail.

    The guard is a Claude Code PreToolUse hook that auto-checks infrastructure
    commands (terraform destroy, kubectl delete, commitment purchases, ...)
    against the human-authored policy in policy.py before the agent runs them.
    Advisory and propose-only: it asks or denies, it never executes.
    """
    from . import guard
    from .welcome import _fire_telemetry, amber, bold, cyan, dim, green

    action = getattr(parsed, "guard_action", "status")
    global_scope = getattr(parsed, "guard_global", False)
    scope = "global" if global_scope else "project"
    scope_label = "~/.claude/settings.json" if global_scope else ".claude/settings.json (this project)"

    if action == "hook":
        # Machine path: Claude Code invokes this on every Bash tool call.
        raise SystemExit(guard.run_hook())

    if action == "install":
        # No license check here on purpose. The guard is free forever (see the
        # note on "agent_gate" in license.py): it is the one surface that works
        # with no cloud account, so gating it would silence it for exactly the
        # people it should reach. This used to print a "part of nable Pro" notice
        # gated on feature_available("agent_gate"), which is now permanently True
        # and so was dead code promising a paywall that does not exist.
        already = guard.is_installed(guard._settings_path(global_scope))
        # Was the existing hook one of the dead ones 0.8.194 wrote? Read it before
        # install() overwrites, so we can count how many people the uv-cache-path
        # bug actually reached. There was no guard telemetry at all before this.
        was_broken = bool(guard.broken_hook_command(guard._settings_path(global_scope)))
        try:
            path = guard.install(global_scope)
        except OSError as e:
            # Writing someone's editor config can fail for ordinary reasons (a
            # read-only file, a root-owned .claude). A traceback here reads as
            # "nable is broken" when the fix is one chmod.
            _fire_telemetry("guard_installed", {"scope": scope, "outcome": "write_failed"})
            raise SystemExit(
                f"\n  Could not write {guard._settings_path(global_scope)}: {e.strerror or e}.\n"
                f"  Check the file's permissions, then run the install again.\n")
        # Anonymous counters only: scope, whether it was already there, whether we
        # repaired a dead hook, and which command form we wrote. No paths, no
        # commands, no cost data. Honors NABLE_NO_TELEMETRY like everything else.
        _fire_telemetry("guard_installed", {
            "scope": scope,
            "outcome": "repaired" if was_broken else ("already" if already else "new"),
            "hook_form": "uvx" if guard._hook_command() == guard._UVX_HOOK_CMD else "binary",
        })
        print()
        if already:
            print(f"  {green('✓')} Guard already installed in {path}")
        else:
            print(f"  {green('✓')} Agent cost guardrail installed → {path}")
        print()
        print(f"  {bold('What it does:')} before your agent runs an infra-mutating command")
        print("  (terraform destroy, kubectl delete, aws ec2 terminate-instances, a")
        print("  commitment purchase), nable checks it against your policy:")
        print(f"    one-way door      → {cyan('ask')}   you confirm, with the reason shown")
        print(f"    not in allowlist  → {cyan('deny')}  the agent is told why")
        print("    reversible + safe → silent, zero friction")
        print()
        print(dim("  Policy knobs: FINOPS_POLICY_MAX_AUTO_USD, FINOPS_POLICY_ALLOWED_ACTIONS"))
        print(dim("  Strict mode (also confirm terraform apply / helm upgrade): FINOPS_GUARD_STRICT=1"))
        print(dim(f"  Remove any time: nable guard uninstall{' --global' if global_scope else ''}"))
        print(dim("  Restart Claude Code to pick up the hook."))
        print()
        return

    if action == "uninstall":
        removed = guard.uninstall(global_scope)
        print()
        if removed:
            print(f"  {green('✓')} Guard removed from {scope_label}")
        else:
            print(f"  Guard was not installed in {scope_label}")
        print()
        return

    if action == "check":
        cmd = getattr(parsed, "guard_command", "")
        if not cmd:
            print("\n  Usage: nable guard check --command \"terraform destroy ...\"\n")
            return
        verdict = guard.gate_command(cmd)
        print()
        if verdict is None:
            hit = guard.classify_command(cmd)
            if hit:
                print(f"  {green('allow')}  ({hit[1]}, reversible and in policy) — the guard stays silent")
            else:
                print(f"  {green('allow')}  not an infra-mutating command — the guard stays silent")
        else:
            print(f"  {cyan(verdict['decision'])}  {verdict['reason']}")
        print()
        return

    # status (default)
    print()
    stale = False
    for scope, is_global in (("project", False), ("global", True)):
        p = guard._settings_path(is_global)
        if guard.is_installed(p):
            # "Installed" must mean "will actually run". A hook whose binary has
            # been removed (a deleted venv, a pruned uv cache) fails open in
            # Claude Code, so reporting a plain "installed" here would tell
            # someone they are guarded at the moment they stopped being.
            broken = guard.broken_hook_command(p)
            if broken:
                stale = True
                state = amber("installed, but broken")
            else:
                state = green("installed")
        else:
            state = dim("not installed")
        print(f"  {scope:<8} {state}   {dim(str(p))}")
    print()
    if stale:
        print(f"  {amber('The hooked command no longer exists, so the guard is not running.')}")
        print(dim("  Claude Code skips a hook it cannot execute, silently. Re-run:"))
        print(f"  {cyan('nable guard install')}")
        print()
    print(dim("  Install:  nable guard install            (this project)"))
    print(dim("            nable guard install --global    (all projects)"))
    print(dim("  The hook is Claude Code. Other MCP agents (Cursor, etc.) get the same"))
    print(dim("  gate as a tool: the agent calls check_action_policy before acting."))
    print()


def _run_credits(parsed) -> None:
    """`finops credits` - the box-side half of the hosting-credit purchase flow.

    A Stripe purchase lands as metadata on the customer (webhook); a human or a
    fleet SSM command applies it here with `finops credits grant <usd> --note
    <invoice>`. Additive within the month, use-it-or-lose-it at the roll.
    `budget` sets the recurring monthly allowance instead. `show` prints the
    meter the dashboard uses.
    """
    try:
        from .billing import credits as _credits
    except ImportError:
        print(
            "Managed-AI credits are a hosted/enterprise feature and aren't part of the\n"
            "open-source nable package. See https://getnable.com."
        )
        return
    action = getattr(parsed, "action", "show")
    usd = getattr(parsed, "usd", None)
    if action == "grant":
        if not usd or usd <= 0:
            print("Usage: finops credits grant <usd> [--note <reference>]")
            return
        st = _credits.add_grant(usd, source="cli", note=getattr(parsed, "note", ""))
        print(f"Granted ${usd:,.2f} of managed AI for {st['period']}.")
    elif action == "budget":
        _credits.set_monthly_budget(usd)
        if usd and usd > 0:
            print(f"Monthly managed-AI budget set to ${usd:,.2f}.")
        else:
            print("Monthly managed-AI budget cleared (unmetered).")
    st = _credits.budget_status()
    if not st.get("metered"):
        print("Unmetered: no budget or grants set. Usage is tracked, nothing is blocked.")
        print(f"  spent this period ({st['period']}): ${st['spent']:,.4f} across {st['turns']} turn(s)")
        return
    print(f"Managed AI, {st['period']}:")
    print(f"  total     ${st['total']:,.2f}"
          + (f"  (budget ${st['budget']:,.2f} + grants ${st['grants']:,.2f})" if st.get("grants") else ""))
    print(f"  spent     ${st['spent']:,.2f}  ({st['pct_used']:.0f}% used, {st['turns']} turns)")
    print(f"  remaining ${st['remaining']:,.2f}")
    if st.get("low_balance"):
        print("  ⚠ past 80% of this month's allowance.")
    log_entries = _credits.grant_log()
    if log_entries:
        print("  grants this period:")
        for g in log_entries:
            note = f" ({g['note']})" if g.get("note") else ""
            print(f"    +${g['usd']:,.2f} {g['source']}{note} at {g['at'][:16]}")


def _print_tools_cheatsheet() -> None:
    """Print a concise 'what you can ask nable' cheat-sheet.

    nable exposes 180+ MCP tools; a new user has no way to know what to ask.
    This groups the most useful questions by intent so the first session is
    productive without reading docs.
    """
    from .welcome import bold, cyan, dim

    sections = [
        ("Costs", [
            "What's our cloud spend this month?",
            "Why did our AWS bill go up 40% last month?",
            "Compare our cloud spend vs SaaS spend",
            "What will our bill look like next month?",
        ]),
        ("Waste & savings", [
            "Find all our savings opportunities, ranked by dollar impact",
            "Which EC2 instances should we downsize?",
            "Audit our AWS account for waste",
            "Which workloads are good candidates for Graviton or Spot?",
        ]),
        ("Network & traffic", [
            "How much are we spending on network traffic and where is it going?",
            "What's our internal vs external data transfer cost?",
        ]),
        ("Kubernetes", [
            "Which Kubernetes namespace is over-provisioned?",
            "Show our cluster cost by namespace",
        ]),
        ("AI / LLM", [
            "What's our AI cost per model?",
            "How can we cut our LLM spend without losing quality?",
        ]),
        ("Business context (Pro)", [
            "What's our cost per customer and how is it trending?",
            "What's our infra runway at the current burn?",
        ]),
        ("Anomalies & attribution", [
            "Any unusual cost spikes this week?",
            "Which team is spending the most on Datadog?",
        ]),
    ]
    print()
    print("  " + bold("Ask nable in Claude (or Cursor / Windsurf / VS Code)"))
    print("  " + dim("Just ask. nable picks the right tool. A few to start:"))
    print()
    for title, qs in sections:
        print("  " + cyan(title))
        for q in qs:
            print(f"    \"{q}\"")
        print()
    print("  " + dim("Not connected yet? Run: finops welcome"))
    print("  " + dim("Docs: https://getnable.com/docs"))
    print()


# After connecting a provider, suggest a question relevant to *that* provider, so
# the close is not always "ask about AWS" after connecting something else.
_CONNECT_QUESTIONS = {
    "openai": "What's my OpenAI spend, by model?",
    "anthropic": "What's my Anthropic spend, by model?",
    "openrouter": "What's my OpenRouter spend, by model?",
    "litellm": "What's my LLM spend, by model?",
    "langfuse": "What's my LLM spend, by model?",
    "datadog": "What am I spending on Datadog?",
    "snowflake": "What are my Snowflake credits costing me?",
    "databricks": "What are my Databricks jobs costing?",
    "mongodb": "What's my MongoDB Atlas bill?",
    "twilio": "What am I spending on Twilio?",
    "cloudflare": "What's my Cloudflare bill?",
    "vercel": "What's my Vercel bill?",
    "azure": "What are my Azure costs this month?",
    "gcp": "What are my GCP costs this month?",
    "modal": "What are my Modal GPU costs?",
    "together": "What's my Together AI spend?",
    "replicate": "What's my Replicate spend?",
    "cohere": "What's my Cohere usage costing?",
    "mistral": "What's my Mistral spend?",
    "newrelic": "What am I spending on New Relic?",
}
_CONNECT_CHANNELS = {"slack": "Slack", "teams": "Microsoft Teams", "notion": "Notion", "n8n": "n8n"}


def _post_connect_message(provider: str | None) -> str:
    """The 'done, here's what to do' line, tailored to the provider just connected."""
    p = (provider or "").lower()
    if p in _CONNECT_CHANNELS:
        return f"Done. Restart Claude Desktop, alerts and digests will post to {_CONNECT_CHANNELS[p]}."
    if p == "sso":
        return "Done. Restart Claude Desktop, SSO is wired up for the team dashboard."
    q = _CONNECT_QUESTIONS.get(p, "What's driving my cloud and AI bill?")
    return f'Done. Restart Claude Desktop, then ask:  "{q}"'


def main(args: list[str] | None = None) -> None:
    import sys as _sys
    if args is None:
        args = _sys.argv[1:]

    # Bare `finops` (what `uvx nable` runs) launches the guided welcome flow.
    # `finops setup` / `finops setup aws` keep the explicit provider menu/flow, so
    # strip the leading "setup" token but remember it was not a bare invocation.
    _bare_invocation = not args
    if args and args[0] == "setup":
        args = args[1:]

    # --help / --version must be side-effect-free: argparse handles them by exiting
    # inside parse_args, so skip the welcome banner and PATH warning (and don't burn
    # the one-time welcome event) when they are present.
    _help_or_version = any(a in ("-h", "--help", "--version") for a in args)

    if not _help_or_version:
        from .welcome import show_welcome
        show_welcome()
        # Check PATH early so users know if the command won't be available in new shells
        _check_path_warning()

    # Track setup wizard start, fire-and-forget. A bare _send_event here blocks the
    # main thread on a network POST (up to ~5s on a captive-portal/filtered network)
    # before argparse and first output, reading as a hang. Thread it like
    # record_tool_call does so telemetry never sits in the critical path.
    try:
        from . import telemetry as _tel
        import threading as _th
        _th.Thread(
            target=_tel._send_event,
            args=(
                _tel._get_install_id(),
                "setup_wizard_started",
                {"subcommand": args[0] if args else "interactive"},
            ),
            daemon=True,
        ).start()
    except Exception:
        pass

    import argparse
    import logging
    # Silence noisy third-party loggers during interactive setup. httpx/httpcore
    # are included so the telemetry POSTs don't print "HTTP Request: POST
    # .../capture/" INFO lines into the middle of the interactive prompts.
    for _noisy in ("botocore", "boto3", "apscheduler", "httpx", "httpcore", "urllib3", "posthog"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    class _NableParser(argparse.ArgumentParser):
        """argparse with two DX fixes: grouped --help instead of a 45-command
        wall, and a did-you-mean error instead of dumping every choice."""

        # Ordered groups. A command registered but not listed here auto-renders
        # under "other", so new subcommands can never silently vanish from help.
        _GROUPS = [
            # "get answers" leads: help text is the CLI's homepage, and the
            # commands that produce value outrank the ones that configure it.
            ("get answers", ["scan", "ai-budget"]),
            ("start here", ["welcome", "connect", "doctor", "tools", "serve", "upgrade"]),
            ("clouds", ["aws", "aws-cur", "azure", "gcp"]),
            ("ai / llm providers", ["openai", "anthropic", "openrouter", "litellm",
                                     "modal", "together", "replicate", "cohere", "mistral"]),
            ("saas & observability", ["datadog", "newrelic", "databricks", "snowflake",
                                       "mongodb", "twilio", "cloudflare", "vercel", "langfuse"]),
            ("alerts & reports", ["slack", "teams", "notion", "n8n"]),
            ("editor & agents", ["claude", "guard", "agents"]),
            ("account & billing", ["login", "logout", "license", "license-status", "credits"]),
            ("advanced", ["config", "vault", "profile", "sso", "iam-template", "infra"]),
        ]

        def _sub_action(self):
            for a in self._actions:
                if isinstance(a, argparse._SubParsersAction):
                    return a
            return None

        def format_help(self) -> str:
            sub = self._sub_action()
            if sub is None:
                return super().format_help()
            helps = {c.dest: (c.help or "") for c in sub._choices_actions}
            registered = list(sub.choices.keys())
            lines = [self.format_usage().rstrip(), "", self.description or "", ""]
            seen: set = set()
            for title, names in self._GROUPS:
                rows = [n for n in names if n in registered]
                if not rows:
                    continue
                lines.append(title)
                for n in rows:
                    lines.append(f"  {n:<16} {helps.get(n, '')}")
                    seen.add(n)
                lines.append("")
            leftovers = [n for n in registered if n not in seen]
            if leftovers:
                lines.append("other")
                for n in leftovers:
                    lines.append(f"  {n:<16} {helps.get(n, '')}")
                lines.append("")
            lines += [
                "options",
                "  -h, --help       show this help message and exit",
                "  --version        show the installed nable version",
                "",
                self.epilog or "",
            ]
            return "\n".join(lines)

        def error(self, message: str):
            if "invalid choice" in message:
                import difflib
                import re as _re
                import sys as _sys
                m = _re.search(r"invalid choice: '([^']*)'", message)
                bad = m.group(1) if m else ""
                sub = self._sub_action()
                choices = list(sub.choices.keys()) if sub else []
                close = difflib.get_close_matches(bad, choices, n=3, cutoff=0.6)
                print(f"nable: unknown command '{bad}'", file=_sys.stderr)
                if close:
                    print(f"Did you mean: {', '.join(close)}?", file=_sys.stderr)
                print("Run 'nable --help' to see all commands.", file=_sys.stderr)
                _sys.exit(2)
            super().error(message)

    parser = _NableParser(
        prog="nable",
        description=(
            "nable: find and cut cloud, AI, and SaaS spend from your terminal, across "
            "AWS, Azure, GCP, Kubernetes, and 15+ providers. Local and read-only; also "
            "answers in Claude and Cursor."
        ),
        epilog=(
            "first time? run: nable connect   (finds every provider you already have)\n"
            "then:            nable scan      (recoverable spend, ranked, free)\n"
            "docs: https://getnable.com/docs\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version",
        version=f"nable (finops-mcp) {_installed_version() or 'unknown'}",
    )
    sub = parser.add_subparsers(dest="cmd", metavar="<command>")

    # The value-producing commands register first (module is light: stdlib only).
    from .cli_scan import add_parser as _add_scan_parser
    _add_scan_parser(sub)
    from .cli_ai_budget import add_parser as _add_ai_budget_parser
    _add_ai_budget_parser(sub)

    aws_p = sub.add_parser("aws",          help="Connect AWS (Cost Explorer, CloudWatch)")
    aws_p.add_argument("--org",          action="store_true", help="Auto-discover accounts from AWS Organizations")
    aws_p.add_argument("--add",          action="store_true", help="Add another AWS account (skips intro)")
    aws_p.add_argument("--list",         action="store_true", help="List all configured AWS accounts")
    aws_p.add_argument("--iam-template", action="store_true", help="Print least-privilege IAM CloudFormation template")
    aws_p.add_argument("--iam-terraform",action="store_true", help="Print least-privilege IAM Terraform snippet")
    aws_p.add_argument("--launch-stack", action="store_true", help="Print the one-click link that mints a read-only access key")
    aws_p.add_argument("--check-scope",  action="store_true", help="Verify your AWS key is read-only")
    sub.add_parser("azure",        help="Connect Azure Cost Management")
    sub.add_parser("gcp",          help="Connect GCP Cloud Billing / BigQuery export")
    sub.add_parser("datadog",      help="Connect Datadog usage and cost API")
    sub.add_parser("langfuse",     help="Connect Langfuse LLM observability costs")
    sub.add_parser("snowflake",    help="Connect Snowflake credit consumption")
    sub.add_parser("mongodb",      help="Connect MongoDB Atlas invoice API")
    sub.add_parser("twilio",       help="Connect Twilio usage records")
    sub.add_parser("cloudflare",   help="Connect Cloudflare billing and subscriptions")
    sub.add_parser("vercel",       help="Connect Vercel invoice API (Enterprise only)")
    sub.add_parser("slack",        help="Configure Slack anomaly alerts and digest")
    sub.add_parser("teams",        help="Configure Microsoft Teams alerts")
    sub.add_parser("notion",       help="Configure Notion cost report publishing")
    sub.add_parser("n8n",          help="Configure n8n workflow automation webhook")
    sub.add_parser("sso",          help="Configure enterprise SSO (OIDC / Okta / Azure AD)")
    sub.add_parser("openai",       help="Connect OpenAI usage and billing API")
    sub.add_parser("anthropic",    help="Connect Anthropic usage API")
    sub.add_parser("openrouter",   help="Connect OpenRouter gateway usage")
    sub.add_parser("litellm",      help="Connect a self-hosted LiteLLM proxy")
    sub.add_parser("modal",        help="Connect Modal serverless-GPU account")
    sub.add_parser("together",     help="Connect Together AI account")
    sub.add_parser("replicate",    help="Connect Replicate account")
    sub.add_parser("cohere",       help="Connect Cohere API usage")
    sub.add_parser("mistral",      help="Connect Mistral AI API usage")
    sub.add_parser("newrelic",     help="Connect New Relic data ingest and seat costs")
    sub.add_parser("databricks",   help="Connect Databricks DBU consumption and job costs")
    sub.add_parser("claude",       help="Register nable in Claude Desktop config")
    up_p = sub.add_parser("upgrade", help="Upgrade nable: the finops CLI, plus the editor/Claude Desktop MCP pin")
    up_p.add_argument("version", nargs="?", default="", help="Target version (default: latest on PyPI)")
    cr_p = sub.add_parser("credits", help="Managed-AI credit balance for this instance (show / grant / budget)")
    cr_p.add_argument("action", nargs="?", default="show", choices=["show", "grant", "budget"],
                      help="show = balance, grant = add a purchased top-up, budget = set the monthly allowance")
    cr_p.add_argument("usd", nargs="?", type=float, default=None, help="Dollar amount for grant/budget")
    cr_p.add_argument("--note", default="", help="Reference for a grant (e.g. Stripe invoice id)")
    sub.add_parser("aws-cur",      help="Deploy AWS CUR pipeline via CloudFormation")
    config_p = sub.add_parser("config", help="Configure user preferences (persona, etc.)")
    config_p.add_argument("--persona", metavar="ROLE", default="",
                          help="Set response persona: engineer, finops, finance, platform")

    lic_p = sub.add_parser("license",       help="Activate a Pro license key (FINOPS-2-...)")
    lic_p.add_argument("key", nargs="?", default="", help="License key (FINOPS-2-...)")
    login_p = sub.add_parser("login",       help="Sign in by email to activate Pro (no license key to copy)")
    login_p.add_argument("email", nargs="?", default="", help="Account email (optional; prompts if omitted)")
    sub.add_parser("logout",                help="Sign out and remove the stored license from this machine")
    sub.add_parser("license-status",        help="Check current license plan and expiry")
    infra_p = sub.add_parser("infra",       help="Show connector setup overview or provider guide")
    infra_p.add_argument("provider", nargs="?", default="", help="Show setup for a specific provider")

    serve_p = sub.add_parser(
        "serve",
        help="Start a local web dashboard your whole team can view in a browser",
        description=(
            "Start the team dashboard. On an always-on host this also runs the "
            "finance interfaces non-engineers consume:\n"
            "  - Scheduler (pushed snapshots, anomaly alerts, daily + weekly digests) "
            "when FINOPS_ENABLE_SCHEDULER=1.\n"
            "  - Slack bot (two-way cost Q&A) when SLACK_BOT_TOKEN and SLACK_APP_TOKEN "
            "are both set.\n"
            "Both stay off on a plain laptop run. The dashboard requires a password by "
            "default (auto-generated and printed once; set FINOPS_DASHBOARD_PASSWORD to "
            "pin one, or =off to disable). See DEPLOY.md."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    serve_p.add_argument("--port", type=int, default=8080, help="Port to listen on (default: 8080)")
    serve_p.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1, local only; use 0.0.0.0 to let your team reach it on the LAN)")
    serve_p.add_argument("--open", action="store_true", help="Open browser on start")
    serve_p.add_argument("--demo", action="store_true",
                         help="Serve a populated sample dashboard, no cloud account needed "
                              "(demo data only, auth off, opens your browser)")

    sub.add_parser("connect", help="Scan this machine for provider credentials and connect them all in one keystroke")
    sub.add_parser("agents",  help="The agent team: Budget Guard, Savings Analyst, the Ledger, and their setup status")
    welcome_p = sub.add_parser("welcome", help="Guided onboarding: connect your first provider (and your editor)")
    welcome_p.add_argument("--demo", action="store_true", help="Show nable on sample data, no account needed")
    sub.add_parser("doctor",       help="Check all connectors and credentials (alias for finops-doctor)")
    sub.add_parser("tools",        help="Show example questions you can ask nable in Claude")

    guard_p = sub.add_parser("guard", help="Agent cost guardrail: auto-check infra commands against your policy")
    guard_p.add_argument("guard_action", choices=["install", "uninstall", "status", "hook", "check"],
                         nargs="?", default="status")
    guard_p.add_argument("--global", dest="guard_global", action="store_true",
                         help="Install into ~/.claude/settings.json instead of this project")
    guard_p.add_argument("--command", dest="guard_command", default="",
                         help="With 'check': a shell command to classify against your policy")

    iam_p = sub.add_parser("iam-template", help="Print the least-privilege IAM policy / CloudFormation nable needs")
    iam_p.add_argument("action", choices=["terraform", "cloudformation"], nargs="?", default="cloudformation")

    vault_p = sub.add_parser("vault", help="Inspect the local credential vault (list, audit)")
    vault_p.add_argument("action", choices=["list", "delete", "rotate"])
    vault_p.add_argument("key", nargs="?", default="")

    profile_p = sub.add_parser("profile", help="Manage named profiles for multi-account use")
    profile_p.add_argument("profile_action", choices=["list", "create", "use", "current"], nargs="?", default="list")
    profile_p.add_argument("profile_name", nargs="?", default="")

    parsed = parser.parse_args(args)
    # Ensure optional attrs exist for all subparsers (only `vault` defines `action`/`key`)
    if not hasattr(parsed, "action"):
        parsed.action = None
    if not hasattr(parsed, "key"):
        parsed.key = ""

    # The guard hook is a machine protocol: Claude Code parses this process's
    # stdout as JSON on every Bash call, so it must run before any banner.
    if parsed.cmd == "guard" and getattr(parsed, "guard_action", "") == "hook":
        from .guard import run_hook
        raise SystemExit(run_hook())

    # Answer commands own their whole output: no setup banner ahead of `scan`,
    # its branded first line must be the first thing on screen (and in --json
    # mode stdout must stay a single parseable document).
    if parsed.cmd != "scan":
        print("\n  nable setup: all credentials stay on your machine\n")

    dispatch = {
        "aws": setup_aws_account,
        "azure": setup_azure,
        "gcp": setup_gcp,
        "slack": setup_slack,
        "teams": setup_teams,
        "notion": lambda: setup_saas_api_key("Notion", [
            ("NOTION_API_KEY", "Integration Token (from notion.so/my-integrations)", True),
            ("NOTION_PAGE_ID", "Target Page ID (from the page URL)", False),
        ]),
        "n8n": setup_n8n,
        "sso": setup_sso,
        "datadog": lambda: setup_saas_api_key("Datadog", [
            ("DATADOG_API_KEY", "API Key", True),
            ("DATADOG_APP_KEY", "Application Key", True),
            ("DATADOG_SITE", "Site (datadoghq.com or datadoghq.eu)", False),
        ]),
        "langfuse": lambda: setup_saas_api_key("Langfuse", [
            ("LANGFUSE_PUBLIC_KEY", "Public Key (pk-lf-...)", True),
            ("LANGFUSE_SECRET_KEY", "Secret Key (sk-lf-...)", True),
            ("LANGFUSE_HOST", "Host URL (leave blank for cloud.langfuse.com)", False),
        ]),
        "snowflake": lambda: setup_saas_api_key("Snowflake", [
            ("SNOWFLAKE_ACCOUNT", "Account identifier (e.g. xy12345.us-east-1)", False),
            ("SNOWFLAKE_USER", "Username", False),
            ("SNOWFLAKE_PASSWORD", "Password", True),
            ("SNOWFLAKE_WAREHOUSE", "Warehouse name (e.g. COMPUTE_WH)", False),
            ("SNOWFLAKE_ROLE", "Role (default: ACCOUNTADMIN)", False),
            ("SNOWFLAKE_CREDIT_PRICE", "Credit price USD (your contract rate, optional)", False),
        ]),
        "mongodb": lambda: setup_saas_api_key("MongoDB Atlas", [
            ("MONGODB_ATLAS_PUBLIC_KEY", "Public Key", False),
            ("MONGODB_ATLAS_PRIVATE_KEY", "Private Key", True),
            ("MONGODB_ATLAS_ORG_IDS", "Organization IDs (comma-separated)", False),
        ]),
        "twilio": lambda: setup_saas_api_key("Twilio", [
            ("TWILIO_ACCOUNT_SID", "Account SID (ACxxxx...)", False),
            ("TWILIO_AUTH_TOKEN", "Auth Token", True),
        ]),
        "cloudflare": lambda: setup_saas_api_key("Cloudflare", [
            ("CLOUDFLARE_API_TOKEN", "API Token", True),
            ("CLOUDFLARE_ACCOUNT_ID", "Account ID", False),
        ]),
        "vercel": lambda: setup_saas_api_key("Vercel", [
            ("VERCEL_TOKEN", "Access Token", True),
            ("VERCEL_TEAM_ID", "Team ID (optional, blank for personal)", False),
        ]),
        # ── AI / LLM providers ────────────────────────────────────────────────
        "openai": lambda: setup_saas_api_key("OpenAI", [
            ("OPENAI_API_KEY", "API Key (sk-...)", True),
            ("OPENAI_ADMIN_KEY", "Admin/Org Key for billing data (sk-admin-..., optional)", True),
            ("OPENAI_ORG_ID", "Organization ID (org-..., optional)", False),
        ]),
        "anthropic": lambda: setup_saas_api_key("Anthropic", [
            ("ANTHROPIC_API_KEY", "API Key (sk-ant-...)", True),
            ("ANTHROPIC_ADMIN_KEY", "Admin Key (sk-ant-admin-…) for actual billed costs via the Cost API + org usage (optional, recommended)", True),
            ("ANTHROPIC_ORGANIZATION_ID", "Organization ID (optional)", False),
        ]),
        "openrouter": lambda: setup_saas_api_key("OpenRouter", [
            ("OPENROUTER_API_KEY", "API Key (sk-or-...)", True),
            ("OPENROUTER_PROVISIONING_KEY", "Provisioning key for per-model usage (optional, recommended)", True),
        ]),
        "litellm": lambda: setup_saas_api_key("LiteLLM proxy", [
            ("LITELLM_PROXY_URL", "Proxy base URL (e.g. http://localhost:4000)", False),
            ("LITELLM_MASTER_KEY", "Master/admin key (sk-...)", True),
        ]),
        "modal": lambda: setup_saas_api_key("Modal", [
            ("MODAL_TOKEN_ID", "Token ID (ak-...)", True),
            ("MODAL_TOKEN_SECRET", "Token secret (as-...)", True),
        ]),
        "together": lambda: setup_saas_api_key("Together AI", [
            ("TOGETHER_API_KEY", "API Key", True),
        ]),
        "replicate": lambda: setup_saas_api_key("Replicate", [
            ("REPLICATE_API_TOKEN", "API Token (r8_...)", True),
        ]),
        "cohere": lambda: setup_saas_api_key("Cohere", [
            ("COHERE_API_KEY", "API Key", True),
        ]),
        "mistral": lambda: setup_saas_api_key("Mistral AI", [
            ("MISTRAL_API_KEY", "API Key", True),
        ]),
        "newrelic": lambda: setup_saas_api_key("New Relic", [
            ("NEW_RELIC_API_KEY", "API Key (NRAK-...)", True),
            ("NEW_RELIC_ACCOUNT_ID", "Account ID", False),
            ("NEW_RELIC_INGEST_PRICE_PER_GB", "Data ingest price USD/GB (your contract rate, optional, for $ amounts)", False),
            ("NEW_RELIC_FULL_PLATFORM_PRICE", "Full-platform user price USD/user/mo (optional, for $ amounts)", False),
        ], note="New Relic reports usage (GB ingested, user counts). Add your "
                "contract rates below and nable turns them into dollars; leave blank "
                "to see usage only."),
        "databricks": lambda: setup_saas_api_key("Databricks", [
            ("DATABRICKS_HOST", "Workspace URL (e.g. https://adb-1234567890.1.azuredatabricks.net)", False),
            ("DATABRICKS_TOKEN", "Personal Access Token or Service Principal token", True),
            ("DATABRICKS_ACCOUNT_ID", "Account ID for billing API (optional, leave blank for single-workspace)", False),
            ("DATABRICKS_ACCOUNT_TOKEN", "Account-level token (optional, defaults to DATABRICKS_TOKEN)", True),
            ("DATABRICKS_DBU_PRICE", "DBU price in USD (optional, default 0.40, use your contract rate)", False),
        ]),
    }

    if parsed.cmd == "config":
        _handle_config_cmd(parsed)
        return

    if parsed.cmd == "profile":
        _handle_profile_cmd(parsed)
        return

    if parsed.cmd == "vault":
        if parsed.action == "list":
            vault_list()
        elif parsed.action == "delete":
            if not parsed.key:
                _err("Specify a key name: finops setup vault delete KEY")
            else:
                vault_delete(parsed.key)
        elif parsed.action == "rotate":
            vault_rotate()
        return
    elif parsed.cmd == "iam-template":
        # Standalone alias: finops setup iam-template
        from .security.iam_setup import print_iam_template
        fmt = "terraform" if (parsed.action == "terraform") else "cloudformation"
        print_iam_template(fmt)
        return
    elif parsed.cmd == "aws" and getattr(parsed, "iam_template", False):
        from .security.iam_setup import print_iam_template
        print_iam_template("cloudformation")
        return
    elif parsed.cmd == "aws" and getattr(parsed, "iam_terraform", False):
        from .security.iam_setup import print_iam_template
        print_iam_template("terraform")
        return
    elif parsed.cmd == "aws" and getattr(parsed, "launch_stack", False):
        from .security.iam_setup import quick_create_url
        print(
            "\n  One-click: open this link, click Create stack, then copy\n"
            "  AccessKeyId and SecretAccessKey from the Outputs tab into\n"
            "  'finops setup aws'. The stack is read-only and auditable.\n"
        )
        print(f"  {quick_create_url()}\n")
        return
    elif parsed.cmd == "aws" and getattr(parsed, "check_scope", False):
        from .security.iam_setup import check_credential_scope
        print("\n  Checking AWS credential scope...\n")
        result = check_credential_scope()
        if "error" in result:
            _err(result["error"])
            return
        print(f"  Account:      {result['account_id']}")
        print(f"  Identity:     {result['identity_arn']}")
        print(f"  Correctly scoped: {'✓ Yes' if result['scoped_correctly'] else '✗ No'}")
        if result.get("required_denied"):
            print(f"\n  Missing permissions ({len(result['required_denied'])}):")
            for a in result["required_denied"]:
                print(f"    ✗ {a}")
        if result.get("dangerous_allowed"):
            print("\n  ⚠ Over-provisioned: write permissions detected:")
            for a in result["dangerous_allowed"]:
                print(f"    ⚠ {a}")
            print()
            print("  Run `finops setup aws --iam-template` to generate a scoped policy.")
        elif not result.get("required_denied"):
            print("\n  ✓ Credentials are read-only and correctly scoped for nable.")
        print()
        return
    elif parsed.cmd == "claude":
        _configure_claude_desktop()
        return
    elif parsed.cmd == "upgrade":
        _run_upgrade(getattr(parsed, "version", ""))
        return
    elif parsed.cmd == "aws-cur":
        _run_aws_cur_setup()
        return
    elif parsed.cmd == "license":
        _run_license_setup(getattr(parsed, "key", ""))
        return
    elif parsed.cmd == "login":
        _run_login(getattr(parsed, "email", ""))
        return
    elif parsed.cmd == "logout":
        _run_logout()
        return
    elif parsed.cmd == "license-status":
        _run_license_status()
        return
    elif parsed.cmd == "serve":
        if getattr(parsed, "demo", False):
            # `uvx nable serve --demo`: the one-command populated dashboard.
            # FORCE keeps it demo even on a machine with a real account connected
            # (the flag promises sample data, and it doubles as the demo-recording
            # path). Env must be set before the server import below; demo_data may
            # already be imported with the flag unset, so pin its module state too.
            os.environ["FINOPS_DEMO_MODE"] = "1"
            os.environ["FINOPS_DEMO_FORCE"] = "1"
            os.environ.setdefault("FINOPS_DASHBOARD_PASSWORD", "off")
            from . import demo_data as _dd
            _dd.DEMO_MODE = True
        try:
            from .server_web import run_server, set_connectors
        except ImportError:
            print(
                "The local web dashboard has been removed from the open-source nable package.\n"
                "It's now a hosted/enterprise feature. The local product is the MCP server that\n"
                "runs inside your editor (Claude, Cursor, etc.). For a hosted dashboard, see\n"
                "https://getnable.com."
            )
            return
        # Importing the MCP server module hydrates vault/keyring credentials into
        # the environment (load_vault_to_env runs at its import) and exposes the
        # canonical connector set. Without this the serve process ran with an empty
        # environment, so Azure, GCP, and SaaS providers (which read credentials
        # from env) showed $0 or disconnected on the dashboard even when set up.
        from .server import _CLOUD_CONNECTORS, _SAAS_CONNECTORS
        set_connectors({**_CLOUD_CONNECTORS, **_SAAS_CONNECTORS})
        run_server(
            host=getattr(parsed, "host", "127.0.0.1"),
            port=getattr(parsed, "port", 8080),
            open_browser=getattr(parsed, "open", False) or getattr(parsed, "demo", False),
        )
        return
    elif parsed.cmd == "scan":
        from .cli_scan import run as _scan_run
        raise SystemExit(_scan_run(parsed))
    elif parsed.cmd == "ai-budget":
        from .cli_ai_budget import run as _ab_run
        raise SystemExit(_ab_run(parsed))
    elif parsed.cmd == "welcome":
        from .welcome import run_welcome_flow
        run_welcome_flow(demo=getattr(parsed, "demo", False))
        return
    elif parsed.cmd == "connect":
        from .setup_scan import run_connect_command
        run_connect_command()
        return
    elif parsed.cmd == "credits":
        _run_credits(parsed)
        return
    elif parsed.cmd == "tools":
        _print_tools_cheatsheet()
        return
    elif parsed.cmd == "agents":
        _run_agents()
        return
    elif parsed.cmd == "guard":
        _run_guard(parsed)
        return
    elif parsed.cmd == "doctor":
        from .doctor import main as _doctor_main
        # Pass empty args: doctor.main() defaults to parsing sys.argv, which still
        # contains the consumed "doctor" token and would error. For flags
        # (--json/--audit), use the standalone `finops-doctor` command.
        _doctor_main([])
        return
    elif parsed.cmd == "infra":
        _run_infra_overview(getattr(parsed, "provider", ""))
        return
    elif parsed.cmd == "aws":
        if getattr(parsed, "list", False):
            _print_aws_accounts()
        elif getattr(parsed, "org", False):
            setup_aws_org()
        elif getattr(parsed, "add", False):
            setup_aws_account()
        else:
            setup_aws_account()
        return
    elif parsed.cmd in dispatch:
        dispatch[parsed.cmd]()
    else:
        # Bare `finops` / `uvx nable`: launch the guided welcome flow (auto-wire
        # the editor, ambient-credential scan, value moment, never dead-ends), so
        # first-run value is never gated behind a persona quiz + 26-item menu.
        if _bare_invocation:
            from .welcome import run_welcome_flow
            run_welcome_flow()
            return

        # Explicit `finops setup`: full interactive provider menu for power users.
        _wizard_select_persona()

        providers = ["aws", "azure", "gcp", "openai", "anthropic", "openrouter", "litellm", "modal", "together", "replicate", "datadog", "langfuse", "snowflake", "mongodb", "twilio", "cloudflare", "vercel", "cohere", "mistral", "newrelic", "databricks", "slack", "teams", "notion", "n8n"]
        print("  Which providers would you like to configure?")
        for i, p in enumerate(providers, 1):
            print(f"  {i:2d}) {p}")
        raw = _prompt("\n  Enter numbers (comma-separated), 'all', or press Enter for aws only", default="1")
        if raw.lower() == "all":
            selected = providers
        else:
            indices = [int(x.strip()) - 1 for x in raw.split(",") if x.strip().isdigit()]
            selected = [providers[i] for i in indices if 0 <= i < len(providers)]
        for p in selected:
            try:
                dispatch[p]()
            except KeyboardInterrupt:
                print("\n  Skipped.")

    # Always offer to configure Claude Desktop at the end of setup
    _configure_claude_desktop()

    from .welcome import _cli
    print("\n  " + _post_connect_message(parsed.cmd))
    print()
    print("  Want a visual dashboard?")
    print(f"    {_cli('serve')}")
    print("    → Serves a web dashboard at http://localhost:8080, add --open to launch your browser")
    print("    → To let your team or manager view it, add --host 0.0.0.0 (still password-protected)")
    print()
    print(f"  To add more providers: {_cli('setup')}")
    print("  Full docs: https://getnable.com/docs\n")
    _offer_email_signup()

    # Fire setup_completed event
    try:
        from . import telemetry as _tel
        _tel._send_event(_tel._get_install_id(), "setup_completed", {
            "subcommand": args[0] if args else "interactive",
        })
    except Exception:
        pass


# ── Post-setup email capture ──────────────────────────────────────────────────

def _offer_email_signup() -> None:
    """
    Offer a free weekly cost digest and capture email for follow-up.
    Non-blocking: any error is silently skipped.
    """
    # Air-gap mode forbids all non-provider egress. This step POSTs to
    # getnable.com, so under air-gap do not even prompt.
    try:
        from .config import is_airgap
        if is_airgap():
            return
    except Exception:
        pass
    # Skip if already captured in this session or previously declined
    sentinel = Path.home() / ".config" / "finops" / ".email_captured"
    if sentinel.exists():
        return

    print("─" * 60)
    print("  Want the quickstart guide sent to your inbox?")
    print("  Your email will be sent to getnable.com to deliver the guide.")
    print()
    try:
        email = input("  Your email (Enter to skip): ").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return

    if not email or "@" not in email:
        # User skipped, mark so we don't ask again this install
        try:
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text("skipped\n")
        except Exception:
            pass
        return

    try:
        import urllib.request
        import json as _json
        req = urllib.request.Request(
            "https://getnable.com/api/subscribe",
            data=_json.dumps({"email": email, "source": "setup_wizard"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
        _ok("Guide sent. Check your inbox.")
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(f"{email}\n")
    except Exception:
        # Don't block setup if the request fails, but don't write the sentinel
        # so the user is re-prompted next time (their email was never recorded)
        _ok("Got it. We'll follow up soon.")

    print()


# ── Claude Desktop auto-configuration ─────────────────────────────────────────

def _run_license_setup(key: str = "") -> None:
    """
    Activate a Pro license key.
    Called by: finops setup license FINOPS-2-xxx
                finops setup license   (interactive, prompts for key)
    """
    from .license import validate_key, _UPGRADE_URL, _CHECKOUT_URL
    from .security.vault import Vault

    print("\n  nable Pro license activation\n")

    # If key not passed as arg, prompt
    if not key:
        print(f"  Subscribe at: {_CHECKOUT_URL}")
        print("  After checkout your license key is shown on the confirmation page")
        print("  and emailed to you. It starts with FINOPS-2-\n")
        key = _prompt("  Paste your license key").strip()

    if not key:
        _warn("No key entered. Run 'finops setup license FINOPS-2-...' to activate.")
        return

    # Validate before storing
    status = validate_key(key)

    if status.mode == "invalid":
        _err(f"Invalid key: {status.message}")
        print(f"\n  Subscribe at: {_CHECKOUT_URL}\n")
        return

    if status.mode not in ("pro", "trial"):
        _warn(f"Key validated but returned unexpected plan: {status.mode}")

    # Store in vault AND write to env file for Claude Desktop
    vault = Vault.default()
    vault.store("FINOPS_LICENSE_KEY", key)

    # Also try to write directly into the Claude Desktop config
    _inject_license_into_claude_config(key)

    print(f"\n  ✓  Pro plan active, {status.email or 'license validated'}")
    print("  ✓  Key stored in vault.")
    print(f"  ✓  Plan: {status.mode.upper()}")
    if status.issued:
        print(f"  ✓  Issued: {status.issued}")
    print()
    print("  Restart Claude Desktop to activate Team features:")
    print("    • Ticket auto-creation (Jira, Linear, GitHub Issues)")
    print("    • Scheduled email reports")
    print("    • Commitment purchase recommendations")
    print("    • Org-wide multi-account rollup")
    print("    • Business metrics and unit economics")
    print()

    try:
        from . import telemetry as _tel
        _tel._send_event(_tel._get_install_id(), "license_activated", {
            "plan": status.mode,
            "has_email": bool(status.email),
        })
    except Exception:
        pass


def _run_license_status() -> None:
    """
    Print current license status without restarting anything.
    Called by: finops setup license-status
               finops license-status
    """
    from .license import check_license, _UPGRADE_URL

    status = check_license()

    print("\n  nable license status\n")

    mode_display = {
        "pro":     "\033[32mTeam (Pro)\033[0m",
        "trial":   "\033[33mTrial\033[0m",
        "free":    "\033[90mFree\033[0m",
        "invalid": "\033[31mInvalid\033[0m",
    }.get(status.mode, status.mode)

    print(f"  Plan:    {mode_display}")
    if status.email:
        print(f"  Email:   {status.email}")
    if status.issued:
        print(f"  Issued:  {status.issued}")
    if status.days_remaining >= 0:
        print(f"  Trial:   {status.days_remaining} day(s) remaining")
    print(f"  Message: {status.message}")

    if status.mode == "free":
        print(f"\n  Upgrade at: {_UPGRADE_URL}")
        print("  Then run:   finops setup license FINOPS-2-...\n")
    elif status.mode == "trial":
        print(f"\n  Upgrade before trial ends: {_UPGRADE_URL}\n")
    else:
        print()


def _run_login(email: str = "") -> None:
    """
    Activate Pro by email, with no license key to copy or remember. Sends an
    8-digit code to your inbox, verifies it, and stores the license locally so
    the server picks it up automatically.
    Called by: finops login [email]
    """
    import json
    import urllib.request
    import urllib.error
    from .license import store_license, _UPGRADE_URL

    base = os.environ.get("FINOPS_ACCOUNT_BASE", "https://getnable.com").rstrip("/")

    print("\n  Sign in to nable\n")
    email = (email or "").strip().lower()
    if not email:
        email = _prompt("  Email you used at checkout").strip().lower()
    if "@" not in email or "." not in email:
        _err("That does not look like an email address.")
        return

    def _post(path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{base}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8") or "{}"
        return json.loads(raw)

    # 1) request a code (always returns ok, by design, to avoid email enumeration)
    print("  Sending your code…", flush=True)
    try:
        _post("/api/account/send-code", {"email": email})
    except urllib.error.HTTPError as e:  # 429 / 5xx from the server, not a network fault
        msg = ""
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:
            pass
        if e.code == 429:
            _err("Too many requests. Wait a few minutes, then try again.")
        else:
            _err(msg or f"Could not send the code ({e.code}).")
        return
    except Exception as e:  # genuine network / DNS / timeout
        _err(f"Could not reach getnable.com. Check your connection and try again. ({e})")
        return
    print(f"  ✓  Sent an 8-digit code to {email}. It expires in 10 minutes.\n")

    # 2) verify the code -> the server returns the license for this email
    code = _prompt("  Enter the code").strip()
    if not code:
        _warn("No code entered. Run 'finops login' again when it arrives.")
        return
    try:
        data = _post("/api/account/verify-code", {"email": email, "code": code})
    except urllib.error.HTTPError as e:
        msg = ""
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:
            pass
        if e.code == 429:
            _err("Too many attempts. Wait a few minutes, then try again.")
        else:
            _err(msg or f"Sign-in failed ({e.code}).")
        return
    except Exception as e:
        _err(f"Could not verify the code: {e}")
        return

    plan = (data or {}).get("plan", "free")
    key = (data or {}).get("license_key") or ""

    if not key or plan == "free":
        _warn(f"No active subscription found for {email}.")
        print(f"\n  Get Pro at: {_UPGRADE_URL}")
        print("  Subscribed with a different email? Run 'finops login' with that one.\n")
        return

    status = store_license(key)
    if status.mode == "invalid":
        _err(f"The license we received did not validate: {status.message}")
        return
    _inject_license_into_claude_config(key)

    print(f"\n  ✓  Signed in as {status.email or email}")
    print(f"  ✓  Plan: {status.mode.upper()}")
    print("  ✓  License stored on this machine. Nothing to copy or remember.")
    print()
    print("  Restart Claude Desktop (or your MCP client) to pick up Pro.")
    print()

    try:
        from . import telemetry as _tel
        _tel._send_event(_tel._get_install_id(), "login_activated", {"plan": status.mode})
    except Exception:
        pass


def _run_logout() -> None:
    """Remove the stored license from this machine. Called by: finops logout"""
    from .license import clear_license
    clear_license()
    print("\n  ✓  Signed out. Pro features are off on this machine.")
    print("  Sign back in any time with: finops login\n")


def _inject_license_into_claude_config(key: str) -> None:
    """
    Try to write FINOPS_LICENSE_KEY directly into claude_desktop_config.json
    so the user doesn't have to manually edit it.
    """
    import json
    import platform

    if platform.system() == "Darwin":
        config_path = Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    elif platform.system() == "Windows":
        config_path = Path(os.environ.get("APPDATA", "")) / "Claude" / "claude_desktop_config.json"
    else:
        config_path = Path.home() / ".config" / "Claude" / "claude_desktop_config.json"

    try:
        if not config_path.exists():
            return

        config = json.loads(config_path.read_text())
        servers = config.get("mcpServers", {})

        updated = False
        for server_name, server_cfg in servers.items():
            if "finops" in server_name.lower() or "nable" in server_name.lower():
                env = server_cfg.setdefault("env", {})
                env["FINOPS_LICENSE_KEY"] = key
                updated = True

        if updated:
            config_path.write_text(json.dumps(config, indent=2))
            config_path.chmod(0o600)
            print("  ✓  Written to Claude Desktop config automatically.")
        else:
            print("  →  Add to your Claude Desktop config manually:")
            print(f'       "FINOPS_LICENSE_KEY": "{key}"')
            _warn("This is your license key. Keep it private: not in screen-shares or public gists.")
    except Exception:
        print("  →  Add to your Claude Desktop config manually:")
        print(f'       "FINOPS_LICENSE_KEY": "{key}"')
        _warn("This is your license key. Keep it private: not in screen-shares or public gists.")


def _inject_aws_into_claude_config(access_key: str, secret_key: str, region: str) -> None:
    """
    Write AWS credentials into the finops MCP server's env block in
    claude_desktop_config.json so the server subprocess inherits them.
    Safe to call multiple times; existing keys are overwritten, others left alone.
    """
    import json
    import platform

    if platform.system() == "Darwin":
        config_path = Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    elif platform.system() == "Windows":
        config_path = Path(os.environ.get("APPDATA", "")) / "Claude" / "claude_desktop_config.json"
    else:
        config_path = Path.home() / ".config" / "Claude" / "claude_desktop_config.json"

    if not config_path.exists():
        # Claude Desktop not yet configured. The credentials are in the vault;
        # _configure_claude_desktop_inner will inject them when it runs.
        print("\n  (Claude Desktop config not found yet. Run 'finops setup claude' to register")
        print("   nable and the credentials will be included automatically.)")
        return

    try:
        try:
            config = json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError) as parse_err:
            _warn(f"Could not parse Claude Desktop config: {parse_err}")
            _warn(f"Check {config_path} manually before re-running setup.")
            return

        servers = config.get("mcpServers", {})
        updated_server = None
        for server_name, server_cfg in servers.items():
            if "finops" in server_name.lower() or "nable" in server_name.lower():
                env = server_cfg.setdefault("env", {})
                env["AWS_DEFAULT_REGION"] = region
                updated_server = server_name

        if updated_server:
            config_path.write_text(json.dumps(config, indent=2) + "\n")
            config_path.chmod(0o600)
            _ok(f"AWS region written to Claude Desktop config ({updated_server}).")
            print("  Restart Claude Desktop to apply.")
        else:
            print("\n  nable is not yet in Claude Desktop config.")
            print("  Run 'finops setup claude' to register it.")
    except Exception as e:
        _warn(f"Could not update Claude Desktop config: {e}")
        print("  Credentials are saved in the vault and will load at MCP server startup.")


def _run_aws_cur_setup() -> None:
    """
    Interactive AWS CUR CloudFormation deployment.
    Called by: finops setup aws-cur
    """
    from .setup.cloud_infra import setup_aws_cur
    from .security.vault import Vault

    env_vars = setup_aws_cur()
    if not env_vars:
        return

    # Offer to store the outputs in the nable vault
    store = _prompt(
        "\n  Store these in the nable credential vault? [Y/n]", default="y"
    ).lower()
    if store in ("y", "yes", ""):
        vault = Vault.default()
        for key, val in env_vars.items():
            if val:
                vault.store(key, val)
        print("\n  ✓  Stored in vault. CUR line-item queries are now available.")
        print("  ✓  Restart Claude Desktop to pick up the new env vars.\n")

        try:
            from . import telemetry as _tel
            _tel._send_event(_tel._get_install_id(), "provider_connected", {
                "provider": "aws_cur",
                "via": "cloudformation",
            })
        except Exception:
            pass
    else:
        print("\n  Add the env vars above to your Claude Desktop config or .env manually.\n")


def _run_infra_overview(provider: str = "") -> None:
    """
    Show the connector registry overview or a specific provider's setup guide.
    Called by: finops setup infra [provider]
    """
    from .setup.cloud_infra import (
        print_connector_overview,
        print_saas_setup,
        AzureSetupPackage,
        GCPSetupPackage,
        SnowflakeSetupPackage,
    )

    if not provider:
        print_connector_overview()
        print(
            "  Run 'finops setup infra <provider>' for detailed setup steps.\n"
            "  Run 'finops setup aws-cur' to deploy the CUR pipeline interactively.\n"
        )
        return

    p = provider.lower()
    if p == "aws" or p == "aws-cur":
        print(
            "\n  AWS uses your existing credential chain (IAM role, ~/.aws/credentials, env vars).\n"
            "  For line-item CUR detail, run: finops setup aws-cur\n"
        )
    elif p == "azure":
        sub_id = _prompt("  Azure Subscription ID (leave blank to skip validation)")
        AzureSetupPackage().print_setup_commands(sub_id or "<your-subscription-id>")
    elif p == "gcp":
        project = _prompt("  GCP Project ID")
        billing = _prompt("  Billing Account ID (format: XXXXXX-XXXXXX-XXXXXX)")
        GCPSetupPackage().print_setup_commands(
            project or "<project-id>",
            billing or "<billing-account-id>",
        )
    elif p == "snowflake":
        account = _prompt("  Snowflake account identifier (e.g. xy12345.us-east-1)")
        SnowflakeSetupPackage().print_setup_sql(account or "<account>")
    else:
        print_saas_setup(p)


def _installed_version() -> str:
    # finops.__version__ is bumped in every release and is correct even for
    # editable/source installs, where dist metadata freezes at install time and
    # went stale (it reported 0.8.36 on a 0.8.171 tree). Metadata is the fallback,
    # not the source of truth. This string also feeds the editor-config version
    # pin, so it must never be stale.
    try:
        from finops import __version__
        if __version__:
            return __version__
    except Exception:
        pass
    try:
        from importlib.metadata import version
        return version("finops-mcp")
    except Exception:
        return ""


# Pin uvx to a clean managed interpreter. Without it, uv builds against whatever
# `python` the user's shell exposes, and an x86_64 Anaconda base on an Apple
# Silicon machine makes uv source-build cryptography for the wrong architecture
# and fail ("incompatible architecture"). A managed version is arch-native and
# isolated from any conda/system Python contamination. The install command uses
# the same flag, so the interpreter is cached before the client ever launches.
_MANAGED_PYTHON = "3.12"


def _uvx_args(target: str = "") -> list:
    """The uvx args list written into client configs: a managed Python plus the
    pinned package, so the launch is arch-clean and a cache hit after install."""
    return ["--python", _MANAGED_PYTHON, _pinned_package(target)]


def _connected_extras() -> str:
    """Optional install extras the MCP launch needs, based on connected providers.

    Azure and GCP SDKs are optional (kept out of the default install so the first
    cold `uvx` launch stays small and fast), so the launch command must request
    them only when those providers are actually connected. AWS-only stays slim.
    """
    try:
        from .security.vault import Vault

        keys = [k.upper() for k in Vault.default().list_keys()]
    except Exception:
        return ""
    extras = []
    if any(k.startswith("AZURE") for k in keys):
        extras.append("azure")
    if any(k.startswith("GCP") or k.startswith("GOOGLE") for k in keys):
        extras.append("gcp")
    if any(k.startswith("SNOWFLAKE") for k in keys):
        extras.append("snowflake")
    return ",".join(extras)


def _pinned_package(target: str = "") -> str:
    """The uvx target written into client configs, pinned to one version.

    Unpinned, every PyPI release makes the next cold launch re-resolve and
    download, which can exceed an MCP client's startup timeout. Pinned, a
    release changes nothing for existing installs until `finops upgrade`.

    Connected-provider extras (azure/gcp) are folded in so their SDKs install on
    the next launch; an AWS-only setup stays on the slim default package.
    """
    v = target or _installed_version()
    extras = _connected_extras()
    base = f"finops-mcp[{extras}]" if extras else "finops-mcp"
    return f"{base}=={v}" if v else base


def _claude_config_file() -> "Path | None":
    """Return the existing claude_desktop_config.json, or None."""
    candidates = [
        Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
        Path.home() / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json",
        Path.home() / ".config" / "Claude" / "claude_desktop_config.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _upgrade_running_cli(current: str, target: str) -> bool:
    """Upgrade the `finops` command in the environment running right now.

    `finops upgrade` used to only warm the uvx cache and repin Claude Desktop. A
    normal `pip install finops-mcp` of the CLI stayed on the old version, so
    `finops <newcommand>` still failed right after a "success" message. This
    upgrades the actual command you type. Returns True if the CLI is now at target.
    """
    import subprocess

    if current == target:
        _ok(f"finops CLI already on {target}")
        return True

    py = sys.executable
    show = subprocess.run([py, "-m", "pip", "show", "finops-mcp"],
                          capture_output=True, text=True)
    if show.returncode != 0:
        # Not a pip install in this env (uv tool, pipx, or a shim). Be honest.
        _warn(f"This `finops` command is {current}; I can't upgrade it in place.")
        print("  Upgrade the CLI with whatever you installed it with:")
        print("    uv tool upgrade finops-mcp     # if you used `uv tool install`")
        print("    pipx upgrade finops-mcp        # if you used pipx")
        print(f"    pip install -U 'finops-mcp=={target}'")
        return False
    if "Editable project location" in show.stdout:
        _warn(f"This finops is an editable/source install ({current}); leaving it alone.")
        print(f"  It tracks your working tree. For a real install:  pip install -U 'finops-mcp=={target}'")
        return False

    print(f"  Upgrading this finops CLI: {current} -> {target} ...")
    res = subprocess.run([py, "-m", "pip", "install", "-U", f"finops-mcp=={target}"],
                         capture_output=True, text=True, timeout=600)
    if res.returncode != 0:
        _err(f"pip could not upgrade the CLI: {(res.stderr or res.stdout).strip()[:300]}")
        print(f"  Do it manually:  pip install -U 'finops-mcp=={target}'")
        return False
    _ok(f"finops CLI upgraded to {target}")
    return True


def _run_upgrade(target: str = "") -> None:
    """Upgrade nable, deliberately, in two places.

    (1) The `finops` CLI in the current environment, the command you type.
    (2) The MCP server that editors/Claude Desktop launch via uvx: wizard-written
        configs pin finops-mcp==X so a PyPI release never slows a working client, so
        this warms the new version into the uvx cache now (outside any startup
        window) and moves the pin. One Claude Desktop restart picks it up.
    """
    import json
    import shutil
    import subprocess

    _section("Upgrade nable")
    current = _installed_version() or "unknown"

    if not target:
        try:
            import httpx
            r = httpx.get("https://pypi.org/pypi/finops-mcp/json", timeout=10)
            r.raise_for_status()
            target = r.json()["info"]["version"]
        except Exception as e:
            _err(f"Could not reach PyPI to find the latest version ({e}).")
            print("  Pass one explicitly:  finops upgrade 0.8.57")
            return

    print(f"  Installed: {current}")
    print(f"  Latest:    {target}\n")

    # 1. The CLI you are typing right now.
    _upgrade_running_cli(current, target)

    # 2. The MCP server editors launch via uvx.
    uvx_bin = shutil.which("uvx")
    if not uvx_bin:
        print("\n  No uvx found, so there is no editor/Claude Desktop cache to warm.")
        return

    print("\n  Caching the new version for editors (so Claude never waits on it)...")
    try:
        res = subprocess.run(
            [uvx_bin, "--from", f"finops-mcp=={target}", "finops", "--help"],
            capture_output=True, text=True, timeout=300,
        )
        if res.returncode != 0:
            _err(f"Could not cache finops-mcp {target}: {(res.stderr or res.stdout).strip()[:300]}")
            _warn("Your config was NOT changed. You are still on the working version.")
            return
    except Exception as e:
        _err(f"Could not cache finops-mcp {target}: {e}")
        _warn("Your config was NOT changed. You are still on the working version.")
        return
    _ok(f"finops-mcp {target} cached")

    # Move the pin in the Claude Desktop config (nable key, legacy finops key too).
    config_path = _claude_config_file()
    if not config_path:
        _warn("Claude Desktop config not found. Nothing to repin.")
        print("  If you use another MCP client, update its nable entry to:")
        print(f"    {uvx_bin} {_pinned_package(target)}")
        return
    try:
        config = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        _err(f"Could not parse {config_path}: {e}")
        return

    moved = False
    for server_key in ("nable", "finops"):
        entry = config.get("mcpServers", {}).get(server_key)
        if not entry:
            continue
        args = entry.get("args") or []
        new_args = [
            _pinned_package(target) if isinstance(a, str) and a.split("==")[0] == "finops-mcp" else a
            for a in args
        ]
        if new_args != args:
            entry["args"] = new_args
            moved = True
    if moved:
        config_path.write_text(json.dumps(config, indent=2) + "\n")
        config_path.chmod(0o600)
        _ok(f"Claude Desktop pinned to finops-mcp=={target}")
        print("\n  Restart Claude Desktop once to pick it up. The new version is already")
        print("  cached, so the restart is instant.")
    else:
        _warn("No finops-mcp pin found in the Claude Desktop config (nothing changed).")
        print("  Re-run 'finops setup claude' to write a pinned entry.")


def _build_mcp_server_entry() -> "tuple[dict, str]":
    """Build the nable mcpServers entry (command/args/env), shared by every client
    writer so Claude Desktop, Cursor, and Claude Code never drift apart. Pins the
    installed version under uvx for the same reason Claude Desktop does: an
    unpinned re-resolve on first launch can blow past a client's startup timeout."""
    import shutil
    uvx_bin = shutil.which("uvx")
    finops_bin = shutil.which("finops-mcp") or str(Path(sys.executable).parent / "finops-mcp")
    if uvx_bin:
        mcp_entry: dict = {"command": uvx_bin, "args": _uvx_args()}
        display_cmd = f"uvx --python {_MANAGED_PYTHON} {_pinned_package()}"
    else:
        mcp_entry = {"command": finops_bin}
        display_cmd = finops_bin
    try:
        from .security.vault import Vault
        _val = Vault.default().get("FINOPS_LICENSE_KEY")
        if _val:
            mcp_entry["env"] = {"FINOPS_LICENSE_KEY": _val}
    except Exception:
        pass
    return mcp_entry, display_cmd


def _merge_write_mcpservers(config_path: Path, mcp_entry: dict) -> bool:
    """Merge nable into a {"mcpServers": {...}} JSON config, preserving other
    servers and migrating a legacy "finops" key. Returns True on write, False if
    the file is unreadable, never clobber a config we cannot parse."""
    import json
    try:
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(config, dict):
        return False
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return False
    existing = servers.get("nable") or servers.get("finops") or {}
    entry = dict(mcp_entry)
    if isinstance(existing, dict) and existing.get("env"):
        entry["env"] = {**existing["env"], **entry.get("env", {})}
    servers.pop("finops", None)
    servers["nable"] = entry
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    try:
        config_path.chmod(0o600)
    except OSError:
        pass
    return True


def _cursor_config_path() -> "Path | None":
    """~/.cursor/mcp.json when Cursor is plausibly installed (its dir exists or the
    app is present). Cursor reads global MCP servers from this file."""
    cursor_dir = Path.home() / ".cursor"
    present = (
        cursor_dir.exists()
        or Path("/Applications/Cursor.app").exists()
        or (Path.home() / "Applications" / "Cursor.app").exists()
    )
    return (cursor_dir / "mcp.json") if present else None


def _configure_cursor(mcp_entry: dict) -> bool:
    """Write nable into ~/.cursor/mcp.json (same schema as Claude Desktop)."""
    path = _cursor_config_path()
    if path is None:
        return False
    if _merge_write_mcpservers(path, mcp_entry):
        _ok(f"Cursor configured → {path}")
        return True
    _warn(f"Could not parse {path}; left it untouched.")
    return False


def _claude_code_add_command() -> "str | None":
    """The `claude mcp add` one-liner for Claude Code, if its CLI is on PATH. We
    print this rather than shelling out, so we never mutate the user's Claude Code
    config in a way that depends on their CLI version."""
    import shutil
    if not shutil.which("claude"):
        return None
    if shutil.which("uvx"):
        return f"claude mcp add -s user nable -- uvx --python {_MANAGED_PYTHON} {_pinned_package()}"
    return "claude mcp add -s user nable -- finops-mcp"


def _configure_mcp_clients() -> dict:
    """Wire nable into every MCP client we can detect, and report the truth.

    Returns {"configured": [auto-written client names], "manual": [(client, cmd)]}
    so the caller tells the user exactly which editors are ready and what is left,
    instead of claiming "you're set up" for clients that were never wired (the
    silent Cursor/Claude Code miss that sank a real slice of the funnel)."""
    configured: list = []
    manual: list = []
    mcp_entry, _display = _build_mcp_server_entry()

    if _configure_claude_desktop():
        configured.append("Claude Desktop")
    try:
        if _cursor_config_path() is not None and _configure_cursor(mcp_entry):
            configured.append("Cursor")
    except Exception:
        pass
    cc = _claude_code_add_command()
    if cc:
        manual.append(("Claude Code", cc))
    return {"configured": configured, "manual": manual}


def _configure_claude_desktop() -> bool:
    """
    Auto-detect claude_desktop_config.json and inject the nable MCP server
    with the correct absolute path to finops-mcp. Returns True when configured
    (or already configured), False otherwise.

    This is the #1 reason nable doesn't work on company computers. Claude
    Desktop is a GUI app that doesn't inherit the user's shell PATH, so
    'finops-mcp' as a bare command fails unless it's in /usr/bin or /bin.

    We resolve the absolute path at setup time and write it to the config.
    """
    try:
        return bool(_configure_claude_desktop_inner())
    except (KeyboardInterrupt, EOFError):
        print("\n  Claude Desktop configuration skipped.")
        return False
    except Exception as e:
        _warn(f"Claude Desktop auto-config failed: {e}")
        print("  Run 'finops setup claude' to try again, or configure manually.")
        return False


def _configure_claude_desktop_inner() -> bool:
    """Inner implementation -- called by _configure_claude_desktop with error handling.
    Returns True when the config was written or was already current, else False."""
    import json
    import shutil

    # Platform-specific config paths
    config_paths = [
        # macOS
        Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
        # Windows
        Path.home() / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json",
        # Linux (some distributions)
        Path.home() / ".config" / "Claude" / "claude_desktop_config.json",
        Path.home() / ".config" / "claude-desktop" / "claude_desktop_config.json",
    ]

    config_path = next((p for p in config_paths if p.parent.exists()), None)

    if config_path is None:
        import platform as _platform
        # On macOS, Claude Desktop may be installed but not yet launched,
        # so its config directory does not exist yet. Create it so setup can proceed.
        if _platform.system() == "Darwin":
            mac_claude_dir = Path.home() / "Library" / "Application Support" / "Claude"
            mac_app = Path("/Applications/Claude.app")
            mac_app_user = Path.home() / "Applications" / "Claude.app"
            if mac_app.exists() or mac_app_user.exists():
                mac_claude_dir.mkdir(parents=True, exist_ok=True)
                config_path = mac_claude_dir / "claude_desktop_config.json"
                print("\n  (Claude Desktop app found but not yet launched, creating config directory.)")
            else:
                print("\n  ──────────────────────────────────────────────────")
                print("  Claude Desktop not found. To finish setup manually:")
                print("  1. Install Claude Desktop from https://claude.ai/download")
                print("  2. Open Claude Desktop at least once, then run 'finops setup claude'")
                print("  ──────────────────────────────────────────────────")
                return False
        else:
            print("\n  ──────────────────────────────────────────────────")
            print("  Claude Desktop not found. To finish setup manually:")
            print("  1. Install Claude Desktop from https://claude.ai/download")
            print("  2. Run 'finops setup claude' after installing")
            print("  ──────────────────────────────────────────────────")
            return False

    # Determine the best launch strategy:
    # 1. uvx , isolated venv, works in corporate environments with no PATH issues
    # 2. absolute path to finops-mcp binary
    uvx_bin = shutil.which("uvx")
    finops_bin = shutil.which("finops-mcp")
    if not finops_bin:
        # Fall back to the scripts directory next to the current Python interpreter
        candidate = Path(sys.executable).parent / "finops-mcp"
        finops_bin = str(candidate)
        if not candidate.exists():
            _warn(
                f"finops-mcp binary not found at {finops_bin}.\n"
                f"  Make sure `pip install finops-mcp` completed successfully, then run\n"
                f"  'finops setup claude' again."
            )

    if uvx_bin:
        # Pin the exact installed version. An unpinned "finops-mcp" re-resolves
        # "latest" on the first launch after every PyPI release, and that cold
        # download can blow past Claude Desktop's startup timeout ("Server
        # disconnected"). Pinned, releases are invisible until the user runs
        # `finops upgrade`, which moves the pin and pre-warms the cache.
        pinned = _pinned_package()
        mcp_entry = {"command": uvx_bin, "args": _uvx_args()}
        display_cmd = f"uvx --python {_MANAGED_PYTHON} {pinned}  (uvx found at {uvx_bin})"
    else:
        mcp_entry = {"command": finops_bin}
        display_cmd = finops_bin

    # Load existing config or start fresh
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = json.load(f)
        except (json.JSONDecodeError, OSError) as parse_err:
            _warn(f"Could not parse Claude Desktop config: {parse_err}")
            _warn(f"Check {config_path} manually before re-running 'finops setup claude'.")
            return False
    else:
        config = {}

    config.setdefault("mcpServers", {})

    # Pull non-secret config from the vault into the env block.
    # AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are intentionally excluded:
    # the MCP server loads them from the vault at startup via load_vault_to_env(),
    # so they never need to appear in plaintext in claude_desktop_config.json.
    vault_env: dict[str, str] = {}
    try:
        from .security.vault import Vault
        _v = Vault.default()
        for _k in ("FINOPS_LICENSE_KEY",):
            _val = _v.get(_k)
            if _val:
                vault_env[_k] = _val
    except Exception:
        pass

    if vault_env:
        mcp_entry["env"] = {**vault_env, **mcp_entry.get("env", {})}

    # Standardize on "nable" (the product name). Read either key so we can
    # migrate a legacy "finops" entry without leaving both registered.
    existing = config["mcpServers"].get("nable") or config["mcpServers"].get("finops", {})
    # Already up-to-date? (compare command/args only, env may differ)
    existing_base = {k: v for k, v in existing.items() if k != "env"}
    new_base = {k: v for k, v in mcp_entry.items() if k != "env"}
    # Only short-circuit when the entry is ALREADY under the new "nable" key. If it
    # exists only under the legacy "finops" key, fall through to the migration below
    # (pop "finops", register "nable") even when the command is otherwise identical.
    if existing_base == new_base and not vault_env and "nable" in config["mcpServers"]:
        _ok(f"Claude Desktop already configured: {display_cmd}")
        return True

    from .welcome import bold, dim, _rule
    print(f"\n  {_rule()}")
    print(f"  {bold('Configure Claude Desktop to use nable?')}")
    print(f"  {dim('Config file:')} {config_path}")
    print(f"  {dim('Command:    ')} {display_cmd}")
    _notes = []
    if uvx_bin:
        _notes.append("uvx mode: works on corporate machines without PATH changes")
    if vault_env:
        _notes.append(f"including {len(vault_env)} credential(s) from vault")
    if existing:
        _notes.append("updates existing entry")
    for _note in _notes:
        print(f"  {dim('· ' + _note)}")
    print(f"  {_rule()}")

    try:
        ans = _prompt("  Write config? [Y/n]", default="y").lower()
    except (KeyboardInterrupt, EOFError):
        print("\n  Skipped. Add manually:")
        _print_manual_config(mcp_entry)
        return False

    if ans not in ("y", "yes", ""):
        print("  Skipped. Add manually:")
        _print_manual_config(mcp_entry)
        return False

    # Preserve any env keys already in the existing entry that we're not overwriting
    if existing.get("env"):
        merged_env = {**existing["env"], **mcp_entry.get("env", {})}
        if merged_env:
            mcp_entry["env"] = merged_env

    config["mcpServers"].pop("finops", None)  # drop legacy key so we never double-register
    config["mcpServers"]["nable"] = mcp_entry

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    config_path.chmod(0o600)

    _ok(f"Claude Desktop configured → {config_path}")
    print("  Restart Claude Desktop for the changes to take effect.")
    return True


def _print_manual_config(mcp_entry: dict) -> None:
    import json
    snippet = json.dumps({"mcpServers": {"nable": mcp_entry}}, indent=2)
    print("\n  Add to your claude_desktop_config.json:\n")
    for line in snippet.splitlines():
        print(f"    {line}")
    print()
    if "FINOPS_LICENSE_KEY" in snippet:
        _warn("This block contains your license key. Treat it as a secret: "
              "keep it out of screen-shares, public gists, and tickets.")


if __name__ == "__main__":
    main(sys.argv[1:])
