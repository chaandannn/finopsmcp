"""
License validation for nable (finops-mcp).

Tiers
─────
free         Permanent. ~90% of the platform. No key required.
             Cost queries, anomaly detection, rightsizing, PR comments,
             Slack/Teams alerts, all connectors, snapshots, attribution.

trial        7 days of full Pro access from first install. Kicks in
             automatically when no key is set. Uses the same dual-store
             (OS keyring + file) so deleting one source can't reset it.

pro          Full access. Unlocked by a signed license key.
             Adds: ticket auto-creation, scheduled email digests,
             commitment purchase recommendations, multi-team org reports.

Key format:  FINOPS-2-{b64_payload}-{b64_ed25519_sig}   (current)
             FINOPS-1-{b64_payload}-{b64_hmac}           (retired: the v1 HMAC secret
             leaked in public git history, so v1 keys are forgeable and never accepted)
  payload:   base64url(json: {"e": email, "d": issued_YYYYMMDD, "p": "pro"})
  v2 sig:    Ed25519(private_key, "2:" + b64_payload). Clients verify with the
             bundled public key — no shared secret, and keys cannot be forged.
  v1 sig:    HMAC-SHA256(_SECRET, "1:" + b64_payload). Needs the secret on the
             client to verify; kept only for backward compatibility.

Generate a key (run server-side, with FINOPS_LICENSE_PRIVATE_KEY set):
  python -c "from finops.license import generate_key; print(generate_key('user@example.com'))"
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import platform
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger("finops.license")

_env_secret = os.environ.get("FINOPS_LICENSE_SECRET", "")
# FINOPS_LICENSE_SECRET must be set in the environment. No default is kept in
# source — a committed default allows anyone with repo access to forge valid keys.
# If the env var is missing, pro key validation is disabled and all keys are
# treated as invalid so users fall through to the free/trial tier safely.
_SECRET = _env_secret.encode() if _env_secret else b""
if not _env_secret:
    log.debug(
        "FINOPS_LICENSE_SECRET is not set. Pro license key validation is disabled. "
        "Set this env var to the secret used when keys were issued."
    )
# Ed25519 PUBLIC key for verifying v2 license keys. Safe to ship: it can verify
# keys but cannot create them. Keys are signed with the matching private key
# (FINOPS_LICENSE_PRIVATE_KEY), held server-side only and never bundled — so
# clients verify with no shared secret and nobody can forge keys from the package.
_PUBLIC_KEY_B64 = "5wMiDYa-2vOJqIr94jOkIovlTm_bBDdh43B5uFJ3Y34"

_KEY_TTL_DAYS   = 366          # pro keys expire 1 year after issue date
_VALID_PLANS    = {"pro", "team", "trial", "enterprise"}
_UPGRADE_URL    = "https://getnable.com/#pricing"
_CHECKOUT_URL     = "https://buy.stripe.com/3cI3cucid6J85tm3wC2Nq08"   # Team $1,000/mo direct checkout
_PRO_CHECKOUT_URL = "https://buy.stripe.com/5kQeVc4PL9Vk4piaZ42Nq0a"   # Pro $25/mo direct checkout
_ACTIVATE_CMD   = "finops login"                     # shown after purchase: one email + code, no key
_TRIAL_DAYS  = 7
_TRIAL_FILE  = Path.home() / ".finops-mcp" / "trial_start"

# Keyring service/username for the trial start date. Earlier releases disguised
# this as "system.cache.prefs" to resist trial resets, which backfired: macOS
# shows the item name in its permission dialog, so users saw "python wants to
# access system.cache.prefs", unattributable and malware-shaped. The honest name
# costs little; the HMAC signature and the dual store are the real protection.
# Legacy names are kept so existing entries migrate on first read.
_KR_SERVICE  = "nable-trial"
_KR_USERNAME = "trial-start"
_KR_LEGACY_SERVICE  = "system.cache.prefs"
_KR_LEGACY_USERNAME = "user.defaults"

# ── Pro-only features (the ~10%) ──────────────────────────────────────────────
# Everything NOT in this set is available on the free tier.
#
# Free tier includes:
#   All cost queries · Anomaly detection + Slack/Teams alerts · Rightsizing (view)
#   PR cost comments + budget CI gate · All cloud + SaaS connectors
#   Kubernetes cost analysis · Helm release visibility · Efficiency scorecard
#   Budgets (create/track/alert) · Scheduled Slack reports · Postgres shared mode
#   Commitment coverage analysis (view %) · Tag attribution · Idle resource detection
#   Multi-account account listing · Storage info · Check notification config
#
PRO_FEATURES: set[str] = {
    "ticket_creation",           # auto-create Jira / Linear / GitHub Issues from any finding
    "scheduled_email_digests",   # email delivery of scheduled reports
    "commitment_recommendations", # RI / SP purchase recommendations with $ amounts + ROI
    "org_reports",               # full org-wide cost rollup across all accounts / OUs
    # ── The pull/push line: free = ask on demand, Pro = it runs for you ───────────
    "alerts",                    # proactive alert policies + scheduled push (set_alert_policy, weekly push)
    "forecasting",               # forward projections: cost / Azure / LLM forecasts
    "ai_unit_economics",         # cost per PR by model, AI KPIs, the GitHub engineering-attribution report
    "remediation",               # drafting the fix: open rightsizing / terraform-tag PRs
    # ── The agent team (watch, act, coordinate with agents) ──────────────────────
    # NOTE: "agent_gate" is deliberately NOT here. The guard is the front door:
    # it needs no cloud account, no credentials and no license, which makes it the
    # one surface that can reach someone before they have connected anything. It
    # was previously Pro-but-ungated by the temporary _HOLD_AI_UNGATE flag below,
    # which meant the day pricing shipped the gate would have gone SILENT for every
    # free user, failing open with no message. Free forever is a commitment, not a
    # hold. The paid line for guard is team-shaped (shared policy, audit trail,
    # fleet enforcement), never the individual's ability to stop their own agent.
    "agent_learning",            # the Ledger: mark acted-on, verify savings landed, learned approval profile
    # Cost queries, anomaly detection, rightsizing findings, and the full
    # multi-cloud normalized view (compare_providers, total-spend-all-sources) are
    # intentionally FREE: breadth across every cloud/SaaS/AI provider is the wedge,
    # not the upsell. Users see the value on demand, then pay for it to run
    # continuously and act for them. The agent team (gate, remediation, learning)
    # and the always-on push layer are Pro.
}

# ── Temporary AI/agent ungate (2026-07-10) ───────────────────────────────────
# While we get the first real users fully set up, the AI / agent features run
# FREE. Pricing + subscription for them is being decided (the North-style flat +
# % of savings model under review, see the pricing plan). This is a deliberate,
# reversible hold, not a tier change: set _HOLD_AI_UNGATE = False (or empty the
# set) to re-gate them the moment the paid model ships. Everything still lives in
# PRO_FEATURES so the upgrade copy and the eventual re-gate stay one edit away.
_HOLD_AI_UNGATE = True
_UNGATED_AI_FEATURES: frozenset[str] = frozenset({
    "agent_learning",             # the Ledger: verify savings, learn approvals
    "remediation",                # the fix as a pull request
    "ai_unit_economics",          # AI cost per PR, AI KPIs
    "forecasting",                # cost / Azure / LLM forecasts
    "alerts",                     # proactive alert policies + scheduled push
    "commitment_recommendations", # RI / SP purchase recommendations
})


def _is_ungated_now(feature: str) -> bool:
    """True when a normally-Pro feature is on the temporary free hold above."""
    return _HOLD_AI_UNGATE and feature in _UNGATED_AI_FEATURES


# ── Team-only features ($1k/mo flat) ─────────────────────────────────────────
# The conversational layer: team-shaped value gets team-shaped pricing. Hard
# gate, no free questions. Trial includes Team so demos feel the full product.
TEAM_FEATURES: set[str] = {
    "slack_conversational_bot",  # @nable questions, thread memory, RCA investigations
    "slack_remediation",         # draft PRs/tickets from chat behind the approval gate
}


@dataclass
class LicenseStatus:
    mode: str          # "free" | "trial" | "pro" | "team" | "enterprise" | "invalid"
    email: str
    issued: str        # YYYY-MM-DD or ""
    message: str
    days_remaining: int = -1   # trial days left (-1 = not applicable)

    @property
    def is_pro(self) -> bool:
        # Higher tiers include lower. (Enterprise keys previously failed pro
        # gates because this list stopped at "pro".)
        return self.mode in ("pro", "team", "enterprise", "trial")

    @property
    def is_team(self) -> bool:
        return self.mode in ("team", "enterprise", "trial")

    @property
    def is_free(self) -> bool:
        return self.mode in ("free", "pro", "team", "enterprise", "trial")


# ── Crypto helpers ────────────────────────────────────────────────────────────

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    pad = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * (pad % 4))


def _sign(version: str, payload_b64: str) -> str:
    msg = f"{version}:{payload_b64}".encode()
    return _b64(hmac.new(_SECRET, msg, hashlib.sha256).digest())


def _verify_ed25519(payload_b64: str, sig_b64: str) -> bool:
    """Verify a v2 key signature against the bundled public key. No secret needed."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub = Ed25519PublicKey.from_public_bytes(_unb64(_PUBLIC_KEY_B64))
        pub.verify(_unb64(sig_b64), f"2:{payload_b64}".encode())
        return True
    except Exception:
        return False


