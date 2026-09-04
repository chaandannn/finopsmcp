"""Why team attribution came back empty.

There is a step between "I tagged my resources" and "my bill is grouped by team"
that AWS does not do for you: a tag key has to be **activated as a cost
allocation tag** in Billing before it appears in Cost Explorer or the CUR at all.
Until then the tag is on the resource, visible in the console, and completely
absent from every cost record.

The failure that produces is the worst kind. Nothing errors. Cost Explorer
returns rows, the tag column is simply empty, and every dollar lands in
``unattributed``. A user who has correctly tagged their whole estate sees a
report saying none of it is attributed, with nothing to suggest the fix is two
clicks in a console they were not looking at.

Activation is also retroactive only from the point it is switched on, which is
why saying so early matters: a month of history cannot be recovered later.

Read-only throughout: ``ce:ListCostAllocationTags`` is already in the IAM policy
nable asks for, and nothing here activates anything. Activation is a billing
account change and stays the user's to make.

Goes through ``billing_access.ce_client`` like every other Cost Explorer call, so
it inherits the whole policy rather than restating part of it: refused in demo
mode, refused in scheduled and background work, refused outright under
NABLE_NO_COST_EXPLORER=1. Cost Explorer bills per request, and a diagnostic is
exactly the kind of "surely one more call is fine" that turns into a charge
nobody agreed to. This one runs only when a question a person just asked came
back with nothing attributed, never on a timer and never per row.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def check_aws_tag_activation(tag_keys: list[str]) -> dict[str, Any]:
    """Which of these tag keys AWS will actually put on a cost record.

    Returns ``{"available": bool, "active": [...], "inactive": [...],
    "unknown": [...], "error": str|None}``. ``available`` is False when the
    question could not be asked at all, which is different from "asked, and the
    answer is none": a caller must not tell a user their tags are inactive
    because a permission was missing.
    """
    result: dict[str, Any] = {
        "available": False, "active": [], "inactive": [],
        "unknown": list(tag_keys), "error": None,
    }
    if not tag_keys:
        result["unknown"] = []
        return result

    try:
        from ..billing_access import ce_client

        ce = ce_client(reason="checking which tag keys are activated for cost allocation")
        seen: dict[str, str] = {}
        token = None
        while True:
            kwargs: dict[str, Any] = {"MaxResults": 100}
            if token:
                kwargs["NextToken"] = token
            page = ce.list_cost_allocation_tags(**kwargs)
            for entry in page.get("CostAllocationTags", []) or []:
                key = str(entry.get("TagKey", ""))
                if key:
                    seen[key.lower()] = str(entry.get("Status", "")).lower()
            token = page.get("NextToken")
            if not token:
                break
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never break the answer
        log.debug("cost allocation tag check unavailable: %s", exc)
        result["error"] = str(exc)
        return result

    result["available"] = True
    result["unknown"] = []
    for key in tag_keys:
        status = seen.get(key.lower())
        if status == "active":
            result["active"].append(key)
        elif status is None:
            # Never registered: AWS has not seen this key on any resource, or it
            # is spelled differently there than in the rules.
            result["inactive"].append(key)
        else:
            result["inactive"].append(key)
    return result


def explain_empty_attribution(tag_keys: list[str]) -> str | None:
    """One sentence a user can act on, or None if this is not the problem.

    Deliberately returns None rather than a hedge when the check could not run.
    A guess about someone's billing configuration is worse than saying nothing,
    because they will go and act on it.
    """
    status = check_aws_tag_activation(tag_keys)
    if not status["available"] or not status["inactive"]:
        return None

    inactive = ", ".join(sorted(status["inactive"]))
    active = ", ".join(sorted(status["active"]))
    msg = (
        f"AWS is not putting these tag keys on your cost records: {inactive}. "
        "A tag only reaches Cost Explorer and the CUR once it is activated as a "
        "cost allocation tag in Billing (Billing and Cost Management -> Cost "
        "allocation tags), which is separate from tagging the resource. "
        "Activation applies going forward only, so history before you switch it "
        "on cannot be attributed later."
    )
    if active:
        msg += f" Already active: {active}."
    return msg
