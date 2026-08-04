"""What else is attached to this thing.

A cost finding names one resource. The question a reviewer actually has before
acting on it is never "how much does it cost" — the finding already says that.
It is "what breaks if I touch it". Answering that is the difference between a
list of line items and something a human will act on before their coffee.

THE INVARIANT THIS MODULE EXISTS FOR: an empty map must never read as "safe".
"No dependencies found" and "we did not look for dependencies" render
identically in every UI, and only one of them is safe to act on. So every map
declares which relationship classes it checked and which it could not, and
`isolated` returns None — not True — whenever anything was left unexamined. A
caller that wants to say "nothing depends on this" has to get True, and True is
only reachable when the whole checklist ran.

Everything here is derived from data the scanners already collected, or from a
prober the caller supplies. Nothing in this module calls a cloud API itself, so
it stays testable offline and cannot become a surprise credential path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

# Relationship kinds. Deliberately small: each one has to mean something
# specific to a person deciding whether to delete a resource.
ATTACHED_TO = "attached_to"       # live coupling: this volume is on that instance
MEMBER_OF = "member_of"           # managed by: this instance is in that ASG
TARGETS = "targets"               # this load balancer sends traffic to these
DERIVED_FROM = "derived_from"     # this snapshot came from that volume
REFERENCED_BY = "referenced_by"   # infrastructure code / another resource names it
OWNED_BY = "owned_by"             # tag-derived team or service ownership


@dataclass(frozen=True)
class Edge:
    kind: str
    target_type: str
    target_id: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "target_type": self.target_type,
                "target_id": self.target_id, "detail": self.detail}


@dataclass
class ResourceMap:
    resource_id: str
    resource_type: str
    edges: list[Edge] = field(default_factory=list)
    # Relationship classes that SHOULD have been checked for this resource type
    # but could not be. Never silently dropped: an unchecked class is the whole
    # reason `isolated` refuses to say True.
    unexamined: list[str] = field(default_factory=list)

    @property
    def dependencies(self) -> list[Edge]:
        """Edges that represent something breaking. Ownership is excluded: a
        team is a person to notify, not a resource that fails. Counting it as a
        dependency made every tagged resource look coupled to something."""
        return [e for e in self.edges if e.kind != OWNED_BY]

    @property
    def isolated(self) -> bool | None:
        """True: we checked every relationship class and found nothing attached.
        False: something is attached.
        None: we could not check everything, so the question is open.

        Callers must treat None as "unknown", never as False-y "not isolated"
        and never as "safe". The three-state return is the point.
        """
        if self.dependencies:
            return False
        if self.unexamined:
            return None
        return True

    @property
    def blast_radius(self) -> int:
        """How many distinct other resources this touches."""
        return len({(e.target_type, e.target_id) for e in self.dependencies})

    def owners(self) -> list[str]:
        return [e.target_id for e in self.edges if e.kind == OWNED_BY]

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "resource_type": self.resource_type,
            "edges": [e.to_dict() for e in self.edges],
            "unexamined": list(self.unexamined),
            "isolated": self.isolated,
            "blast_radius": self.blast_radius,
        }

    def summary(self) -> str:
        """One line a non-engineer can act on.

        The unchecked note is appended whenever anything was left unexamined,
        INCLUDING when we did find a dependency. Finding one attachment is not
        evidence there are no others: an idle NAT gateway whose Elastic IP we
        found but whose route tables we never read reads as "attached to one
        thing", and deleting it takes a private subnet's egress down.
        """
        if self.isolated is True:
            return "Nothing else references this. Checked every relationship we track."

        parts = []
        for kind in (ATTACHED_TO, MEMBER_OF, TARGETS, DERIVED_FROM, REFERENCED_BY):
            ids = [e.target_id for e in self.dependencies if e.kind == kind]
            if ids:
                parts.append(f"{kind.replace('_', ' ')} {', '.join(ids[:3])}"
                             + (f" (+{len(ids) - 3} more)" if len(ids) > 3 else ""))
        found = "; ".join(parts)

        if not self.unexamined:
            return found or "Attached to other resources."
        missing = ", ".join(self.unexamined)
        if found:
            return f"{found}. Not checked: {missing}."
        return f"No attachments found, but {missing} could not be checked."


class Prober(Protocol):
    """Supplied by the caller when live lookups are available and wanted.

    Returning None means "could not determine", which lands the class in
    `unexamined`. Returning an empty list means "checked, found none", which is
    a real answer and does NOT block `isolated`. That distinction is the entire
    contract; a prober that returns [] on error would silently turn "unknown"
    into "safe to delete".
    """

    def __call__(self, relationship: str, resource_id: str,
                 resource_type: str) -> list[tuple[str, str]] | None: ...


# For each resource type: the relationship classes that must be answered before
# anyone can call it isolated, and where to find each in scanner metadata.
#   (relationship_kind, target_type, metadata_key_or_None)
# A None metadata key means the only way to answer it is a live probe.
_CHECKLIST: dict[str, tuple[tuple[str, str, str | None], ...]] = {
    "ebs_volume": (
        (ATTACHED_TO, "ec2_instance", "attached_to"),
        (DERIVED_FROM, "ebs_snapshot", "snapshot_ids"),
        (REFERENCED_BY, "iac_resource", "iac_references"),
    ),
    "ebs_snapshot": (
        (DERIVED_FROM, "ebs_volume", "source_volume_id"),
        (REFERENCED_BY, "ami", "ami_ids"),
    ),
    "ec2_instance": (
        (MEMBER_OF, "autoscaling_group", "asg_name"),
        (TARGETS, "target_group", "target_group_arns"),
        (ATTACHED_TO, "ebs_volume", "volume_ids"),
        (REFERENCED_BY, "iac_resource", "iac_references"),
    ),
    "elastic_ip": (
        (ATTACHED_TO, "ec2_instance", "instance_id"),
        (ATTACHED_TO, "nat_gateway", "nat_gateway_id"),
    ),
    "load_balancer": (
        (TARGETS, "target_group", "target_group_arns"),
        (REFERENCED_BY, "dns_record", "dns_names"),
    ),
    "rds_instance": (
        (MEMBER_OF, "rds_cluster", "cluster_id"),
        (DERIVED_FROM, "rds_snapshot", "snapshot_ids"),
        (REFERENCED_BY, "iac_resource", "iac_references"),
    ),
    # An idle NAT gateway is one of the most common findings and one of the
    # most dangerous to delete blind: a route table still pointing at it takes
    # a private subnet's egress down the moment it goes.
    "nat_gateway": (
        (REFERENCED_BY, "route_table", "route_table_ids"),
        (ATTACHED_TO, "elastic_ip", "allocation_id"),
        (REFERENCED_BY, "iac_resource", "iac_references"),
    ),
}

# Tag keys that identify a human owner, in priority order.
_OWNER_TAGS = ("Owner", "owner", "Team", "team", "Service", "service",
               "app", "App", "Application")


def _as_ids(value: Any) -> list[str]:
    """Normalize a metadata value into ids, dropping empties and None entries.

    Scanners are inconsistent here: `attached_to` on an unattached volume is
    `[None]`, not `[]`, because it maps over an empty Attachments list. Treating
    that as an edge would invent a dependency on a resource named "None".
    """
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple, set)) else [value]
    out = []
    for v in items:
        if v is None:
            continue
        s = str(v).strip()
        if s and s.lower() not in ("none", "null", ""):
            out.append(s)
    return out


def map_resource(
    finding: dict,
    *,
    prober: Callable[..., list[tuple[str, str]] | None] | None = None,
    resource_type: str | None = None,
) -> ResourceMap:
    """Build the map for one finding from its metadata, plus an optional prober.

    finding: any dict carrying `resource_id` and (usually) `metadata`.
    prober:  called only for checklist classes the metadata cannot answer.
    """
    meta = finding.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    rid = str(finding.get("resource_id") or "")
    rtype = resource_type or str(
        finding.get("resource_type") or meta.get("resource_type") or "")

    rmap = ResourceMap(resource_id=rid, resource_type=rtype)

    checklist = _CHECKLIST.get(rtype)
    if checklist is None:
        # An unknown resource type means we have no checklist to run, so we
        # cannot make any claim about isolation. Say so rather than returning a
        # confident empty map.
        rmap.unexamined.append(f"relationships for resource type '{rtype or 'unknown'}'")
        _add_owner_edges(rmap, meta)
        return rmap

    for kind, target_type, meta_key in checklist:
        ids: list[str] | None = None

        if meta_key and meta_key in meta:
            ids = _as_ids(meta[meta_key])
        elif prober is not None:
            probed = prober(kind, rid, rtype)
            # None means the prober could not answer. [] means it checked and
            # found none. Only the former is unexamined.
            ids = None if probed is None else [i for _, i in probed]

        if ids is None:
            rmap.unexamined.append(f"{kind.replace('_', ' ')} ({target_type})")
            continue
        for i in ids:
            rmap.edges.append(Edge(kind=kind, target_type=target_type, target_id=i))

    _add_owner_edges(rmap, meta)
    return rmap


def _add_owner_edges(rmap: ResourceMap, meta: dict) -> None:
    """Ownership never affects `isolated` (a team is not a dependency), so a
    missing owner is not added to `unexamined`. It is added to the map because
    the first question after "what breaks" is "whose is it"."""
    tags = meta.get("tags")
    if not isinstance(tags, dict):
        return
    # Case-insensitive lookup, first match in priority order wins.
    lowered = {str(k).lower(): v for k, v in tags.items()}
    for key in _OWNER_TAGS:
        val = lowered.get(key.lower())
        if val:
            rmap.edges.append(
                Edge(kind=OWNED_BY, target_type="team", target_id=str(val),
                     detail=f"tag {key}"))
            return