# ── Machine fingerprint ───────────────────────────────────────────────────────

# Local anti-tamper key for the trial clock and machine fingerprint. This is
# deliberately NOT the Pro signing secret (_SECRET), which is unset on real
# installs — HMAC-ing the trial with an empty key made every trial signature
# forgeable by anyone, even without reading the source. This is local-first DRM,
# not a security boundary: the constant ships in the open-source package, so a
# determined user who reads the source can still forge it. It only raises the bar
# above the trivial empty-key default and stops casual trial-clock tampering. The
# real lock, if trial abuse ever costs real money, is server-side validation,
# avoided today because local-first / no-egress is a core product promise.
_TRIAL_OBFUSCATION_KEY = b"finops-mcp.trial.anti-tamper.v1"


def _machine_id() -> str:
    parts = [platform.node(), platform.machine(), str(Path.home())]
    raw = "|".join(parts).encode()
    return hmac.new(_TRIAL_OBFUSCATION_KEY, raw, hashlib.sha256).hexdigest()[:24]


def _sign_date(iso_date: str) -> str:
    msg = f"trial:{_machine_id()}:{iso_date}".encode()
    return hmac.new(_TRIAL_OBFUSCATION_KEY, msg, hashlib.sha256).hexdigest()[:32]


# ── Keyring helpers ───────────────────────────────────────────────────────────

