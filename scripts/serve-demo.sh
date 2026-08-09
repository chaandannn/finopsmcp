#!/usr/bin/env bash
# Run the StreamCo demo dashboard locally, for design and QA passes.
#
# The dashboard lives in the PRIVATE nable-enterprise repo, not here: this repo
# has no finops.server_web (the seam owns it). So this script has to find an
# enterprise checkout before it can serve anything.
#
# It exists because .claude/launch.json used to point straight at a script in a
# session scratchpad directory. Those are per-session and get cleaned up, so the
# preview config broke as soon as the session that created it ended. Anything a
# launch config depends on has to live in the repo.
#
# Checkout resolution, first hit wins:
#   1. $NABLE_ENTERPRISE_DIR
#   2. ~/nable-enterprise
#   3. a sibling ../nable-enterprise
# No auto-clone: cloning a private repo behind someone's back is not this
# script's business. It prints the command instead.
#
#   scripts/serve-demo.sh            # port 8140
#   PORT=8811 scripts/serve-demo.sh
set -euo pipefail

PORT="${PORT:-8140}"
PASSWORD="${FINOPS_DASHBOARD_PASSWORD:-demo-preview}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-$REPO_ROOT/.venv/bin/python}"

[ -x "$PY" ] || { echo "no interpreter at $PY (set PYTHON=...)" >&2; exit 1; }

ENT=""
for cand in "${NABLE_ENTERPRISE_DIR:-}" "$HOME/nable-enterprise" "$REPO_ROOT/../nable-enterprise"; do
  [ -n "$cand" ] || continue
  if [ -f "$cand/nable_enterprise/seam.py" ]; then ENT="$(cd "$cand" && pwd)"; break; fi
done

if [ -z "$ENT" ]; then
  cat >&2 <<'MSG'
Could not find a nable-enterprise checkout, and the dashboard lives there.

  gh repo clone getnable/nable-enterprise ~/nable-enterprise

Then re-run, or point at an existing clone:

  NABLE_ENTERPRISE_DIR=/path/to/nable-enterprise scripts/serve-demo.sh
MSG
  exit 1
fi

echo "enterprise checkout: $ENT"
echo "demo dashboard:      http://127.0.0.1:${PORT}   (password: ${PASSWORD})"

# Demo data only, telemetry off, bound to loopback. FINOPS_DEMO_FORCE makes the
# demo dataset win even when real credentials are present on this machine.
exec env \
  FINOPS_DEMO=1 \
  FINOPS_DEMO_FORCE=1 \
  NABLE_NO_TELEMETRY=1 \
  FINOPS_DASHBOARD_PASSWORD="$PASSWORD" \
  NABLE_ENT_DIR="$ENT" \
  NABLE_DEMO_PORT="$PORT" \
  "$PY" - <<'PY'
import os, sys, threading

sys.path.insert(0, os.environ["NABLE_ENT_DIR"])

import nable_enterprise.seam as seam
seam.install()

import finops.server_web as sw
# The module snapshots the password from the environment at import time; the
# env var is already set above, but pin it so a stale import order cannot leave
# the box open with auth disabled.
sw._DASHBOARD_PASSWORD = os.environ["FINOPS_DASHBOARD_PASSWORD"]

port = int(os.environ["NABLE_DEMO_PORT"])
httpd, actual = sw.start_server_background("127.0.0.1", port)
print(f"serving on http://127.0.0.1:{actual}", flush=True)
threading.Event().wait()
PY
