#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/.local/share/cursor-timelog-mcp"
mkdir -p "$DEST"
cp -a "$ROOT/cursor_timelog" "$ROOT/requirements.txt" "$DEST/"
python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install -r "$DEST/requirements.txt"
mkdir -p "$HOME/.cursor"
CONFIG="$HOME/.cursor/mcp.json"
"$DEST/.venv/bin/python" - "$CONFIG" "$DEST" <<'PY'
import json, pathlib, sys
cfg=pathlib.Path(sys.argv[1]); dest=pathlib.Path(sys.argv[2])
try: data=json.loads(cfg.read_text()) if cfg.exists() else {}
except Exception as e: raise SystemExit(f"Cannot parse {cfg}: {e}")
data.setdefault("mcpServers", {})["cursor-timelog"]={"command":str(dest/".venv/bin/python"),"args":["-m","cursor_timelog.server"],"env":{"PYTHONPATH":str(dest)}}
cfg.parent.mkdir(parents=True, exist_ok=True)
cfg.write_text(json.dumps(data, indent=2)+"\n")
print(f"Configured {cfg}")
PY
cat <<EOF

Installed Cursor Timelog MCP.
1. Restart Cursor completely.
2. Open Cursor Settings > MCP and confirm 'cursor-timelog' is connected.
3. Ask: 'Generate today's timelog for all projects.'

Install path: $DEST
MCP config: $CONFIG
No UI, database, activity collector, background service, or persistent activity log is installed.
EOF