# The trial date, cached after the first successful keychain read so the OS
# keychain is not re-read (and macOS does not re-prompt) on every status check.
_kr_cached_date: "date | None" = None


def _macos_keychain_missing() -> bool:
    """True on macOS when there is no login keychain to open.

    When $HOME/Library/Keychains has no login keychain (an altered $HOME, a
    sandbox, a hardened setup, or a genuinely deleted keychain), the OS Security
    framework pops a BLOCKING 'a keychain cannot be found to store X' modal the
    instant keyring is called, BEFORE the Python exception is raised, so the
    except-and-pass around the call cannot suppress it. The only way to avoid the
    modal is to not make the call. Detect the missing keychain and fall back to
    the signed file store, which is the primary store anyway."""
    if sys.platform != "darwin":
        return False
    try:
        kc = Path.home() / "Library" / "Keychains"
        return not any(kc.glob("login.keychain*"))
    except Exception:
        return True  # cannot tell -> prefer the file store, never risk the modal


def _keyring_disabled() -> bool:
    """FINOPS_NO_KEYRING=1 (or FINOPS_AIRGAP=1) forbids any OS keychain access.

    The keychain belongs to the macOS user, not to $HOME, so an ephemeral run
    (tests, demos, scratch-HOME cold runs) that misses the trial file would
    otherwise probe the developer's real keychain, including the legacy
    disguised entry, and prompt. Also disabled when no macOS login keychain
    exists, to avoid the blocking 'cannot be found' modal. Mirrors
    security/vault.py."""
    return (
        os.environ.get("FINOPS_NO_KEYRING", "") == "1"
        or os.environ.get("FINOPS_AIRGAP", "") == "1"
        or _macos_keychain_missing()
    )


