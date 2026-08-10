"""Security regression: git branch/ref names must never be parseable as git
options. A ref beginning with '-' is read by git as a flag (e.g.
``--upload-pack=<cmd>``), which is an argument-injection -> RCE vector when the
ref is passed as a positional argv token to ``git push`` / ``git checkout``.

Covers the remediation PR tools (open_rightsizing_pr / open_terraform_tag_pr).
"""
import pytest

from finops.remediation.rightsizing_pr import validate_git_ref


@pytest.mark.parametrize(
    "bad",
    [
        "--upload-pack=touch /tmp/pwned",
        "--exec=evil",
        "-x",
        "a..b",
        "branch.lock",
        "has space",
        "semi;colon",
        "back`tick`",
        "pipe|cmd",
        "trailing/",
        "a" * 201,
        "",
    ],
)
def test_rejects_unsafe_git_refs(bad):
    with pytest.raises(ValueError):
        validate_git_ref(bad, "branch")


@pytest.mark.parametrize(
    "good",
    ["main", "nable/rightsizing-fixes", "feature/abc-123", "release_1.2.3", "a/b/c"],
)
def test_accepts_real_git_refs(good):
    validate_git_ref(good, "branch")  # must not raise
