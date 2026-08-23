"""Security hardening for the PR-comment webhook: validate path segments before
they reach api.github.com (CodeQL py/partial-ssrf) and reject malformed input at
the trust boundary, before any HTTP call."""
from __future__ import annotations

import finops.pr_comments.github_app as ga
import finops.pr_comments.webhook as wh


def test_handle_event_rejects_path_injection_in_owner():
    payload = {
        "action": "opened",
        "pull_request": {"number": 1, "head": {"sha": "abc"}},
        "repository": {"owner": {"login": "../../evil"}, "name": "repo"},
        "installation": {"id": 5},
    }
    out = ga.handle_pull_request_event(payload)
    assert out["status"] == "rejected" and "owner/repo" in out["reason"]


def test_handle_event_rejects_non_int_pr_number():
    payload = {
        "action": "opened",
        "pull_request": {"number": "1/../x", "head": {"sha": "abc"}},
        "repository": {"owner": {"login": "acme"}, "name": "infra"},
        "installation": {"id": 5},
    }
    out = ga.handle_pull_request_event(payload)
    assert out["status"] == "rejected"


def test_path_segment_regexes_reject_traversal_and_newlines():
    assert ga._GH_SEGMENT.match("acme-corp_1.2")
    assert not ga._GH_SEGMENT.match("a/b")       # no path separators
    assert not ga._GH_SEGMENT.match("a\nb")      # no CR/LF
    assert wh._GH_REPO.match("acme/infra")
    assert not wh._GH_REPO.match("acme/infra/../x")
    assert not wh._GH_REPO.match("acme")          # must be owner/repo


def test_a_dot_segment_is_rejected_even_though_its_characters_are_legal():
    """The gap the character class left open.

    "." and ".." are built entirely from permitted characters, so they passed
    the regex, and `owner=".."` produced

        https://api.github.com/repos/../{repo}/pulls/{n}/files

    which a URL normaliser resolves upwards into a different endpoint than the
    caller intended. GitHub allows neither name, so refusing them costs nothing.
    """
    assert ga._valid_segment("acme-corp_1.2")
    assert ga._valid_segment("docs.github.com")   # a real repo name, still fine
    for bad in (".", "..", "...", "", None):
        assert not ga._valid_segment(bad), f"{bad!r} was accepted as a path segment"


def test_an_unbounded_segment_cannot_be_used_to_build_an_enormous_url():
    assert ga._valid_segment("a" * 100)
    assert not ga._valid_segment("a" * 101)


def test_handle_event_rejects_a_dot_owner_before_any_http(monkeypatch):
    """The wiring, not the helper. Deleting the guard from handle_pull_request_event
    must fail something, or the validator above is decoration."""
    called = []
    monkeypatch.setattr(ga, "_headers", lambda *a, **k: called.append(1) or {})
    out = ga.handle_pull_request_event({
        "action": "opened",
        "pull_request": {"number": 1, "head": {"sha": "abc"}},
        "repository": {"owner": {"login": ".."}, "name": "infra"},
        "installation": {"id": 5},
    })
    assert out["status"] == "rejected"
    assert called == [], "built auth headers before validating the path segments"


def test_webhook_rejects_bad_repo_before_any_http(monkeypatch):
    calls = []
    monkeypatch.setattr(wh, "_get_pr_files", lambda *a, **k: calls.append(1) or [])
    wh._handle_pr_event({
        "action": "opened",
        "pull_request": {"number": 1},
        "repository": {"full_name": "acme/infra/../../x"},
    })
    assert calls == []  # rejected before fetching anything


def test_verify_signature_is_constant_time_and_correct():
    import hashlib
    import hmac
    secret, body = "s3cr3t", b'{"a":1}'
    good = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert ga.verify_signature(body, good, secret) is True
    assert ga.verify_signature(body, "sha256=deadbeef", secret) is False
    assert ga.verify_signature(body, "", secret) is False
