"""The single-instance verdict must never claim more than it measured.

Two things this locks down:

1. The old CPU-only path put the STRINGS "high - check instance type for exact
   figure" and "medium - run Compute Optimizer" into an `estimated_monthly_savings`
   field. That is neither a number nor an honest absence of one, and a caller
   formatting it as currency gets nonsense. The heuristic now ships no savings key
   at all.

2. `get_ec2_utilization` already fetches network and disk and the verdict ignored
   them, so an instance at 2% CPU pushing gigabytes a day was called idle. That is
   the classic false positive behind "one bad optimization kills the program".
"""
from __future__ import annotations

from finops.analyzers.optimizer import (
    _cpu_verdict,
    _deep_analysis_verdict,
    _non_cpu_activity,
)


def _util(*, net_in=0, net_out=0, disk_read=0, disk_write=0):
    return {
        "network_in_bytes": {"average_per_day": net_in},
        "network_out_bytes": {"average_per_day": net_out},
        "disk_read_ops": {"average_per_day": disk_read},
        "disk_write_ops": {"average_per_day": disk_write},
    }


# ── activity detection ──────────────────────────────────────────────────────


def test_quiet_instance_has_no_activity_signals():
    assert _non_cpu_activity(_util(net_in=1_000_000, disk_read=500)) == []


def test_heavy_network_is_an_activity_signal():
    signals = _non_cpu_activity(_util(net_in=4_000_000_000, net_out=1_000_000_000))
    assert len(signals) == 1
    assert "GB/day" in signals[0]


def test_heavy_disk_is_an_activity_signal():
    signals = _non_cpu_activity(_util(disk_read=5_000_000, disk_write=1_000_000))
    assert len(signals) == 1
    assert "disk ops/day" in signals[0]


def test_activity_detection_survives_missing_and_bad_values():
    assert _non_cpu_activity({}) == []
    assert _non_cpu_activity({"network_in_bytes": {"average_per_day": None}}) == []
    assert _non_cpu_activity({"disk_read_ops": {"average_per_day": "n/a"}}) == []


# ── the verdict ─────────────────────────────────────────────────────────────


def test_low_cpu_but_busy_network_is_not_called_idle():
    """The load-bearing case. 2% CPU while moving 5 GB/day is a workload that is
    not CPU-bound, not an unused box."""
    activity = _non_cpu_activity(_util(net_in=5_000_000_000))
    v = _cpu_verdict(cpu_avg=2.0, cpu_p99=9.0, lookback_days=14, activity=activity)
    assert v["action"] == "none", "an instance moving 5 GB/day must not be flagged idle"
    assert "not CPU-bound" in v["reason"]
    assert v["confidence"] == "low"


def test_low_cpu_and_quiet_is_worth_investigating():
    v = _cpu_verdict(cpu_avg=2.0, cpu_p99=9.0, lookback_days=14, activity=[])
    assert v["action"] == "investigate_for_stop"


def test_moderate_cpu_and_quiet_suggests_downsize():
    v = _cpu_verdict(cpu_avg=12.0, cpu_p99=30.0, lookback_days=14, activity=[])
    assert v["action"] == "investigate_for_downsize"


def test_bursty_instance_is_left_alone():
    """Low average, high p99: it needs the headroom at peak."""
    v = _cpu_verdict(cpu_avg=12.0, cpu_p99=95.0, lookback_days=14, activity=[])
    assert v["action"] == "none"


def test_busy_instance_is_left_alone():
    v = _cpu_verdict(cpu_avg=65.0, cpu_p99=90.0, lookback_days=14, activity=[])
    assert v["action"] == "none"


def test_heuristic_never_ships_a_savings_figure():
    """The regression that motivated this file: no string in a money field, and no
    invented number either."""
    for cpu_avg, cpu_p99, activity in [
        (2.0, 9.0, []),
        (12.0, 30.0, []),
        (65.0, 90.0, []),
        (2.0, 9.0, ["moving ~5.0 GB/day of network traffic"]),
    ]:
        v = _cpu_verdict(cpu_avg, cpu_p99, 14, activity)
        assert "estimated_monthly_savings" not in v, (
            f"CPU heuristic invented a savings figure: {v.get('estimated_monthly_savings')!r}"
        )


def test_every_actionable_heuristic_verdict_explains_its_blind_spot():
    for cpu_avg, cpu_p99 in [(2.0, 9.0), (12.0, 30.0)]:
        v = _cpu_verdict(cpu_avg, cpu_p99, 14, [])
        assert v["kind"] == "investigation"
        assert "memory" in v["why_unsure"].lower()
        assert v["confirm_steps"]