def _kr_get() -> date | None:
    global _kr_cached_date
    if _keyring_disabled():
        return None
    if _kr_cached_date is not None:
        return _kr_cached_date
    try:
        import keyring  # type: ignore
        val = keyring.get_password(_KR_SERVICE, _KR_USERNAME)
        if not val:
            # One-time migration from the legacy disguised entry.
            val = keyring.get_password(_KR_LEGACY_SERVICE, _KR_LEGACY_USERNAME)
            if val:
                iso, _, sig = val.partition(":")
                if sig and hmac.compare_digest(sig, _sign_date(iso)):
                    keyring.set_password(_KR_SERVICE, _KR_USERNAME, val)
                try:
                    keyring.delete_password(_KR_LEGACY_SERVICE, _KR_LEGACY_USERNAME)
                except Exception:
                    pass
        if val:
            iso, _, sig = val.partition(":")
            if sig and hmac.compare_digest(sig, _sign_date(iso)):
                _kr_cached_date = date.fromisoformat(iso)
                return _kr_cached_date
            log.debug("Keyring entry signature mismatch - ignoring")
    except Exception:
        pass
    return None


def _kr_set(d: date) -> None:
    global _kr_cached_date
    if _keyring_disabled():
        return
    try:
        import keyring  # type: ignore
        iso = d.isoformat()
        keyring.set_password(_KR_SERVICE, _KR_USERNAME, f"{iso}:{_sign_date(iso)}")
        _kr_cached_date = d  # warm the cache so the next read skips the keychain
    except Exception:
        pass


# ── File helpers ──────────────────────────────────────────────────────────────

def _file_get() -> date | None:
    try:
        if _TRIAL_FILE.exists():
            lines = _TRIAL_FILE.read_text().strip().splitlines()
            if not lines:
                return None
            iso = lines[0].strip()
            sig = lines[1].strip() if len(lines) > 1 else ""
            # Require a valid signature; an empty or missing sig is rejected
            if not sig or not hmac.compare_digest(sig, _sign_date(iso)):
                log.debug("Trial file signature missing or invalid, ignoring")
                return None
            return date.fromisoformat(iso)
    except Exception:
        pass
    return None


def _file_set(d: date) -> None:
    try:
        _TRIAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        iso = d.isoformat()
        _TRIAL_FILE.write_text(f"{iso}\n{_sign_date(iso)}\n")
        # Owner-only: the signature shouldn't be world-readable (trial-forgery aid).
        try:
            _TRIAL_FILE.chmod(0o600)
        except OSError:
            pass
    except Exception:
        pass


# ── Trial date logic ──────────────────────────────────────────────────────────

def _get_or_create_trial_start() -> date:
    """
    Return the trial start date. The signed file is the primary store; the OS
    keyring is the recovery store, consulted only when the file is missing or
    invalid. Steady state therefore never opens the keychain: on macOS every
    keychain access from an unsigned interpreter can pop a permission dialog,
    and the previous version of this function did a read AND an unconditional
    rewrite on every license check. The rewrite recreated the keychain item,
    which reset its ACL, so "Always Allow" never stuck and users were prompted
    on every session. The keyring is now written exactly once, at creation.

    Anti-reset properties are unchanged: deleting the file restores it from the
    keyring, a tampered date in either store fails the HMAC signature (which is
    machine-bound, so a copied file can't move the date either), and deleting
    both stores resets the trial, the same accepted residual as before.
    """
    file_date = _file_get()
    if file_date is not None:
        return file_date

    kr_date = _kr_get()
    if kr_date is not None:
        _file_set(kr_date)  # restore the missing file store; keychain untouched
        return kr_date

    today = date.today()
    _kr_set(today)  # creating our own item never prompts; only rewrites did
    _file_set(today)
    return today


# ── Key generation / validation ───────────────────────────────────────────────

