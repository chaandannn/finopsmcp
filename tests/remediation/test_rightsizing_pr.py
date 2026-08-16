"""Tests for finops.remediation.rightsizing_pr."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from finops.remediation.rightsizing_pr import open_rightsizing_pr


# ── fixtures ──────────────────────────────────────────────────────────────────

def _make_tf(tmp_path: Path, resource_type: str = "aws_instance", resource_name: str = "web") -> Path:
    tf = tmp_path / "main.tf"
    tf.write_text(
        f'resource "{resource_type}" "{resource_name}" {{\n'
        f'  instance_type = "m5.xlarge"\n'
        f'}}\n'
    )
    return tf


def _make_row(
    row_id: int = 1,
    resource_id: str = "i-abc123",
    resource_name: str = "web",
    savings: float = 100.0,
    rec_cfg: dict | None = None,
) -> SimpleNamespace:
    cfg = rec_cfg or {
        "tf_resource_type": "aws_instance",
        "tf_resource_name": "web",
        "instance_type": "m5.large",
        "from_instance_type": "m5.xlarge",
    }
    return SimpleNamespace(
        id=row_id,
        resource_id=resource_id,
        resource_name=resource_name,
        estimated_monthly_savings_usd=savings,
        recommended_config=json.dumps(cfg),
    )


# ── helpers ───────────────────────────────────────────────────────────────────

def _patch_db(rows: list) -> Any:
    """Return a context-manager patch that stubs the DB engine."""
    mock_conn = MagicMock()
    mock_conn.__enter__ = lambda s: mock_conn
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.execute.return_value.fetchall.return_value = rows

    mock_engine = MagicMock()
    mock_engine.connect.return_value = mock_conn
    return patch("finops.remediation.rightsizing_pr.get_engine", return_value=mock_engine)


# ── Test: dry_run=True returns diffs without writing or calling git ────────────

def test_dry_run_returns_diffs_without_writing(tmp_path: Path) -> None:
    tf = _make_tf(tmp_path)
    row = _make_row()

    with _patch_db([row]), \
         patch("finops.remediation.rightsizing_pr.build_id_map", return_value={}), \
         patch("finops.remediation.rightsizing_pr.resolve_recommendation", return_value=None), \
         patch("finops.remediation.rightsizing_pr.find_resource_file", return_value=str(tf)), \
         patch("finops.remediation.rightsizing_pr.run_git") as mock_git:

        result = open_rightsizing_pr(
            tf_dir=str(tmp_path),
            dry_run=True,
        )

    assert result.get("dry_run") is True
    assert "diffs" in result
    # At least one file has a diff
    assert len(result["diffs"]) >= 1
    # Git must not have been called
    mock_git.assert_not_called()
    # File on disk must be unchanged
    assert "m5.xlarge" in tf.read_text()


# ── Test: patch_only=True writes files but does not call git ──────────────────

def test_patch_only_writes_files_without_git(tmp_path: Path) -> None:
    tf = _make_tf(tmp_path)
    row = _make_row()

    with _patch_db([row]), \
         patch("finops.remediation.rightsizing_pr.build_id_map", return_value={}), \
         patch("finops.remediation.rightsizing_pr.resolve_recommendation", return_value=None), \
         patch("finops.remediation.rightsizing_pr.find_resource_file", return_value=str(tf)), \
         patch("finops.remediation.rightsizing_pr.mark_acted_on", return_value=True), \
         patch("finops.remediation.rightsizing_pr.run_git") as mock_git:

        result = open_rightsizing_pr(
            tf_dir=str(tmp_path),
            patch_only=True,
        )

    assert result.get("patch_only") is True
    assert str(tf) in result.get("files_modified", [])
    # File on disk must have the new instance type
    assert "m5.large" in tf.read_text()
    assert "m5.xlarge" not in tf.read_text()
    # Git must not have been called
    mock_git.assert_not_called()


# ── Test: resource resolution falls through to recommended_config in DB ───────

def test_resource_resolved_from_recommended_config(tmp_path: Path) -> None:
    tf = _make_tf(tmp_path, resource_type="aws_instance", resource_name="api")
    cfg = {
        "tf_resource_type": "aws_instance",
        "tf_resource_name": "api",
        "instance_type": "t3.small",
        "from_instance_type": "t3.medium",
    }
    # Write tf matching the resource name
    tf.write_text(
        'resource "aws_instance" "api" {\n'
        '  instance_type = "t3.medium"\n'
        '}\n'
    )
    row = _make_row(rec_cfg=cfg, resource_name="api")

    with _patch_db([row]), \
         patch("finops.remediation.rightsizing_pr.build_id_map", side_effect=RuntimeError("no state")), \
         patch("finops.remediation.rightsizing_pr.find_resource_file", return_value=str(tf)), \
         patch("finops.remediation.rightsizing_pr.mark_acted_on", return_value=True), \
         patch("finops.remediation.rightsizing_pr.run_git"):

        result = open_rightsizing_pr(
            tf_dir=str(tmp_path),
            patch_only=True,
        )

    assert "error" not in result or result.get("files_modified")
    assert "t3.small" in tf.read_text()


# ── Test: no patchable recs returns error dict ────────────────────────────────

def test_no_open_recommendations_returns_error() -> None:
    with _patch_db([]):
        result = open_rightsizing_pr(tf_dir="/tmp/fake", dry_run=True)

    assert "error" in result
    assert result.get("pr_url") is None


# ── Test: the defensible path — resolve resource_id from real Terraform state ──

def test_resolves_from_terraform_state_without_cfg_tf_fields(tmp_path: Path) -> None:
    """The moat path: recommended_config carries NO tf address; nable resolves the
    cloud resource_id to its .tf address from a real terraform.tfstate and patches
    it. Uses the REAL build_id_map / resolve_recommendation / find_resource_file
    (not mocked), so a regression here silently breaks the find->fix loop."""
    tf = tmp_path / "main.tf"
    tf.write_text('resource "aws_instance" "api" {\n  instance_type = "m5.xlarge"\n}\n')
    state = {"version": 4, "resources": [
        {"type": "aws_instance", "name": "api", "instances": [
            {"attributes": {"id": "i-realstate", "instance_type": "m5.xlarge"}}]}]}
    (tmp_path / "terraform.tfstate").write_text(json.dumps(state))

    # No tf_resource_type/name in the rec — it must come from state resolution.
    row = _make_row(
        resource_id="i-realstate", resource_name="api",
        rec_cfg={"instance_type": "m5.large", "from_instance_type": "m5.xlarge"},
    )

    with _patch_db([row]), \
         patch("finops.remediation.rightsizing_pr.mark_acted_on", return_value=True), \
         patch("finops.remediation.rightsizing_pr.run_git"):
        result = open_rightsizing_pr(tf_dir=str(tmp_path), patch_only=True)

    assert result.get("files_modified"), result
    assert "m5.large" in tf.read_text()
    assert "m5.xlarge" not in tf.read_text()


# ── Test: the verification step is scheduled (closes the find->fix->prove loop) ─





# ── Test: skipped when tf_resource_type is missing ────────────────────────────

def test_skips_rec_missing_tf_resource_type(tmp_path: Path) -> None:
    # recommended_config has no tf_resource_type
    row = _make_row(rec_cfg={"instance_type": "m5.large"})

    with _patch_db([row]), \
         patch("finops.remediation.rightsizing_pr.build_id_map", return_value={}), \
         patch("finops.remediation.rightsizing_pr.resolve_recommendation", return_value=None):

        result = open_rightsizing_pr(tf_dir=str(tmp_path), dry_run=True)

    # The rec should be skipped, not patched
    assert "error" in result or len(result.get("diffs", {})) == 0
    skipped = result.get("skipped", [])
    assert any("tf_resource_type" in s.get("reason", "") for s in skipped)
