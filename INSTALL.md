# Install Cursor Timelog MCP on Ubuntu

Install this first. The Activity Tracker dashboard calls this server when you generate a timelog.

These steps use the project folder:

`/home/mahesh.dave@brainvire.com/htdocs/AI_Implementations/cursor-timelog-mcp`

## 1. Install Ubuntu packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

Python 3.10 or newer is required. `git` is used only to read commit history for the timelog.

## 2. Open the project

```bash
cd /home/mahesh.dave@brainvire.com/htdocs/AI_Implementations/cursor-timelog-mcp
chmod +x scripts/*.sh
```

## 3. Install the MCP server

```bash
./scripts/install.sh
```

This command:

- Copies the server to `~/.local/share/cursor-timelog-mcp`
- Creates a virtual environment and installs `mcp`
- Adds a `cursor-timelog` entry to `~/.cursor/mcp.json`
- Leaves every other MCP entry unchanged

## 4. Restart Cursor

Quit every Cursor window, then open Cursor again.

In Cursor, open **Settings → Tools & MCP** and confirm `cursor-timelog` is connected. If it still shows disconnected, toggle that entry off and on.

## 5. Check that it works

In any Cursor chat, type:

```text
timelog
```

You should get today's work for all projects, with a time and an approximate duration for each task.

To check the data sources from a terminal:

```bash
~/.local/share/cursor-timelog-mcp/.venv/bin/python - << 'PY'
import asyncio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
import os
async def main():
    params = StdioServerParameters(
        command=os.path.expanduser("~/.local/share/cursor-timelog-mcp/.venv/bin/python"),
        args=["-m", "cursor_timelog.server"],
        env={"PYTHONPATH": os.path.expanduser("~/.local/share/cursor-timelog-mcp"), **os.environ},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("check_timelog_sources", {"target_date": "today"})
            print(result.content[0].text)
asyncio.run(main())
PY
```

`cursor_database_found` should be `true`.

## Update after you pull new code

```bash
cd /home/mahesh.dave@brainvire.com/htdocs/AI_Implementations/cursor-timelog-mcp
git pull
./scripts/install.sh
```

Restart Cursor, or toggle `cursor-timelog` off and on.

## Uninstall

```bash
cd /home/mahesh.dave@brainvire.com/htdocs/AI_Implementations/cursor-timelog-mcp
./scripts/uninstall.sh
```

This removes the `cursor-timelog` MCP entry and `~/.local/share/cursor-timelog-mcp`. Other MCP servers and your Cursor chats are not touched. Manual timelog entries stored in that folder are deleted with it.