def generate_key(email: str, plan: str = "pro", version: int = 2,
                 expiry_days: int | None = None) -> str:
    """Generate a signed license key. Run server-side, where the signing key lives.

    version=2 (default): Ed25519, signed with FINOPS_LICENSE_PRIVATE_KEY. Clients
        verify with the bundled public key and need no shared secret.
    version=1 is retired: its HMAC secret leaked in public git history, so any
        v1 key is forgeable. Generation and validation both refuse it.

    expiry_days: when set, embeds an explicit expiry ("x", YYYYMMDD) this many
        days out. The client treats "x" as a hard expiry; keys without it fall
        back to the legacy _KEY_TTL_DAYS window from the issue date. The Stripe
        webhook sets this so a monthly subscription's key dies with the billing
        cycle (plus grace), not a year after issue.
    """
    if version == 1:
        raise ValueError("v1 license keys are retired; the signing secret is public.")
    fields = {"e": email, "d": date.today().strftime("%Y%m%d"), "p": plan}
    if expiry_days is not None:
        fields["x"] = (date.today() + timedelta(days=expiry_days)).strftime("%Y%m%d")
    payload = _b64(json.dumps(fields).encode())
    if version == 2:
        priv_b64 = os.environ.get("FINOPS_LICENSE_PRIVATE_KEY", "")
        if not priv_b64:
            raise RuntimeError(
                "FINOPS_LICENSE_PRIVATE_KEY is not set. It is required to sign v2 keys and "
                "must only ever live on the issuing side, never in the shipped package."
            )
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        priv = Ed25519PrivateKey.from_private_bytes(_unb64(priv_b64))
        sig = _b64(priv.sign(f"2:{payload}".encode()))
        return f"FINOPS-2-{payload}-{sig}"


def validate_key(key: str) -> LicenseStatus:
    """Parse and verify a license key string."""
    if not key:
        # No key — give a 7-day pro trial, then drop to free forever
        trial_start   = _get_or_create_trial_start()
        days_used     = (date.today() - trial_start).days
        days_remaining = max(0, _TRIAL_DAYS - days_used)

        if days_remaining > 0:
            return LicenseStatus(
                mode="trial",
                email="",
                issued=trial_start.isoformat(),
                message=(
                    f"Trial: {days_remaining} day{'s' if days_remaining != 1 else ''} remaining — all features unlocked."
                ),
                days_remaining=days_remaining,
            )
        else:
            # Trial over → free tier (not expired / blocked)
            return LicenseStatus(
                mode="free",
                email="",
                issued=trial_start.isoformat(),
                message="Free tier active.",
                days_remaining=0,
            )

    parts = key.strip().split("-", 3)
    if len(parts) != 4 or parts[0] != "FINOPS" or parts[1] not in ("1", "2"):
        return LicenseStatus(
            mode="invalid",
            email="",
            issued="",
            message=f"Invalid license key format. Get a valid key at {_UPGRADE_URL}",
        )

    _, version, payload_b64, provided_sig = parts

    # Verify the signature. Only v2 (Ed25519, bundled public key) is accepted.
    # v1 HMAC keys are retired: the signing secret leaked in public git
    # history, so every v1 key is forgeable and must be refused.
    if version == "1":
        return LicenseStatus(
            mode="invalid",
            email="",
            issued="",
            message=f"v1 license keys are retired. Get a current key at {_UPGRADE_URL}",
        )
    sig_ok = _verify_ed25519(payload_b64, provided_sig)

    if not sig_ok:
        return LicenseStatus(
            mode="invalid",
            email="",
            issued="",
            message=f"License key signature invalid. Contact support or renew at {_UPGRADE_URL}",
        )

    try:
        payload = json.loads(_unb64(payload_b64))
    except Exception:
        return LicenseStatus(
            mode="invalid",
            email="",
            issued="",
            message="License key payload corrupt. Contact support.",
        )

    email      = payload.get("e", "")
    issued_raw = payload.get("d", "")
    plan       = payload.get("p", "pro")

    # Validate plan field against allowlist
    if plan not in _VALID_PLANS:
        log.warning("License key contains unrecognised plan %r; treating as invalid.", plan)
        return LicenseStatus(
            mode="invalid",
            email=email,
            issued="",
            message="License key contains an unrecognised plan. Contact support.",
        )

    try:
        issued     = date(int(issued_raw[:4]), int(issued_raw[4:6]), int(issued_raw[6:8]))
        issued_str = issued.isoformat()
    except Exception:
        issued_str = issued_raw
        issued     = None

    # Expiry check. A key may carry an explicit expiry "x" (YYYYMMDD) set at
    # issue time from the billing cycle; honor it as a hard expiry. Keys without
    # "x" (legacy) fall back to _KEY_TTL_DAYS from the issue date. The explicit
    # expiry is what makes a lapsed monthly subscription stop within the cycle
    # instead of a year later.
    expiry_raw = payload.get("x", "")
    expiry = None
    if expiry_raw:
        try:
            expiry = date(int(expiry_raw[:4]), int(expiry_raw[4:6]), int(expiry_raw[6:8]))
        except (ValueError, TypeError):
            expiry = None
    if expiry is None and issued is not None:
        expiry = issued + timedelta(days=_KEY_TTL_DAYS)

    if expiry is not None and date.today() > expiry:
        return LicenseStatus(
            mode="invalid",
            email=email,
            issued=issued_str,
            message=(
                f"License key expired on {expiry.isoformat()}. "
                f"Renew your subscription at {_UPGRADE_URL}"
            ),
        )

    return LicenseStatus(
        mode=plan,
        email=email,
        issued=issued_str,
        message=f"Pro license active: {email}, issued {issued_str}.",
    )


