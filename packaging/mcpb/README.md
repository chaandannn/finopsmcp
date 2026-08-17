# nable as a Claude Desktop Extension (MCPB)

This directory holds the [MCPB](https://github.com/modelcontextprotocol/mcpb)
manifest for submitting nable to the **Claude Connectors Directory** as a
desktop extension (the path for local MCP servers; the in-app directory itself
accepts remote servers only).

## Status / what's done

- `manifest.json` (manifest_version 0.3): metadata, `privacy_policies`, and a
  `server` block that runs `uvx nable`.
- Tool annotations: every MCP tool advertises a `title` + `readOnlyHint`
  (or `destructiveHint`), required for directory review. See
  `src/finops/tool_surface.py` (`WRITE_TOOLS` / `tool_annotation`) and
  `tests/test_tool_annotations.py`.
- Privacy policy: section in the top-level `README.md` + full policy at
  https://getnable.com/privacy.

## Before submitting (remaining steps for a human)

1. **Validate**: `npx @anthropic-ai/mcpb validate packaging/mcpb/manifest.json`
   (or `mcpb validate`). Fix anything it flags. The manifest here follows the
   documented 0.3 schema but has not been run through the validator in CI.
2. **Decide the run strategy.** The manifest uses `uvx nable`, which assumes
   `uv` is on the user's machine. If the directory requires a self-contained
   bundle, package the server code under `server/` with a bundled Python per
   the MCPB python-server guide and repoint `mcp_config`.
3. **Bundle**: `mcpb pack` to produce the `.mcpb` file.
4. **Submit**: the desktop-extension form at https://clau.de/desktop-extention-submission
   (no Team/Enterprise org required, unlike the remote-connector portal).
5. Have ready: icon, the privacy policy URL, docs URL, support contact, and a
   test account / instructions detailed enough for a reviewer.

## The other directory (remote connectors)

The in-app Connectors Directory (add-from-claude.ai) accepts **remote MCP
servers only** and submission needs a Claude Team/Enterprise org. That path
depends on the hosted nable (remote MCP endpoint + OAuth) and is tracked
separately with the hosted roadmap.
