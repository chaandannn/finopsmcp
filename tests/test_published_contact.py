"""The address this package hands strangers.

Why this file exists, stated plainly: finops-mcp is public and installable. The
contact in SECURITY.md is what a researcher uses to report a vulnerability, the
one in CODE_OF_CONDUCT.md is where a harassment report goes, the one in the MCPB
manifest ships inside the bundle, and the one in `finops welcome` is printed to
every new user's terminal on first run. All four were a personal Gmail.

That is not a style preference. A security researcher deciding whether to
disclose privately, and an enterprise reviewer reading SECURITY.md before
approving an install, both read a personal Gmail as a signal about how seriously
the project takes the thing the page is about.

The check is on the SHAPE rather than the one address, so pasting a different
personal account back in fails too.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Files that carry an address a stranger is invited to write to.
PUBLISHED = [
    "SECURITY.md",
    ".github/SECURITY.md",
    ".github/CODE_OF_CONDUCT.md",
    "packaging/mcpb/manifest.json",
    "src/finops/welcome.py",
    "src/finops/notifications/onboarding_email.py",
]

CONTACT = "chandan@nable.sh"
_FREEMAIL = re.compile(
    r"[\w.+-]+@(gmail|googlemail|yahoo|hotmail|outlook|icloud|protonmail|aol)\.com",
    re.I,
)


@pytest.mark.parametrize("rel", PUBLISHED)
def test_no_personal_mailbox_in_a_published_file(rel: str) -> None:
    path = ROOT / rel
    assert path.exists(), f"{rel} moved; this list is now checking nothing"
    hits = _FREEMAIL.findall(path.read_text(encoding="utf-8"))
    assert not hits, (
        f"{rel} publishes a personal mailbox. Use {CONTACT}: this file is read "
        f"by people deciding whether to trust the project."
    )


def test_the_security_contact_is_the_one_we_actually_read() -> None:
    """A disclosure address that bounces is worse than none: the researcher
    concludes nobody is home and their next stop may be a public issue."""
    for rel in ("SECURITY.md", ".github/SECURITY.md"):
        assert CONTACT in (ROOT / rel).read_text(encoding="utf-8"), (
            f"{rel} does not name {CONTACT}, so a vulnerability report has "
            f"nowhere to go"
        )


def test_the_sending_identity_did_not_move_with_the_contact_address() -> None:
    """Reply-To and From are different jobs, and only one of them moved.

    Resend/SMTP is verified for getnable.com and signs hello@getnable.com.
    Repointing From at the contact address would break authentication on the
    onboarding mail, and it fails silently: the mail still sends, it just starts
    landing in spam.
    """
    src = (ROOT / "src/finops/notifications/onboarding_email.py").read_text(encoding="utf-8")
    assert 'FINOPS_SMTP_FROM", "hello@getnable.com"' in src, (
        "the default From address moved off the verified sending domain"
    )
    assert f'msg["Reply-To"] = "{CONTACT}"' in src, (
        "replies to the onboarding email no longer reach the contact address"
    )