# ── Runtime helpers ───────────────────────────────────────────────────────────

def _vault_license_key() -> str:
    """A license key stored locally by `finops login` / `finops setup license`.
    Lazy + guarded so a vault hiccup can never break gating (worst case: free)."""
    try:
        from .security.vault import Vault
        return (Vault.default().get("FINOPS_LICENSE_KEY") or "").strip()
    except Exception:
        return ""


def store_license(key: str) -> LicenseStatus:
    """Validate a key and, if it is a paid plan, persist it to the local vault so
    the server picks it up with no env var. Returns the validated status; an
    invalid key is never stored. Used by `finops login`."""
    global _status
    key = (key or "").strip()
    status = validate_key(key)
    if status.mode in ("pro", "team", "enterprise"):
        try:
            from .security.vault import Vault
            Vault.default().store("FINOPS_LICENSE_KEY", key)
        except Exception:
            pass
        _status = None  # force a re-read on the next get_status()
    return status


def clear_license() -> None:
    """Remove the locally stored license (used by `finops logout`). An explicit
    FINOPS_LICENSE_KEY env var, if set, is left untouched."""
    global _status
    try:
        from .security.vault import Vault
        Vault.default().delete("FINOPS_LICENSE_KEY")
    except Exception:
        pass
    _status = None


def check_license() -> LicenseStatus:
    """Validated license status. An explicit FINOPS_LICENSE_KEY env var wins (CI,
    power users); otherwise the key stored locally by `finops login` is used.
    Validation is always offline against the bundled public key."""
    key    = os.environ.get("FINOPS_LICENSE_KEY", "").strip() or _vault_license_key()
    status = validate_key(key)

    if status.mode == "trial":
        log.debug("License: pro trial, %d days remaining", status.days_remaining)
    elif status.mode == "free":
        log.debug("License: free tier")
    elif status.mode in ("pro", "team", "enterprise"):
        log.debug("License: %s, %s", status.mode, status.email)
    else:
        log.warning("License: invalid key — %s", status.message)

    return status


_status: LicenseStatus | None = None


def get_status() -> LicenseStatus:
    global _status
    if _status is None:
        _status = check_license()
    return _status


def require_team(feature: str) -> dict | None:
    """
    Gate a Team-only feature ($1,000/mo flat). Returns an error dict if access
    is denied, None if granted. Trial keys pass, so demos feel the full product.

    Usage:
        if err := require_team("slack_conversational_bot"):
            return err
    """
    if feature not in TEAM_FEATURES:
        log.warning("require_team called for non-team feature %r — allowing", feature)
        return None

    s = get_status()
    if s.is_team:
        return None

    friendly = feature.replace("_", " ")
    return {
        "error": f"{friendly} requires the Pro plan.",
        "plan": s.mode,
        "upgrade": (
            f"The conversational Slack bot and chat remediation are part of nable Team "
            f"($1,000/mo flat, unlimited seats). Start a free trial or see plans: {_UPGRADE_URL} "
            f"Then sign in: {_ACTIVATE_CMD}"
        ),
    }


