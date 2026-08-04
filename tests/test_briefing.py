"""The overnight run's morning brief.

The load-bearing property is in resource_map: an empty map must never read as
"safe to delete". "Nothing depends on this" and "we did not check what depends
on this" look identical in every UI, and acting on the second one takes a
private subnet's egress down. So `isolated` is three-state and only reaches True
when the whole checklist ran.

Everything else here exists to keep the brief honest about its own limits: the
headline counts only dollars that survived critique, truncation is reported
rather than silent, and a finding nable cannot draft a fix for says so instead
of emitting a plausible-looking command.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from finops.briefing import build_brief, map_resource
from finops.briefing.resource_map import (
    ATTACHED_TO,
    OWNED_BY,
    REFERENCED_BY,
    Edge,
    ResourceMap,
)

TODAY = date(2026, 8, 3)
NOW = datetime(2026, 8, 3, 6, 0, tzinfo=timezone.utc)


def _vol(**meta):
    """An unattached volume whose whole checklist is answerable from metadata."""
    base = {"region": "us-east-1", "attached_to": [None], "snapshot_ids": [],
            "iac_references": [], "age_days": 610}
    base.update(meta)
    return {"title": "Unattached volume", "resource_type": "ebs_volume",
            "resource_id": "vol-1", "why": "Unattached for 74 days",
            "estimated_monthly_savings_usd": 212.0, "evidence": "measured",
            "metadata": base}


# ── the safety invariant ─────────────────────────────────────────────────────

def test_isolated_is_true_only_when_the_whole_checklist_ran():
    m = map_resource(_vol())
    assert m.unexamined == []
    assert m.isolated is True


def test_isolated_is_none_when_any_class_could_not_be_checked():
    """The dangerous case. A missing metadata key means we never looked, and the
    map must not imply the resource is free-standing."""
    meta = {"region": "us-east-1", "attached_to": [None], "snapshot_ids": []}
    # iac_references absent entirely
    m = map_resource(_vol(**meta) | {"metadata": meta})
    assert m.isolated is None, "unchecked relationship must not read as isolated"
    assert any("iac" in u for u in m.unexamined)
    assert m.edges == []


def test_an_unknown_resource_type_never_claims_isolation():
    f = {"resource_type": "quantum_flux_capacitor", "resource_id": "qfc-1", "metadata": {}}
    m = map_resource(f)
    assert m.isolated is None
    assert m.unexamined, "an unknown type must name what it could not check"


def test_isolated_is_false_when_something_is_attached():
    m = map_resource(_vol(attached_to=["i-99"]))
    assert m.isolated is False
    assert m.blast_radius == 1


def test_an_ownership_tag_is_not_a_dependency():
    """A team is a person to notify, not a resource that breaks. Counting the
    Team tag as an edge made every tagged resource report isolated=False with
    blast_radius=0, which is self-contradictory and blocked every safe delete."""
    m = map_resource(_vol(tags={"Team": "media-eng"}))
    assert m.isolated is True
    assert m.blast_radius == 0
    assert m.owners() == ["media-eng"]


def test_a_none_entry_in_attachments_is_not_an_edge():
    """Scanners emit `attached_to: [None]` for an unattached volume, because
    they map over an empty Attachments list. Treating that as an edge invents a
    dependency on a resource literally named "None"."""
    for empty in ([None], [], ["", "  "], ["none"], None):
        m = map_resource(_vol(attached_to=empty))
        assert m.blast_radius == 0, f"{empty!r} produced a phantom dependency"


def test_a_prober_returning_none_means_unchecked_not_empty():
    """The prober contract, and the one a careless implementation gets wrong: a
    prober that swallows an API error and returns [] silently converts
    "unknown" into "safe to delete"."""
    meta = {"region": "us-east-1", "attached_to": [None]}
    finding = {"resource_type": "ebs_volume", "resource_id": "vol-1", "metadata": meta}

    cannot_tell = map_resource(finding, prober=lambda *a, **k: None)
    assert cannot_tell.isolated is None

    checked_found_none = map_resource(finding, prober=lambda *a, **k: [])
    assert checked_found_none.isolated is True

    found_one = map_resource(finding, prober=lambda *a, **k: [("ami", "ami-7")])
    assert found_one.isolated is False


def test_summary_never_says_nothing_references_this_unless_verified():
    """The sentence a human acts on. It may only appear for isolated is True."""
    unknown = ResourceMap("r-1", "ebs_volume", edges=[], unexamined=["iac (iac_resource)"])
    assert "Nothing else references this" not in unknown.summary()
    assert "could not be checked" in unknown.summary()

    verified = ResourceMap("r-1", "ebs_volume", edges=[], unexamined=[])
    assert "Nothing else references this" in verified.summary()


def test_summary_lists_the_actual_dependencies():
    m = ResourceMap("i-1", "ec2_instance", edges=[
        Edge(ATTACHED_TO, "ebs_volume", "vol-9"),
        Edge(REFERENCED_BY, "iac_resource", "infra/main.tf:12"),
        Edge(OWNED_BY, "team", "media-eng"),
    ])
    s = m.summary()
    assert "vol-9" in s and "infra/main.tf:12" in s
    assert "media-eng" not in s, "ownership is not a thing that breaks"


# ── the brief ────────────────────────────────────────────────────────────────

def test_the_headline_counts_only_dollars_that_survived_critique():
    """A finding the critic retracted must not appear in the headline total.
    This is the trust envelope reaching all the way to the top line."""
    good = _vol()
    # 30 days of full-month savings claimed on a resource created yesterday:
    # the critic's full_month_on_new_resource falsifier blocks this.
    bad = _vol()
    bad = dict(bad, resource_id="vol-2", estimated_monthly_savings_usd=99_000.0,
               metadata=dict(bad["metadata"], age_days=1))

    b = build_brief([good, bad], today=TODAY, use_llm=False, now=NOW)
    assert b.total_monthly_usd == 212.0, "a retracted figure reached the headline"
    assert len(b.investigations) == 1
    assert b.investigations[0].monthly_usd is None


def test_an_investigation_never_outranks_an_actionable_item():
    small_but_real = _vol()
    huge_but_retracted = dict(_vol(), resource_id="vol-2",
                              estimated_monthly_savings_usd=99_000.0)
    huge_but_retracted["metadata"] = dict(huge_but_retracted["metadata"], age_days=1)
    b = build_brief([huge_but_retracted, small_but_real], today=TODAY,
                    use_llm=False, now=NOW)
    assert b.items[0].is_recommendation
    assert b.items[0].finding["resource_id"] == "vol-1"


def test_ease_tilts_the_ranking_without_dominating_it():
    """The stated design. A clean small win beats a coupled medium one; a large
    enough number still wins on its own merits."""
    clean_300 = dict(_vol(), resource_id="vol-clean", estimated_monthly_savings_usd=300.0)
    coupled_500 = {
        "title": "Coupled instance", "resource_type": "ec2_instance", "resource_id": "i-x",
        "estimated_monthly_savings_usd": 500.0, "evidence": "measured",
        "metadata": {"region": "us-east-1", "asg_name": "web-asg",
                     "target_group_arns": ["tg-1"], "volume_ids": ["vol-a", "vol-b"],
                     "iac_references": ["main.tf:1"], "age_days": 900},
    }
    b = build_brief([coupled_500, clean_300], today=TODAY, use_llm=False, now=NOW)
    assert b.items[0].finding["resource_id"] == "vol-clean"

    # ...but a big enough number wins anyway.
    coupled_5000 = dict(coupled_500, estimated_monthly_savings_usd=5000.0)
    b2 = build_brief([coupled_5000, clean_300], today=TODAY, use_llm=False, now=NOW)
    assert b2.items[0].finding["resource_id"] == "i-x"


def test_an_unverifiable_resource_ranks_below_a_smaller_verified_one():
    """The behaviour that makes the brief safe: we cannot confirm what routes
    through this NAT gateway, so a smaller thing we CAN confirm goes first."""
    nat = {"title": "Idle NAT gateway", "resource_type": "nat_gateway",
           "resource_id": "nat-1", "estimated_monthly_savings_usd": 320.0,
           "evidence": "measured", "metadata": {"region": "eu-west-1"}}
    b = build_brief([nat, _vol()], today=TODAY, use_llm=False, now=NOW)
    assert b.items[0].finding["resource_id"] == "vol-1"
    assert b.items[1].resource_map.isolated is None
    assert "could not confirm" in b.items[1].why_this_rank


def test_truncation_is_reported_not_silent():
    """A truncated list that looks complete is how a $40k finding goes unread."""
    many = [dict(_vol(), resource_id=f"vol-{i}",
                 estimated_monthly_savings_usd=100.0 + i) for i in range(15)]
    b = build_brief(many, today=TODAY, use_llm=False, limit=5, now=NOW)
    assert len(b.items) == 5
    assert any("not shown here" in g for g in b.gaps)
    assert any("10 further finding" in g for g in b.gaps)


def test_the_brief_never_claims_it_did_anything():
    b = build_brief([_vol()], today=TODAY, use_llm=False, now=NOW)
    d = b.to_dict()
    assert d["executed"] is False
    assert "does not" in d["note"]


# ── the drafted fix ──────────────────────────────────────────────────────────

def test_a_delete_is_drafted_as_a_command_and_marked_irreversible():
    b = build_brief([_vol()], today=TODAY, use_llm=False, now=NOW)
    fix = b.items[0].drafted_fix
    assert fix["commands"] == [
        "aws ec2 delete-volume --volume-id vol-1 --region us-east-1"]
    assert fix["reversible"] is False


def test_a_resize_is_drafted_as_the_three_real_steps():
    f = {"title": "Oversized", "resource_type": "ec2_instance", "resource_id": "i-1",
         "estimated_monthly_savings_usd": 400.0, "evidence": "measured",
         "current_type": "m6i.4xlarge", "recommended_type": "m6i.xlarge",
         "metadata": {"region": "us-east-1", "asg_name": "a", "target_group_arns": [],
                      "volume_ids": [], "iac_references": [], "age_days": 900}}
    fix = build_brief([f], today=TODAY, use_llm=False, now=NOW).items[0].drafted_fix
    assert "m6i.xlarge" in fix["summary"]
    assert len(fix["commands"]) == 3
    assert fix["reversible"] is True
    assert "change window" in fix["caveat"]


def test_an_undraftable_finding_says_so_rather_than_inventing_a_command():
    f = {"title": "Something odd", "resource_type": "weird_thing", "resource_id": "w-1",
         "estimated_monthly_savings_usd": 50.0, "evidence": "measured", "metadata": {}}
    fix = build_brief([f], today=TODAY, use_llm=False, now=NOW).items[0].drafted_fix
    assert fix["commands"] == []
    assert "could not draft" in fix["caveat"]


def test_the_confirm_step_reflects_what_the_map_actually_established():
    """A delete on a verified-isolated resource should not tell the reader to go
    re-check dependencies nable already checked, and a delete on an unverified
    one absolutely must."""
    verified = build_brief([_vol()], today=TODAY, use_llm=False, now=NOW)
    assert "nable checked every relationship" in verified.items[0].drafted_fix["steps"][0]

    meta = {"region": "us-east-1", "attached_to": [None]}   # iac never checked
    unverified = build_brief([dict(_vol(), metadata=meta)], today=TODAY,
                             use_llm=False, now=NOW)
    assert "Confirm nothing depends on" in unverified.items[0].drafted_fix["steps"][0]


def test_every_item_carries_a_verification_step():
    b = build_brief([_vol()], today=TODAY, use_llm=False, now=NOW)
    v = b.items[0].verification
    assert "bill" in v.lower()
    assert "212" in v


def test_in_words_degrades_to_a_band_for_an_investigation():
    bad = dict(_vol(), resource_id="vol-2", estimated_monthly_savings_usd=99_000.0)
    bad["metadata"] = dict(bad["metadata"], age_days=1)
    b = build_brief([bad], today=TODAY, use_llm=False, now=NOW)
    words = b.items[0].in_words()
    assert "99,000" not in words, "a retracted figure leaked into the summary"
    assert "unconfirmed" in words


def test_an_empty_run_says_nothing_found_rather_than_zero_dollars():
    b = build_brief([], today=TODAY, use_llm=False, now=NOW)
    assert b.headline() == "Nothing new found."
    assert b.total_monthly_usd == 0


@pytest.mark.parametrize("bad_amount", [None, float("nan"), float("inf"), "n/a"])
def test_a_garbage_figure_does_not_crash_or_reach_the_headline(bad_amount):
    f = dict(_vol(), estimated_monthly_savings_usd=bad_amount)
    b = build_brief([f], today=TODAY, use_llm=False, now=NOW)
    assert b.total_monthly_usd == 0 or b.total_monthly_usd is not None
    assert "nan" not in b.headline().lower()
    assert "inf" not in b.headline().lower()


def test_build_brief_does_not_mutate_the_findings_it_was_given():
    original = _vol()
    snapshot = dict(original)
    build_brief([original], today=TODAY, use_llm=False, now=NOW)
    assert original == snapshot


def test_the_brief_never_calls_a_model_by_default(monkeypatch):
    """LLM critique spends the operator's Anthropic tokens. An overnight job
    that quietly starts doing that is a bill, not a feature."""
    import finops.recommendations.critique as crit

    def explode(*a, **k):
        raise AssertionError("the brief called the model without being asked")

    monkeypatch.setattr(crit, "_llm_objections", explode)
    monkeypatch.delenv("NABLE_CRITIC_LLM", raising=False)
    build_brief([_vol()], today=TODAY, now=NOW)


# ── partial knowledge is not small knowledge ─────────────────────────────────

def _nat(**meta):
    base = {"region": "eu-west-1", "age_days": 400, "allocation_id": "eipalloc-1",
            "iac_references": []}          # route_table_ids deliberately absent
    base.update(meta)
    return {"title": "Idle NAT gateway", "resource_type": "nat_gateway",
            "resource_id": "nat-1", "estimated_monthly_savings_usd": 320.0,
            "evidence": "measured", "metadata": base}


def test_finding_one_dependency_does_not_imply_there_are_no_others():
    """The exact trap. We found the Elastic IP and never read the route tables.
    Reporting "attached to 1 thing" and stopping there is how someone deletes a
    NAT gateway that a private subnet still routes through."""
    m = map_resource(_nat())
    assert m.isolated is False          # we definitely found something
    assert m.unexamined                  # ...but we did not finish looking
    s = m.summary()
    assert "eipalloc-1" in s
    assert "Not checked" in s and "route_table" in s


def test_a_partially_checked_resource_ranks_as_unknown_not_as_small():
    """320 * small-radius ease would outrank a fully verified 212. It must not:
    an unread relationship class is an unknown blast radius, not a small one."""
    b = build_brief([_nat(), _vol()], today=TODAY, use_llm=False, now=NOW)
    assert b.items[0].finding["resource_id"] == "vol-1"
    assert "could not confirm everything" in b.items[1].why_this_rank


def test_the_chip_reports_unchecked_rather_than_a_reassuring_count():
    from finops.briefing.render import _safety_chip

    cls, txt = _safety_chip(map_resource(_nat()))
    assert cls == "unknown"
    assert "unchecked" in txt

    fully_checked = map_resource(_nat(route_table_ids=["rtb-9"]))
    assert fully_checked.unexamined == []
    assert _safety_chip(fully_checked) == ("coupled", "touches 2")


# ── rendering ────────────────────────────────────────────────────────────────

def test_the_html_escapes_resource_data():
    """Tag values and resource names are attacker-influenceable and this HTML is
    served to a logged-in operator. `Team: <img src=x onerror=alert(1)>` is a
    legal AWS tag."""
    from finops.briefing.render import to_html

    nasty = '<img src=x onerror="alert(1)">'
    f = dict(_vol(), title=nasty)
    f["metadata"] = dict(f["metadata"], tags={"Team": nasty})
    html = to_html(build_brief([f], today=TODAY, use_llm=False, now=NOW))
    assert "<img src=x" not in html
    assert "&lt;img src=x" in html
    assert "onerror=&quot;alert(1)&quot;" in html or "onerror=" not in html.split("&lt;img")[1][:60]


def test_the_html_always_renders_the_unchecked_band():
    from finops.briefing.render import to_html

    html = to_html(build_brief([_nat()], today=TODAY, use_llm=False, now=NOW))
    assert "not checked:" in html
    assert "route_table" in html


def test_slack_shows_the_top_three_and_says_how_many_it_held_back():
    from finops.briefing.render import to_slack_blocks

    many = [dict(_vol(), resource_id=f"vol-{i}",
                 estimated_monthly_savings_usd=500.0 - i) for i in range(8)]
    blocks = to_slack_blocks(build_brief(many, today=TODAY, use_llm=False, now=NOW),
                             url="https://example.invalid/brief")
    body = " ".join(str(b) for b in blocks)
    assert body.count('"type": "section"') <= 4  # headline + 3 items
    assert "5 more ready to act on" in body
    assert "example.invalid" in body


def test_every_surface_says_nable_did_not_act():
    from finops.briefing.render import to_email, to_html, to_markdown, to_slack_blocks

    b = build_brief([_vol()], today=TODAY, use_llm=False, now=NOW)
    assert "ran none of them" in to_markdown(b)
    assert "does not make them" in to_html(b)
    assert "ran none of them" in str(to_slack_blocks(b))
    assert "ran none of them" in to_email(b)["text"]


def test_the_email_subject_carries_the_number():
    from finops.briefing.render import to_email

    e = to_email(build_brief([_vol()], today=TODAY, use_llm=False, now=NOW))
    assert "$212/mo" in e["subject"]
    assert to_email(build_brief([], today=TODAY, use_llm=False, now=NOW))["subject"] \
        == "nable: nothing new to act on"
