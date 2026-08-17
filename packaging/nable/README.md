# nable

Ask Claude about your cloud and AI bill, right in your editor.

`nable` is the brand alias for [`finops-mcp`](https://pypi.org/project/finops-mcp/).
Installing `nable` installs `finops-mcp` and gives you the `nable` command. All the
code lives in `finops-mcp`.

## Quick start

```sh
uvx nable
```

Run it in a terminal and it walks you through connecting Claude and your first
cloud account, then shows your first cost number. An MCP client (Claude Desktop,
Cursor) runs the same command over stdio to start the server. Any subcommand
routes to the CLI:

```sh
uvx nable setup
uvx nable doctor
uvx nable welcome --demo
```

Local-first: your credentials and raw bill never leave your machine. See
[getnable.com](https://getnable.com) and the
[docs](https://getnable.com/docs).