def require_pro(feature: str) -> dict | None:
    """
    Gate a Pro-only feature. Returns an error dict if access is denied, None if granted.
    Only the features listed in PRO_FEATURES are gated — everything else is free.

    Usage:
        if err := require_pro("ticket_creation"):
            return err
    """
    if feature not in PRO_FEATURES:
        # Caller mistake — feature isn't in the pro set, allow it
        log.warning("require_pro called for non-pro feature %r — allowing", feature)
        return None

    # Temporary free hold on the AI / agent features while early users get set up.
    if _is_ungated_now(feature):
        return None

    # Demo mode shows every feature (with synthetic data) so the product demos in
    # full to anyone evaluating it. Gating only applies to real, free-tier accounts.
    try:
        from .demo_data import is_demo
        if is_demo():
            return None
    except Exception:
        pass

    s = get_status()
    if s.is_pro:
        return None

    # Free tier — explain what they're missing and how to unlock it
    friendly = feature.replace("_", " ")

    # Craft a contextual upgrade message based on whether trial expired recently
    if s.mode == "free" and s.issued:
        try:
            trial_start = date.fromisoformat(s.issued)
            days_since_expiry = (date.today() - trial_start).days - _TRIAL_DAYS
            if 0 < days_since_expiry <= 30:
                urgency = (
                    f"Your {_TRIAL_DAYS}-day trial ended {days_since_expiry} day{'s' if days_since_expiry != 1 else ''} ago. "
                )
            else:
                urgency = ""
        except Exception:
            urgency = ""
    else:
        urgency = ""

    # Build a concise FOMO block showing everything Team unlocks
    _TEAM_FEATURES = [
        ("remediation",                "🔧 The fix as a pull request: rightsizing and tag PRs you approve"),
        ("agent_learning",             "🧠 The Ledger: verified savings + a gate that learns what you approve"),
        ("ticket_creation",            "🎫 Auto-create Jira / Linear / GitHub Issues from anomalies & rightsizing"),
        ("scheduled_email_digests",    "📧 Scheduled email reports — weekly, monthly, or custom cadence"),
        ("commitment_recommendations", "💰 RI / Savings Plan recommendations with exact $ ROI"),
        ("org_reports",                "🏢 Org-wide cost rollup across all accounts & OUs"),
        ("cur_athena_detail",          "🔍 Line-item CUR data — per-resource costs, RI waste, tag breakdown"),
        ("azure_detail",               "☁️  Azure resource-level cost detail & reservation utilization"),
        ("business_metrics",           "📈 Unit economics — cost per customer, hosting % of MRR"),
    ]

    lines = ["⬡  nable Pro — everything in free, plus:\n"]
    for key, desc in _TEAM_FEATURES:
        # Don't advertise a feature that is currently on the free hold as a paid
        # unlock; it would list things the user already has for free.
        if _is_ungated_now(key):
            continue
        marker = "▶" if key == feature else " "
        lines.append(f"  {marker} {desc}")
    lines.append(f"\n  You hit this because '{friendly}' requires Pro.")
    lines.append(f"\n  → 7-day free trial: {_PRO_CHECKOUT_URL}")
    lines.append(f"  → Already subscribed? Sign in:  {_ACTIVATE_CMD}")

    return {
        "error": "pro_required",
        "feature": feature,
        "message": "\n".join(lines),
        "upgrade_url": _CHECKOUT_URL,
        "activate_command": _ACTIVATE_CMD,
        "free_tier_available": True,
    }


def feature_available(feature: str) -> bool:
    """
    Quick boolean check. Returns True for all free-tier features,
    and for pro features only when the user has a pro/trial license.
    """
    if feature not in PRO_FEATURES:
        return True
    if _is_ungated_now(feature):
        return True
    return get_status().is_pro
