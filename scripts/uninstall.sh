#!/usr/bin/env bash
set -euo pipefail
python3 - <<'PY'
import json, pathlib
p=pathlib.Path.home()/'.cursor/mcp.json'
if p.exists():
 d=json.loads(p.read_text()); d.get('mcpServers',{}).pop('cursor-timelog',None); p.write_text(json.dumps(d,indent=2)+'\n')
 print('Removed cursor-timelog MCP entry. Other MCP entries preserved.')
PY
rm -rf "$HOME/.local/share/cursor-timelog-mcp"
echo 'Removed MCP installation. Cursor data and other applications were not touched.'
