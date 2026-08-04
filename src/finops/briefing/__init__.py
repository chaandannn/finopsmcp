"""The overnight run and the morning brief.

nable scans while nobody is watching, reviews what it found, ranks it by what
can safely be done today, drafts each change, and has it waiting. It opens
nothing and runs nothing: the brief is a set of decisions, not actions.
"""
from .brief import Brief, BriefItem, build_brief
from .render import to_email, to_html, to_markdown, to_slack_blocks, to_slack_text
from .resource_map import ResourceMap, map_resource

__all__ = ["Brief", "BriefItem", "build_brief", "ResourceMap", "map_resource",
           "to_html", "to_markdown", "to_slack_blocks", "to_slack_text", "to_email"]