# ── reconciling the two sources ─────────────────────────────────────────────


_CO = {
    "finding": "OVER_PROVISIONED",
    "options": [{
        "instance_type": "m6i.large",
        "performance_risk": 1.0,
        "estimated_monthly_savings": 118.40,
    }],
}


def test_compute_optimizer_wins_and_is_measured():
    ours = _cpu_verdict(2.0, 9.0, 14, [])
    v = _deep_analysis_verdict(ours, _CO)
    assert v["source"] == "aws_compute_optimizer"
    assert v["kind"] == "recommendation"
    assert v["estimated_monthly_savings"] == 118.40
    assert v["recommended_instance_type"] == "m6i.large"


def test_falls_back_to_the_heuristic_when_compute_optimizer_is_silent():
    ours = _cpu_verdict(2.0, 9.0, 14, [])
    for co in (None, {}, {"finding": "OPTIMIZED", "options": []}):
        v = _deep_analysis_verdict(ours, co)
        assert v["source"] == "nable_cpu_heuristic"
        assert v["kind"] == "investigation"
        assert "estimated_monthly_savings" not in v


def test_compute_optimizer_without_a_figure_does_not_become_a_recommendation():
    """An option with no savings value is not measured evidence, so it must not be
    promoted over our own honest investigation."""
    co = {"finding": "OVER_PROVISIONED",
          "options": [{"instance_type": "m6i.large", "estimated_monthly_savings": None}]}
    v = _deep_analysis_verdict(_cpu_verdict(2.0, 9.0, 14, []), co)
    assert v["source"] == "nable_cpu_heuristic"
    assert v["kind"] == "investigation"


# ── the wiring ──────────────────────────────────────────────────────────────
#
# Everything above calls _cpu_verdict directly with a pre-built activity list, so
# none of it covers `activity = _non_cpu_activity(utilization)` in
# get_instance_deep_analysis. Reverting that line to `activity = []` left all of
# the tests above green. This exercises the real function so the connection
# between the fetched metrics and the verdict is actually held.


class _FakeClient:
    def get_caller_identity(self):
        return {"Account": "123456789012"}

    def get_ec2_instance_recommendations(self, **kw):
        raise RuntimeError("Compute Optimizer not enabled")


class _FakeSession:
    def client(self, name, **kw):
        return _FakeClient()


def test_deep_analysis_wires_fetched_metrics_into_the_verdict(monkeypatch):
    import finops.analyzers.cloudwatch as cw
    import finops.analyzers.optimizer as opt

    busy_network = {
        "instance_id": "i-busy",
        "instance_type": "m5.xlarge",
        "cpu": {"average": 2.0, "p99": 8.0},
        # 5 GB/day out: this box is working, it just is not CPU-bound.
        "network_in_bytes": {"average_per_day": 1_000_000_000},
        "network_out_bytes": {"average_per_day": 4_000_000_000},
        "disk_read_ops": {"average_per_day": 10},
        "disk_write_ops": {"average_per_day": 10},
    }
    monkeypatch.setattr(opt, "_get_boto3_session", lambda *a, **k: _FakeSession())
    monkeypatch.setattr(cw, "get_ec2_utilization", lambda *a, **k: busy_network)

    out = opt.get_instance_deep_analysis("i-busy", region="us-east-1")

    verdict = out["verdict"]
    assert verdict["source"] == "nable_cpu_heuristic"
    assert verdict["action"] == "none", (
        "2% CPU while moving 5 GB/day was reported as idle; the network signal is "
        "fetched but not reaching the verdict"
    )
    assert "not CPU-bound" in verdict["reason"]
    assert "estimated_monthly_savings" not in verdict


def test_deep_analysis_flags_a_genuinely_quiet_instance(monkeypatch):
    import finops.analyzers.cloudwatch as cw
    import finops.analyzers.optimizer as opt

    quiet = {
        "instance_id": "i-quiet",
        "cpu": {"average": 1.5, "p99": 4.0},
        "network_in_bytes": {"average_per_day": 2_000_000},
        "network_out_bytes": {"average_per_day": 1_000_000},
        "disk_read_ops": {"average_per_day": 100},
        "disk_write_ops": {"average_per_day": 50},
    }
    monkeypatch.setattr(opt, "_get_boto3_session", lambda *a, **k: _FakeSession())
    monkeypatch.setattr(cw, "get_ec2_utilization", lambda *a, **k: quiet)

    verdict = opt.get_instance_deep_analysis("i-quiet", region="us-east-1")["verdict"]
    assert verdict["action"] == "investigate_for_stop"
    assert verdict["kind"] == "investigation"
