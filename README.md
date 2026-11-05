# Cursor Timelog MCP v2.0

A read-only, on-demand MCP server for Cursor on Ubuntu. Type **`timelog`** and it returns today's work for **all projects**, built from what really happened in the IDE, not just Git.

## Where the data comes from

| Source | What it provides |
|---|---|
| **Cursor chat history** (`globalStorage/state.vscdb`) | Every message with its real timestamp, what you asked, what the assistant answered, files edited, tools used, chat title. Covers research, analysis, documentation, debugging, testing and discussions. |
| Agent transcripts (`~/.cursor/projects/*/agent-transcripts`) | Maps each chat to its project; used as a fallback when a chat is missing from the database. |
| Git commits | Your commits, shown as extra evidence (and as their own item when they happened outside any chat). |
| Files edited on disk | Manual editing done without a chat (single saves and bulk checkouts are ignored). |
| Cursor plans / canvases | Documents produced that day. |
| **Manual entries** (`add_manual_entry`) | Web research, meetings, calls, anything no tool can see. |

Nothing runs in the background. The only thing written to disk is `~/.local/share/cursor-timelog-mcp/data/manual_entries.json`, and only when you add a manual entry.

## How hours are calculated

* A task is one chat (or one project inside a chat, based on the files edited).
* Time = first to last message in a work session. A pause longer than 30 minutes ends the session (`TIMELOG_IDLE_GAP_MINUTES`).
* Each task is rounded to the nearest 5 minutes, minimum 5 minutes. Overlapping chats in the same project are never counted twice.
* Requests such as "timelog" or complaints about the timelog itself are not logged as work.
* Commits or file saves outside any chat are measured first-to-last when at least 10 minutes apart; otherwise the item shows **To be confirmed**.
* Hours are approximate by nature. Use `add_manual_entry` or `generate_timelog(manual_hours=...)` for exact values.

## Tools

| Tool | Purpose |
|---|---|
| `get_timelog(request)` | Main entry. `timelog`, `today's timelog`, `timelog for yesterday`, `timelog for 2026-11-03`, `whohasit2024 timelog`, `R&D timelog only`. Default is today, all projects. |
| `generate_timelog(target_date, project, manual_hours)` | Structured version; `manual_hours='whohasit2024=2.5'` overrides a project total. |
| `add_manual_entry(description, project, target_date, start_time, end_time, hours, category)` | Record work that cannot be detected. |
| `list_manual_entries(target_date)` / `remove_manual_entry(entry_id)` | Review or delete manual entries. |
| `search_cursor_activity(query, start_date, end_date)` | Keyword search across tasks, outcomes and commits. |
| `get_cursor_projects()` | Known workspaces. |
| `check_timelog_sources(target_date)` | Diagnostics: which sources were readable and what each returned. |

The result includes `formatting_instructions`, so Cursor writes a table of **Project | Timelog Description | Time | Hours** in clear, grammatical, past-tense English with a total.

## Install / upgrade

```bash
cd cursor-timelog-mcp
chmod +x scripts/*.sh
./scripts/install.sh
```

Then in Cursor: Settings → Tools & MCP → toggle `cursor-timelog` off and on (or restart Cursor). Ask: `timelog`.

## Limits

Cursor's local database format is not a public API and can change; `check_timelog_sources` shows if a source stops working. Browser research, meetings and work in other applications are invisible to any local tool, so add them with `add_manual_entry`.

## Test

```bash
python3 -m unittest discover -s tests -v
```

## Uninstall

```bash
./scripts/uninstall.sh
```
