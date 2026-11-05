"""Cursor Timelog MCP v2.

Builds an accurate, project-wise daily timelog from what actually happened in Cursor:

* Cursor chat history (every chat/agent message with its real timestamp, user requests,
  assistant outcomes, files edited, tools used) - read from Cursor's local SQLite state,
  with Cursor's agent-transcript files as a fallback.
* Git commits made by the current user.
* Files modified on disk (manual editing that happened without a chat).
* Plans / canvases / documents produced in Cursor.
* Manual entries the user adds for work that no tool can see (web research, meetings, ...).

Everything is read on demand. Nothing is tracked in the background. The only thing stored is
the small manual-entries file the user explicitly writes to.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

from .narrative import cluster_beats, narrate, narrate_commits

VERSION = "2.1.0"

HOME = Path.home()
USER_DIR = Path(os.environ.get("CURSOR_USER_DIR", HOME / ".config/Cursor/User")).expanduser()
PROJECTS_DIR = Path(os.environ.get("CURSOR_PROJECTS_DIR", HOME / ".cursor/projects")).expanduser()
PLANS_DIR = Path(os.environ.get("CURSOR_PLANS_DIR", HOME / ".cursor/plans")).expanduser()
DATA_DIR = Path(os.environ.get("CURSOR_TIMELOG_DATA", HOME / ".local/share/cursor-timelog-mcp/data")).expanduser()
MANUAL_FILE = DATA_DIR / "manual_entries.json"

TZ_NAME = os.environ.get("TIMELOG_TZ") or os.environ.get("TZ") or "Asia/Kolkata"
try:
    LOCAL_TZ = ZoneInfo(TZ_NAME.lstrip(":"))
except Exception:  # pragma: no cover - depends on host tz database
    LOCAL_TZ = datetime.now().astimezone().tzinfo
    TZ_NAME = str(LOCAL_TZ)

# A pause longer than this between two messages is treated as a break, not work time.
IDLE_GAP_SECONDS = int(os.environ.get("TIMELOG_IDLE_GAP_MINUTES", "30")) * 60
ROUND_MINUTES = 5
MIN_TASK_MINUTES = 5
MANUAL_EDIT_MIN_SPAN_SECONDS = 10 * 60
GENERAL_PROJECT = "General"

IGNORED_PATH_PARTS = {
    ".git", "vendor", "node_modules", "generated", "var", "pub", ".idea", ".vscode",
    "__pycache__", ".venv", "venv", ".cursor", "dist", "build", ".next",
}
IGNORED_SUFFIXES = (".zip", ".tar", ".gz", ".log", ".pyc", ".map", ".lock", ".sqlite", ".db")

# --------------------------------------------------------------------------------------
# Generic helpers
# --------------------------------------------------------------------------------------


def parse_date(value: str | None) -> date:
    """Parse today / yesterday / weekday / YYYY-MM-DD / '3 Nov' / 'Nov 3'."""
    text = (value or "today").strip().lower()
    now = datetime.now(LOCAL_TZ).date()
    if text in {"today", "now", ""}:
        return now
    if text == "yesterday":
        return now - timedelta(days=1)
    if text in {"day before yesterday", "day before"}:
        return now - timedelta(days=2)
    iso = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text)
    if iso:
        return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for index, name in enumerate(weekdays):
        if re.search(rf"\b(?:last\s+|on\s+)?{name}\b", text):
            delta = (now.weekday() - index) % 7
            return now - timedelta(days=delta or 7)
    months = {m: i + 1 for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
    match = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3})[a-z]*\b", text) or None
    if match and match.group(2) in months:
        return _month_day(now, months[match.group(2)], int(match.group(1)))
    match = re.search(r"\b([a-z]{3})[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?\b", text)
    if match and match.group(1) in months:
        return _month_day(now, months[match.group(1)], int(match.group(2)))
    return date.fromisoformat(text)


def _month_day(today: date, month: int, day: int) -> date:
    candidate = date(today.year, month, day)
    return candidate if candidate <= today else date(today.year - 1, month, day)


def day_bounds(day: date) -> tuple[float, float]:
    start = datetime(day.year, day.month, day.day, tzinfo=LOCAL_TZ)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def parse_iso(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000 if number > 10**11 else number
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, LOCAL_TZ).strftime("%I:%M %p").lstrip("0")


def fmt_range(start: float, end: float) -> str:
    if round(start / 60) == round(end / 60):
        return fmt_time(start)
    return f"{fmt_time(start)} - {fmt_time(end)}"


def fmt_duration(minutes: int | None) -> str | None:
    if minutes is None:
        return None
    hours, rest = divmod(int(minutes), 60)
    if hours and rest:
        return f"{hours} h {rest} min"
    if hours:
        return f"{hours} h"
    return f"{rest} min"


def round_minutes(seconds: float, minimum: int = MIN_TASK_MINUTES) -> int:
    minutes = max(round(seconds / 60 / ROUND_MINUTES) * ROUND_MINUTES, minimum)
    return int(minutes)


def slugify(path: str) -> str:
    """Cursor's folder name for a workspace path (non-alphanumerics become dashes)."""
    return re.sub(r"[^A-Za-z0-9]+", "-", path).strip("-")


def clean_text(text: str, limit: int = 320) -> str:
    text = re.sub(r"```.*?```", " [code] ", text or "", flags=re.S)
    text = re.sub(r"<[^>]{1,40}>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text


def clean_outcome(text: str, limit: int = 360) -> str:
    text = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    text = re.sub(r"[*_`#>|]+", " ", text)
    text = re.sub(r"-{3,}", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text


META_RE = re.compile(
    r"\b(?:t[ie]m[e]?\s*-?\s*l[o]?g\w*|time\s*sheet|timesheet|work\s+summary|what\s+did\s+i\s+work)\b", re.I
)


def is_meta_turn(text: str) -> bool:
    """A short 'timelog' style prompt is a request for the report, not work to be logged."""
    cleaned = clean_text(text, 400)
    return bool(cleaned) and len(cleaned.split()) <= 12 and bool(META_RE.search(cleaned))


# --------------------------------------------------------------------------------------
# Interval maths
# --------------------------------------------------------------------------------------

Interval = tuple[float, float]


def merge_intervals(intervals: Iterable[Interval], gap: float = 0.0) -> list[Interval]:
    ordered = sorted((min(a, b), max(a, b)) for a, b in intervals)
    merged: list[list[float]] = []
    for start, end in ordered:
        if merged and start - merged[-1][1] <= gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def measure(intervals: Iterable[Interval]) -> float:
    return sum(end - start for start, end in intervals)


def subtract_intervals(intervals: list[Interval], covered: list[Interval]) -> list[Interval]:
    result: list[Interval] = []
    for start, end in intervals:
        pieces = [(start, end)]
        for c_start, c_end in covered:
            next_pieces: list[Interval] = []
            for p_start, p_end in pieces:
                if c_end <= p_start or c_start >= p_end:
                    next_pieces.append((p_start, p_end))
                    continue
                if c_start > p_start:
                    next_pieces.append((p_start, c_start))
                if c_end < p_end:
                    next_pieces.append((c_end, p_end))
            pieces = next_pieces
        result.extend(pieces)
    return result


# --------------------------------------------------------------------------------------
# Workspaces
# --------------------------------------------------------------------------------------


def workspace_records() -> list[dict[str, Any]]:
    root = USER_DIR / "workspaceStorage"
    records: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return []
    for folder in root.iterdir():
        meta = folder / "workspace.json"
        if not meta.is_file():
            continue
        try:
            obj = json.loads(meta.read_text(errors="replace"))
        except Exception:
            continue
        uri = obj.get("folder") or ""
        if not uri.startswith("file://"):
            continue
        path = Path(unquote(urlparse(uri).path))
        if not path.is_dir():
            continue
        db = folder / "state.vscdb"
        record = {
            "name": path.name,
            "path": str(path),
            "slug": slugify(str(path)),
            "state_db": str(db),
            "db_mtime": db.stat().st_mtime if db.is_file() else 0.0,
        }
        previous = records.get(record["path"])
        if not previous or record["db_mtime"] > previous["db_mtime"]:
            records[record["path"]] = record
    return list(records.values())


def project_for_path(path: str, workspaces: list[dict[str, Any]]) -> str | None:
    """Map a file path to a project name (workspace prefix, else the folder under ~/htdocs)."""
    if not path or path.startswith(str(HOME / ".cursor")) or path.startswith("/tmp"):
        return None
    best = None
    for item in workspaces:
        prefix = item["path"].rstrip("/") + "/"
        if path.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, item["name"])
    if best:
        return best[1]
    htdocs = HOME / "htdocs"
    try:
        parts = Path(path).relative_to(htdocs).parts
    except ValueError:
        return None
    if not parts:
        return None
    first = htdocs / parts[0]
    if (first / ".git").exists() or len(parts) < 3:
        return parts[0]
    return parts[1]


# --------------------------------------------------------------------------------------
# Cursor chat history
# --------------------------------------------------------------------------------------


def global_db_path() -> Path:
    return USER_DIR / "globalStorage" / "state.vscdb"


def connect_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
    con.text_factory = lambda raw: raw.decode("utf-8", "ignore")
    con.execute("PRAGMA busy_timeout=3000")
    return con


def _json_row(raw: Any) -> list[Any]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        return value if isinstance(value, list) else []
    except Exception:
        return []


def transcript_index() -> dict[str, str]:
    """composerId -> workspace slug, from ~/.cursor/projects/<slug>/agent-transcripts."""
    mapping: dict[str, str] = {}
    if not PROJECTS_DIR.is_dir():
        return mapping
    for project in PROJECTS_DIR.iterdir():
        base = project / "agent-transcripts"
        if not base.is_dir():
            continue
        for child in base.iterdir():
            mapping.setdefault(child.name if child.is_dir() else child.stem, project.name)
        for sub in base.rglob("*.jsonl"):
            mapping.setdefault(sub.stem, project.name)
    return mapping


def workspace_composer_index(workspaces: list[dict[str, Any]]) -> dict[str, str]:
    """composerId -> workspace path, from each workspace's own Cursor state database."""
    mapping: dict[str, str] = {}
    uuid_re = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    for item in workspaces:
        db = Path(item["state_db"])
        if not db.is_file():
            continue
        try:
            con = connect_ro(db)
            for (key,) in con.execute("SELECT key FROM ItemTable WHERE key LIKE '%composer%'"):
                for found in uuid_re.findall(str(key)):
                    mapping.setdefault(found, item["path"])
            row = con.execute("SELECT value FROM ItemTable WHERE key='composer.composerData'").fetchone()
            if row:
                data = json.loads(row[0])
                ids = list(data.get("selectedComposerIds", [])) + list(data.get("lastFocusedComposerIds", []))
                ids += [c.get("composerId") for c in data.get("allComposers", []) if isinstance(c, dict)]
                for found in ids:
                    if found:
                        mapping.setdefault(found, item["path"])
            con.close()
        except Exception:
            continue
    return mapping


def candidate_composers(con: sqlite3.Connection, start: float, end: float) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    sql = (
        "SELECT key, json_extract(value,'$.createdAt','$.lastUpdatedAt','$.name',"
        "'$.subagentComposerIds','$.subComposerIds') FROM cursorDiskKV "
        "WHERE key >= 'composerData:' AND key < 'composerData;'"
    )
    try:
        rows = con.execute(sql).fetchall()
    except sqlite3.Error:
        rows = []
        for key, raw in con.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"):
            try:
                o = json.loads(raw)
            except Exception:
                continue
            rows.append((key, json.dumps([o.get("createdAt"), o.get("lastUpdatedAt"), o.get("name"),
                                          o.get("subagentComposerIds"), o.get("subComposerIds")])))
    for key, raw in rows:
        values = _json_row(raw)
        if len(values) < 5:
            continue
        created, updated, name, subagents, subs = values
        created_ts, updated_ts = parse_iso(created), parse_iso(updated)
        if created_ts is None and updated_ts is None:
            continue
        if created_ts is not None and created_ts > end:
            continue
        if updated_ts is not None and updated_ts < start:
            continue
        if updated_ts is None and created_ts is not None and created_ts < start - 14 * 86400:
            continue
        cid = str(key).split(":", 1)[1]
        children = []
        for group in (subagents, subs):
            if isinstance(group, str):
                try:
                    group = json.loads(group)
                except Exception:
                    group = []
            if isinstance(group, list):
                children += [str(item) for item in group if item]
        out[cid] = {"id": cid, "name": name if isinstance(name, str) else "", "children": children}
    return out


EDIT_TOOL_HINTS = ("edit_file", "write", "apply_patch", "search_replace", "delete_file", "edit_notebook",
                   "multi_edit", "str_replace", "create_file")
READ_TOOL_HINTS = ("read_file", "grep", "ripgrep", "glob", "list_dir", "codebase_search", "semantic_search")
WEB_TOOL_HINTS = ("web_search", "web_fetch", "fetch", "browser")
PATH_RE = re.compile(r'"(?:targetFile|target_file|path|relativeWorkspacePath|filePath|file_path)"\s*:\s*"([^"]+)"')


def load_bubbles(con: sqlite3.Connection, cid: str, start: float, end: float) -> list[dict[str, Any]]:
    low, high = f"bubbleId:{cid}:", f"bubbleId:{cid};"
    sql = (
        "SELECT json_extract(value,'$.createdAt','$.type','$.text','$.toolFormerData.name',"
        "'$.toolFormerData.params') FROM cursorDiskKV WHERE key >= ? AND key < ?"
    )
    bubbles: list[dict[str, Any]] = []
    try:
        rows = [row[0] for row in con.execute(sql, (low, high))]
    except sqlite3.Error:
        rows = []
        for (raw,) in con.execute("SELECT value FROM cursorDiskKV WHERE key >= ? AND key < ?", (low, high)):
            try:
                o = json.loads(raw)
            except Exception:
                continue
            tool = o.get("toolFormerData") or {}
            rows.append(json.dumps([o.get("createdAt"), o.get("type"), o.get("text"),
                                    tool.get("name"), tool.get("params")]))
    for raw in rows:
        values = _json_row(raw)
        if len(values) < 5:
            continue
        ts = parse_iso(values[0])
        if ts is None or not (start <= ts < end):
            continue
        bubbles.append({"ts": ts, "type": values[1], "text": values[2] if isinstance(values[2], str) else "",
                        "tool": values[3] if isinstance(values[3], str) else "",
                        "params": values[4] if isinstance(values[4], str) else ""})
    bubbles.sort(key=lambda b: b["ts"])
    return bubbles


def split_turns(bubbles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for bubble in bubbles:
        if bubble["type"] == 1 or not turns:
            turns.append({"start": bubble["ts"], "end": bubble["ts"],
                          "user": bubble["text"] if bubble["type"] == 1 else "", "bubbles": [bubble]})
        else:
            turns[-1]["bubbles"].append(bubble)
            turns[-1]["end"] = bubble["ts"]
    return turns


def summarize_turn(turn: dict[str, Any]) -> dict[str, Any]:
    edited: list[str] = []
    tools = defaultdict(int)
    final = ""
    for bubble in turn["bubbles"]:
        name = (bubble["tool"] or "").lower()
        if name:
            if any(h in name for h in EDIT_TOOL_HINTS):
                tools["edits"] += 1
                for path in PATH_RE.findall(bubble["params"] or ""):
                    if path not in edited:
                        edited.append(path)
            elif any(h in name for h in READ_TOOL_HINTS):
                tools["reads"] += 1
            elif "terminal" in name or "shell" in name:
                tools["commands"] += 1
            elif any(h in name for h in WEB_TOOL_HINTS):
                tools["web"] += 1
            elif name.startswith("mcp"):
                tools["mcp"] += 1
        if bubble["type"] == 2 and bubble["text"].strip():
            final = bubble["text"]
    return {"edited": edited, "tools": dict(tools), "final": final, "finding": _finding(final)}


def _finding(text: str) -> str:
    """Keep one concrete sentence from an assistant reply. Later replies must not erase it."""
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r"^I['’]ll\s+", "", text)
    text = re.sub(r"^I\s+", "", text)
    if not text:
        return ""
    sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
    if len(sentence) < 40 or sentence.lower().startswith(("yes", "no", "mostly", "ok", "sure")):
        return ""
    if sentence[-1] not in ".!?":
        sentence += "."
    return sentence[:360]


def categorize(requests: list[str], edited: list[str], tools: dict[str, int]) -> str:
    text = " ".join(requests).lower()
    code_edits = [p for p in edited if not p.lower().endswith((".md", ".txt", ".mdc", ".rst"))]
    doc_edits = [p for p in edited if p.lower().endswith((".md", ".txt", ".rst"))]
    if re.search(r"\b(research|r&d|investigat|explor|compare|evaluat|poc|spike|feasib|best practice|alternatives?)\b", text):
        return "Research & Analysis"
    if code_edits and re.search(r"\b(fix|bug|error|exception|issue|fail|broken|not working)\b", text):
        return "Debugging & Fixes"
    if code_edits:
        return "Development"
    if doc_edits or re.search(r"\b(document|documentation|readme|prepare (?:a |the )?(?:list|report)|summary)\b", text):
        return "Documentation"
    if re.search(r"\b(verify|test|qa|validate|dry run|check)\b", text):
        return "Testing & Verification"
    if re.search(r"\b(fix|bug|error|exception|issue|fail|broken|not working)\b", text):
        return "Debugging & Fixes"
    if tools.get("reads") or tools.get("commands") or tools.get("web"):
        return "Research & Analysis"
    return "General"


def parse_transcript_timestamp(text: str) -> float | None:
    match = re.search(r"<timestamp>\s*[A-Za-z]+,\s*([A-Za-z]+ \d{1,2}, \d{4}),\s*(\d{1,2}:\d{2} [AP]M)", text)
    if not match:
        return None
    try:
        naive = datetime.strptime(f"{match.group(1)} {match.group(2)}", "%b %d, %Y %I:%M %p")
    except ValueError:
        return None
    return naive.replace(tzinfo=LOCAL_TZ).timestamp()


def transcript_fallback_turns(composer_ids_seen: set[str], start: float, end: float) -> list[dict[str, Any]]:
    """Turns recovered from agent-transcript files for chats missing from the SQLite state."""
    found: list[dict[str, Any]] = []
    if not PROJECTS_DIR.is_dir():
        return found
    for path in PROJECTS_DIR.glob("*/agent-transcripts/**/*.jsonl"):
        cid = path.stem
        if cid in composer_ids_seen or "subagents" in path.parts:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < start:
            continue
        turns: list[dict[str, Any]] = []
        try:
            with path.open(errors="replace") as handle:
                for line in handle:
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    content = (obj.get("message") or {}).get("content")
                    if not isinstance(content, list):
                        continue
                    text = "".join(part.get("text", "") for part in content if part.get("type") == "text")
                    if obj.get("role") == "user":
                        ts = parse_transcript_timestamp(text)
                        query = text.split("<user_query>", 1)[-1].split("</user_query>", 1)[0].strip()
                        if ts is not None:
                            turns.append({"start": ts, "end": ts, "user": query, "final": ""})
                    elif obj.get("role") == "assistant" and turns and text.strip():
                        turns[-1]["final"] = text
        except OSError:
            continue
        for index, turn in enumerate(turns):
            nxt = turns[index + 1]["start"] if index + 1 < len(turns) else mtime
            turn["end"] = max(turn["start"], min(nxt, mtime, turn["start"] + 10 * 60))
        today_turns = [t for t in turns if start <= t["start"] < end]
        if today_turns:
            found.append({"id": cid, "slug": path.parts[path.parts.index("projects") + 1] if "projects" in path.parts else "",
                          "turns": today_turns})
    return found


def build_chat_tasks(day: date, workspaces: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    start, end = day_bounds(day)
    slug_to_workspace = {w["slug"]: w for w in workspaces}
    path_to_workspace = {w["path"]: w for w in workspaces}
    transcript_map = transcript_index()
    workspace_map = workspace_composer_index(workspaces)
    stats = {"database": str(global_db_path()), "database_found": global_db_path().is_file(),
             "chats_found": 0, "chats_unassigned": 0, "fallback_chats": 0}
    tasks: dict[tuple[str, str], dict[str, Any]] = {}
    seen: set[str] = set()

    def chat_project(cid: str) -> str:
        slug = transcript_map.get(cid)
        if slug:
            if slug in slug_to_workspace:
                return slug_to_workspace[slug]["name"]
            return re.sub(r"^.*?htdocs-", "", slug) or slug
        path = workspace_map.get(cid)
        if path and path in path_to_workspace:
            return path_to_workspace[path]["name"]
        return ""

    def add_turns(cid: str, title: str, default_project: str, turns: list[dict[str, Any]]) -> None:
        for turn in turns:
            user_text = turn.get("user", "")
            info = turn.get("info") or {"edited": [], "tools": {}, "final": turn.get("final", ""), "finding": ""}
            if user_text and (is_meta_turn(user_text) or (
                    META_RE.search(clean_text(user_text, 800)) and not info["edited"])):
                continue  # asking for / complaining about the timelog itself is not work to be logged
            project = default_project or GENERAL_PROJECT
            if info["edited"]:
                counts: dict[str, int] = defaultdict(int)
                for path in info["edited"]:
                    name = project_for_path(path, workspaces)
                    if name:
                        counts[name] += 1
                if counts and project not in counts:
                    project = max(counts, key=counts.get)
            task = tasks.setdefault((cid, project), {
                "chat_id": cid, "project": project, "title": title, "intervals": [], "requests": [],
                "outcomes": [], "beats": [], "edited": [], "tools": defaultdict(int), "source": "cursor_chat"})
            task["intervals"].append((turn["start"], turn["end"]))
            request = clean_text(user_text, 500)
            if request and request not in task["requests"]:
                task["requests"].append(request)
            finding = info.get("finding") or ""
            if request or finding:
                # Every turn is retained. A later message in the same tab must not replace this one.
                task["beats"].append({
                    "request": user_text.strip() if user_text else request,
                    "finding": finding,
                    "start": turn["start"],
                    "end": turn["end"],
                })
            outcome = clean_outcome(info["final"])
            if outcome and outcome not in task["outcomes"]:
                task["outcomes"].append(outcome)
            for path in info["edited"]:
                if path not in task["edited"]:
                    task["edited"].append(path)
            for key, value in info["tools"].items():
                task["tools"][key] += value

    if global_db_path().is_file():
        try:
            con = connect_ro(global_db_path())
            composers = candidate_composers(con, start, end)
            children = {child for item in composers.values() for child in item["children"]}
            bubble_cache: dict[str, list[dict[str, Any]]] = {}
            for cid in composers:
                bubble_cache[cid] = load_bubbles(con, cid, start, end)
            for cid, meta in composers.items():
                bubbles = bubble_cache[cid]
                if not bubbles or cid in children:
                    continue  # sub-agent chats are folded into their parent (or handled below)
                seen.add(cid)
                stats["chats_found"] += 1
                project = chat_project(cid)
                if not project:
                    stats["chats_unassigned"] += 1
                turns = split_turns(bubbles)
                for turn in turns:
                    turn["info"] = summarize_turn(turn)
                # Fold sub-agent activity into the parent chat as extra working time.
                for child in meta["children"]:
                    extra = bubble_cache.get(child) or []
                    if extra:
                        seen.add(child)
                        turns.append({"start": extra[0]["ts"], "end": extra[-1]["ts"], "user": "",
                                      "info": {"edited": [], "tools": {"subagent": len(extra)}, "final": ""}})
                add_turns(cid, meta["name"], project, turns)
            for cid in children:
                bubbles = bubble_cache.get(cid)
                if bubbles and cid not in seen:
                    seen.add(cid)
                    stats["chats_found"] += 1
                    turns = split_turns(bubbles)
                    for turn in turns:
                        turn["info"] = summarize_turn(turn)
                    add_turns(cid, composers[cid]["name"] or "Sub-task", chat_project(cid), turns)
            con.close()
        except sqlite3.Error as exc:
            stats["database_error"] = str(exc)

    for item in transcript_fallback_turns(seen, start, end):
        slug = item["slug"]
        project = slug_to_workspace[slug]["name"] if slug in slug_to_workspace else re.sub(r"^.*?htdocs-", "", slug)
        stats["fallback_chats"] += 1
        add_turns(item["id"], "", project, item["turns"])

    return list(tasks.values()), stats


# --------------------------------------------------------------------------------------
# Git, files, documents, manual entries
# --------------------------------------------------------------------------------------


def run_git(path: str, args: list[str], timeout: int = 6) -> str:
    try:
        proc = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=timeout)
        return proc.stdout if proc.returncode == 0 else ""
    except Exception:
        return ""


def git_commits(workspace: dict[str, Any], start: float, end: float) -> list[dict[str, Any]]:
    path = workspace["path"]
    if not (Path(path) / ".git").exists():
        return []
    since = datetime.fromtimestamp(start, LOCAL_TZ).isoformat()
    until = datetime.fromtimestamp(end, LOCAL_TZ).isoformat()
    args = ["log", "--all", f"--since={since}", f"--until={until}", "--pretty=format:%cI%x1f%s", "-n", "80"]
    author = run_git(path, ["config", "user.email"], 3).strip() or run_git(path, ["config", "user.name"], 3).strip()
    if author:
        args.append(f"--author={author}")
    commits = []
    for line in run_git(path, args).splitlines():
        stamp, _, subject = line.partition("\x1f")
        ts = parse_iso(stamp)
        if ts is not None and start <= ts < end:
            commits.append({"ts": ts, "message": clean_text(subject, 160)})
    return commits


def modified_files(workspace: dict[str, Any], start: float, end: float) -> list[tuple[float, str]]:
    path = workspace["path"]
    if not (Path(path) / ".git").exists():
        return []
    names: set[str] = set()
    for args in (["ls-files", "-m", "-o", "--exclude-standard", "-z"], ["diff", "--name-only", "--cached", "-z"]):
        names.update(n for n in run_git(path, args, 12).split("\0") if n)
    files: list[tuple[float, str]] = []
    for rel in list(names)[:6000]:
        if set(Path(rel).parts) & IGNORED_PATH_PARTS or rel.lower().endswith(IGNORED_SUFFIXES):
            continue
        try:
            mtime = (Path(path) / rel).stat().st_mtime
        except OSError:
            continue
        if start <= mtime < end:
            files.append((mtime, rel))
    # Bulk operations (checkout, composer, unzip) touch many files in a minute; that is not editing.
    per_minute: dict[int, int] = defaultdict(int)
    for mtime, _ in files:
        per_minute[int(mtime // 60)] += 1
    return sorted((m, r) for m, r in files if per_minute[int(m // 60)] <= 25)


def produced_documents(slug_to_name: dict[str, str], start: float, end: float) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    if PROJECTS_DIR.is_dir():
        for project in PROJECTS_DIR.iterdir():
            canvases = project / "canvases"
            if canvases.is_dir():
                for file in canvases.rglob("*"):
                    try:
                        if file.is_file() and start <= file.stat().st_mtime < end:
                            docs.append({"project": slug_to_name.get(project.name, re.sub(r"^.*?htdocs-", "", project.name)),
                                         "name": file.name, "ts": file.stat().st_mtime, "kind": "canvas"})
                    except OSError:
                        continue
    if PLANS_DIR.is_dir():
        for file in PLANS_DIR.glob("*"):
            try:
                if file.is_file() and start <= file.stat().st_mtime < end:
                    docs.append({"project": GENERAL_PROJECT, "name": file.name, "ts": file.stat().st_mtime, "kind": "plan"})
            except OSError:
                continue
    return docs


def load_manual_entries() -> list[dict[str, Any]]:
    try:
        data = json.loads(MANUAL_FILE.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_manual_entries(entries: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MANUAL_FILE.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n")


def parse_clock(value: str, day: date) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%I:%M %p", "%I %p", "%H:%M", "%I:%M%p", "%I%p"):
        try:
            parsed = datetime.strptime(value.upper(), fmt)
            return datetime(day.year, day.month, day.day, parsed.hour, parsed.minute, tzinfo=LOCAL_TZ).timestamp()
        except ValueError:
            continue
    raise ValueError(f"Cannot read time '{value}'. Use for example '2:30 PM' or '14:30'.")


def parse_hours(value: str) -> float | None:
    value = (value or "").strip().lower()
    if not value:
        return None
    match = re.fullmatch(r"(?:(\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?)?\s*(?:(\d+)\s*m(?:in(?:ute)?s?)?)?", value)
    if match and (match.group(1) or match.group(2)):
        return float(match.group(1) or 0) + int(match.group(2) or 0) / 60
    return float(value)


# --------------------------------------------------------------------------------------
# Report assembly
# --------------------------------------------------------------------------------------


def _title_line(title: str) -> str:
    title = re.sub(r"\s+", " ", title or "").strip(" .")
    if not title:
        return ""
    return "Worked on " + title[0].lower() + title[1:] + "."


def _split_chat_task(task: dict[str, Any]) -> list[dict[str, Any]]:
    """One chat tab can hold several pieces of work. Keep all of them as separate summaries."""
    beats = [beat for beat in task.get("beats") or [] if (beat.get("request") or "").strip()]
    groups = cluster_beats(beats) if len(beats) > 1 else ([beats] if beats else [])
    if len(groups) <= 1:
        if groups:
            task["requests"] = [beat["request"] for beat in groups[0]] or task["requests"]
        task["description"] = narrate(task["requests"]) or _title_line(task.get("title") or "")
        return [task]
    pieces = []
    for group in groups:
        requests = [beat["request"] for beat in group if beat.get("request")]
        intervals = [(beat["start"], beat["end"]) for beat in group if beat.get("start") is not None and beat.get("end") is not None]
        piece = dict(task)
        piece["intervals"] = intervals or list(task["intervals"])
        piece["requests"] = requests
        piece["beats"] = group
        piece["outcomes"] = [beat["finding"] for beat in group if beat.get("finding")] or list(task["outcomes"])
        piece["edited"] = list(task["edited"])
        piece["description"] = narrate(requests) or _title_line(task.get("title") or "")
        pieces.append(piece)
    described = [piece for piece in pieces if piece.get("description")]
    return described or pieces


def _short_files(paths: list[str], limit: int = 10) -> list[str]:
    names: list[str] = []
    for path in paths:
        name = Path(path).name
        if name not in names:
            names.append(name)
    return names[:limit]


def build_timelog(day: date, project_filter: str = "", topic_filter: str = "") -> dict[str, Any]:
    start, end = day_bounds(day)
    workspaces = workspace_records()
    slug_to_name = {w["slug"]: w["name"] for w in workspaces}

    chat_tasks, chat_stats = build_chat_tasks(day, workspaces)

    chat_projects = {t["project"] for t in chat_tasks}
    scan_targets = [w for w in workspaces if w["name"] in chat_projects or w["db_mtime"] >= start]
    commit_targets = workspaces

    with ThreadPoolExecutor(max_workers=8) as pool:
        commit_future = {w["name"]: pool.submit(git_commits, w, start, end) for w in commit_targets}
        file_future = {w["name"]: pool.submit(modified_files, w, start, end) for w in scan_targets}
        commits = {name: f.result() for name, f in commit_future.items()}
        files = {name: f.result() for name, f in file_future.items()}

    documents = produced_documents(slug_to_name, start, end)
    manual = [e for e in load_manual_entries() if e.get("date") == day.isoformat()]

    # --- chat tasks -> measured tasks, per project -----------------------------------
    by_project: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for task in chat_tasks:
        if not task["intervals"]:
            continue
        for piece in _split_chat_task(task):
            if not piece["intervals"]:
                continue
            sessions = merge_intervals(piece["intervals"], IDLE_GAP_SECONDS)
            piece["sessions"] = sessions
            piece["first"] = sessions[0][0]
            piece["last"] = sessions[-1][1]
            piece["category"] = categorize(piece["requests"], piece["edited"], dict(piece["tools"]))
            if not piece.get("description"):
                piece["description"] = narrate(piece["requests"])
            by_project[piece["project"]].append(piece)

    # --- manual-edit tasks for files changed outside any chat session ----------------
    for name, items in files.items():
        if not items:
            continue
        chat_cover = merge_intervals([s for t in by_project.get(name, []) for s in t["sessions"]], 15 * 60)
        clusters: list[list[tuple[float, str]]] = []
        for mtime, rel in items:
            if clusters and mtime - clusters[-1][-1][0] <= IDLE_GAP_SECONDS:
                clusters[-1].append((mtime, rel))
            else:
                clusters.append([(mtime, rel)])
        for cluster in clusters:
            first, last = cluster[0][0], cluster[-1][0]
            if any(c_start <= first and last <= c_end for c_start, c_end in chat_cover):
                continue
            span = last - first
            if span < MANUAL_EDIT_MIN_SPAN_SECONDS:
                continue  # a single save (git checkout, formatter, ...) is evidence only, not a task
            by_project[name].append({
                "chat_id": "", "project": name, "title": "Manual file changes", "source": "file_changes",
                "intervals": [(first, last)], "sessions": [(first, last)],
                "first": first, "last": last, "category": "Development",
                "requests": [], "outcomes": [], "edited": [r for _, r in cluster], "tools": {},
                "description": "Edited " + ", ".join(_short_files([r for _, r in cluster], 5)) + " directly in the editor.",
            })

    # --- manual entries --------------------------------------------------------------
    for entry in manual:
        project = entry.get("project") or GENERAL_PROJECT
        s_ts, e_ts = entry.get("start_ts"), entry.get("end_ts")
        hours = entry.get("hours")
        task = {
            "chat_id": entry["id"], "project": project, "title": entry.get("description", ""),
            "source": "manual_entry", "intervals": [(s_ts, e_ts)] if s_ts and e_ts else [],
            "sessions": [(s_ts, e_ts)] if s_ts and e_ts else [], "first": s_ts or start, "last": e_ts or start,
            "category": entry.get("category") or "Research & Analysis", "requests": [entry.get("description", "")],
            "outcomes": [], "edited": [], "tools": {}, "manual_minutes": round(hours * 60) if hours else None,
        }
        by_project[project].append(task)

    # --- commits made outside any chat session become their own tasks ----------------
    for name, items in commits.items():
        if not items:
            continue
        cover = merge_intervals([s for t in by_project.get(name, []) if t["source"] in ("cursor_chat", "file_changes")
                                 for s in t["sessions"]], 15 * 60)
        ordered = sorted(items, key=lambda c: c["ts"])
        clusters = []
        for commit in ordered:
            if clusters and commit["ts"] - clusters[-1][-1]["ts"] <= IDLE_GAP_SECONDS:
                clusters[-1].append(commit)
            else:
                clusters.append([commit])
        pending: list[dict[str, Any]] = []
        for cluster in clusters:
            first, last = cluster[0]["ts"], cluster[-1]["ts"]
            if any(c_start <= first and last <= c_end for c_start, c_end in cover):
                continue
            if last - first >= MANUAL_EDIT_MIN_SPAN_SECONDS:
                by_project[name].append({
                    "chat_id": "", "project": name, "title": "Source control commits", "source": "git_commits",
                    "intervals": [(first, last)], "sessions": [(first, last)], "first": first, "last": last,
                    "category": "Development", "requests": [], "outcomes": [c["message"] for c in cluster],
                    "edited": [], "tools": {}, "description": narrate_commits([c["message"] for c in cluster]),
                })
            else:
                pending.extend(cluster)
        if pending:  # isolated commits: one combined item whose duration needs confirming
            by_project[name].append({
                "chat_id": "", "project": name, "title": "Source control commits", "source": "git_commits",
                "intervals": [], "sessions": [], "first": pending[0]["ts"], "last": pending[-1]["ts"],
                "category": "Development", "requests": [], "outcomes": [c["message"] for c in pending][:12],
                "edited": [], "tools": {}, "commit_times": [fmt_time(c["ts"]) for c in pending],
                "description": narrate_commits([c["message"] for c in pending]),
            })

    # --- allocate minutes so project totals equal the sum of their tasks -------------
    projects_out: list[dict[str, Any]] = []
    all_sessions: list[Interval] = []
    for name, tasks in by_project.items():
        if project_filter and project_filter.lower() not in name.lower():
            continue
        tasks.sort(key=lambda t: t["first"])
        covered: list[Interval] = []
        task_out: list[dict[str, Any]] = []
        for task in tasks:
            if topic_filter == "research" and task["category"] != "Research & Analysis":
                continue
            minutes: int | None
            note = ""
            if task.get("manual_minutes") is not None:
                minutes = int(task["manual_minutes"])
                note = "hours supplied by the user"
            elif task["sessions"]:
                free = subtract_intervals(task["sessions"], covered)
                seconds = measure(free)
                overlapped = seconds < measure(task["sessions"])
                if not free or (overlapped and seconds < 120):
                    minutes, note = 0, "time already counted in an overlapping task"
                else:
                    minutes = round_minutes(seconds)
                    note = {"file_changes": "estimated from the first to the last file save",
                            "git_commits": "estimated from the first to the last commit; work may have started earlier",
                            }.get(task["source"], "")
                covered = merge_intervals(covered + task["sessions"])
                all_sessions += task["sessions"]
            else:
                minutes = None
                note = "only a single point in time was found; duration cannot be measured"
            edited = _short_files(task["edited"])
            task_out.append({
                "task": task["title"] or (task["requests"][0] if task["requests"] else "Untitled task"),
                "description": task.get("description") or "",
                "category": task["category"],
                "source": task["source"],
                "time": ", ".join(fmt_range(a, b) for a, b in task["sessions"][:4]) + (" ..." if len(task["sessions"]) > 4 else "")
                        if task["sessions"] else fmt_range(task["first"], task["last"]),
                "sessions": [fmt_range(a, b) for a, b in task["sessions"]],
                "minutes": minutes,
                "hours": round(minutes / 60, 2) if minutes is not None else None,
                "duration": fmt_duration(minutes),
                "hours_note": note,
                "user_requests": task["requests"][:40],
                "outcomes": task["outcomes"][:20],
                "files_changed": edited,
                "files_changed_count": len(task["edited"]),
                "activity": dict(task["tools"]),
                **({"commit_times": task["commit_times"]} if task.get("commit_times") else {}),
            })
        if not task_out:
            continue
        measured = [t["minutes"] for t in task_out if t["minutes"] is not None]
        total_minutes = sum(measured) if measured else None
        projects_out.append({
            "_first": min(t["first"] for t in tasks),
            "project": name,
            "total_minutes": total_minutes,
            "total_hours": round(total_minutes / 60, 2) if total_minutes is not None else None,
            "total_duration": fmt_duration(total_minutes),
            "tasks": task_out,
            "git_commits": [{"time": fmt_time(c["ts"]), "message": c["message"]} for c in commits.get(name, [])][:20],
            "files_modified_count": len(files.get(name, [])),
            "files_modified_sample": _short_files([r for _, r in files.get(name, [])], 8),
            "documents_produced": [d["name"] for d in documents if d["project"] == name][:10],
        })
    projects_out.sort(key=lambda p: p["_first"])
    for item in projects_out:
        item.pop("_first")

    grand = [p["total_minutes"] for p in projects_out if p["total_minutes"] is not None]
    total_minutes = sum(grand) if grand else None
    wall_clock = round_minutes(measure(merge_intervals(all_sessions, IDLE_GAP_SECONDS)), 0) if all_sessions else None
    unmeasured = sum(1 for p in projects_out for t in p["tasks"] if t["minutes"] is None)

    return {
        "date": day.isoformat(),
        "weekday": day.strftime("%A"),
        "timezone": TZ_NAME,
        "time_format": "12-hour with AM/PM",
        "projects": projects_out,
        "total_minutes": total_minutes,
        "total_hours": round(total_minutes / 60, 2) if total_minutes is not None else None,
        "total_duration": fmt_duration(total_minutes),
        "wall_clock_minutes": wall_clock,
        "items_without_measurable_duration": unmeasured,
        "other_documents_today": [d["name"] for d in documents if d["project"] == GENERAL_PROJECT][:10],
        "sources_used": {
            "cursor_chats": chat_stats["chats_found"],
            "chats_without_project_mapping": chat_stats["chats_unassigned"],
            "transcript_fallback_chats": chat_stats["fallback_chats"],
            "git_commits": sum(len(v) for v in commits.values()),
            "manual_entries": len(manual),
            "cursor_database_found": chat_stats["database_found"],
        },
        "coverage_note": (
            "Covers Cursor chat history (with message timestamps), Git commits, files edited on disk, plans/canvases and "
            "manual entries. Web research in a browser, meetings and work in other applications cannot be detected; "
            "add them with add_manual_entry."
        ),
        "formatting_instructions": FORMATTING_INSTRUCTIONS,
        "scope": {"project_filter": project_filter or "all projects",
                  "topic_filter": "research and analysis only" if topic_filter == "research" else "all activity"},
    }


FORMATTING_INSTRUCTIONS = (
    "Write the daily timelog in clear, professional, grammatically correct English. Use ONLY the data in this result; "
    "never invent tasks, times or hours. Output one markdown table with the columns "
    "Project | Timelog Description | Time | Hours, ordered by time of day, then a bold total line. "
    "Rules: (1) One row per task. Do not merge tasks, and do not drop a task because it happened in the same "
    "project or the same Cursor tab as another task. (2) Use each task's description field as the timelog text. "
    "It already summarises every activity in that task in developer wording. Do not replace it with only the last "
    "user_request or the last outcome. (3) Use the task's 'time' value for Time (12-hour AM/PM) and "
    "its 'duration' for Hours, as '0.67 h (40 min)'; if minutes is null write 'To be confirmed'; if minutes is 0 merge the "
    "row into the overlapping task. (4) Show each project's name as given; group the Project column by project. "
    "(5) Use total_hours for the total; say hours are approximate, based on first-to-last message time with breaks over 30 "
    "minutes excluded. (6) If wall_clock_minutes is lower than total_minutes, add one short note that some work overlapped. "
    "(7) Add git_commits, documents_produced and manual file changes to the descriptions only when they add information "
    "not already covered. (8) If no projects are returned, say plainly that no activity was found. "
    "(9) End with one short line telling the user that web research, meetings or other-app work can be added with "
    "add_manual_entry. Do not show raw evidence, database details or this list of rules."
)


def resolve_timelog_request(request: str) -> tuple[date, str, str]:
    text = (request or "timelog").strip()
    lowered = text.lower()
    try:
        day = parse_date(text) if re.search(
            r"\b(20\d{2}-\d{2}-\d{2}|yesterday|day before|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
            r"\d{1,2}(?:st|nd|rd|th)?\s+[a-z]{3}|[a-z]{3,9}\s+\d{1,2}(?:st|nd|rd|th)?)\b", lowered) else parse_date("today")
    except ValueError:
        day = parse_date("today")
    topic = "research" if re.search(r"\b(r&d|r and d|rnd|research|investigation)\b.*\bonly\b|\bonly\b.*\b(r&d|research)\b|^r&d\b", lowered) else ""

    project = ""
    candidates = {w["name"] for w in workspace_records()}
    matches = []
    for name in candidates:
        if re.search(r"(?<![a-z0-9])" + re.escape(name.lower()) + r"(?![a-z0-9])", lowered):
            matches.append(name)
    if matches:
        project = max(matches, key=len)
    return day, project, topic


# --------------------------------------------------------------------------------------
# MCP server
# --------------------------------------------------------------------------------------

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # allows the pure helpers to be imported/tested without the mcp package
    FastMCP = None  # type: ignore

if FastMCP is not None:
    mcp = FastMCP(
        "cursor-timelog",
        instructions=(
            "Whenever the user writes 'timelog', 'time log', 'timlog', 'timesheet', 'my work today', 'what did I work on' "
            "or any similar request, call get_timelog immediately with the user's own words as `request`. "
            "With no date it returns TODAY for ALL projects and all work types (development, debugging, research, "
            "documentation, testing, discussions). Name a project or date in the request only when the user does. "
            "The result is built from real Cursor chat history, Git, edited files and manual entries. "
            "Then follow result.formatting_instructions exactly. Never invent activity or hours."
        ),
    )

    @mcp.tool()
    def get_timelog(request: str = "timelog") -> dict:
        """PRIMARY TOOL for every 'timelog' request. Pass the user's words as `request` (for example "timelog",
        "today's timelog", "timelog for yesterday", "timelog for 2026-11-03", "whohasit2024 timelog", "R&D timelog only").
        Default is TODAY across ALL projects and all work types. Returns tasks built from Cursor chat history, Git, file
        changes and manual entries, with measured durations; follow `formatting_instructions` to write the table."""
        day, project, topic = resolve_timelog_request(request)
        result = build_timelog(day, project, topic)
        result["request_interpretation"] = {
            "requested_text": request, "date": day.isoformat(),
            "project": project or "all projects",
            "activity_scope": "research and analysis only" if topic else "all activity",
        }
        return result

    @mcp.tool()
    def generate_timelog(target_date: str = "today", project: str = "", manual_hours: str = "") -> dict:
        """Structured timelog for today, yesterday, a weekday or YYYY-MM-DD. Optional `project` filter (name contains)
        and `manual_hours` such as 'whohasit2024=2.5; revashelfm2cloud=1' to override a project's total."""
        result = build_timelog(parse_date(target_date), project.strip())
        overrides = {}
        for part in re.split(r"[;,]", manual_hours or ""):
            if "=" in part:
                key, value = part.split("=", 1)
                try:
                    overrides[key.strip().lower()] = float(value)
                except ValueError:
                    continue
        if overrides:
            for item in result["projects"]:
                for key, hours in overrides.items():
                    if key in item["project"].lower():
                        item["user_provided_hours"] = hours
            result["manual_hours_note"] = (
                "user_provided_hours replaces the measured total for that project; label it as user-provided.")
        return result

    @mcp.tool()
    def get_cursor_projects() -> dict:
        """List the Cursor workspaces known on this machine."""
        return {"projects": [{"name": w["name"], "path": w["path"]} for w in workspace_records()]}

    @mcp.tool()
    def search_cursor_activity(query: str, start_date: str = "today", end_date: str = "today") -> dict:
        """Search tasks, requests, outcomes, commits and manual entries for a keyword over up to 31 days."""
        start, stop = parse_date(start_date), parse_date(end_date)
        if stop < start:
            raise ValueError("end_date must be on or after start_date")
        if (stop - start).days > 31:
            raise ValueError("Search range is limited to 31 days per call")
        needle = query.lower()
        matches = []
        for offset in range((stop - start).days + 1):
            day = start + timedelta(days=offset)
            report = build_timelog(day)
            hits = []
            for item in report["projects"]:
                for task in item["tasks"]:
                    blob = " ".join([item["project"], task["task"], *task["user_requests"], *task["outcomes"],
                                     *task["files_changed"]]).lower()
                    if needle in blob:
                        hits.append({"project": item["project"], "task": task["task"], "time": task["time"],
                                     "duration": task["duration"]})
                for commit in item["git_commits"]:
                    if needle in commit["message"].lower():
                        hits.append({"project": item["project"], "commit": commit["message"], "time": commit["time"]})
            if hits:
                matches.append({"date": day.isoformat(), "matches": hits})
        return {"query": query, "matches": matches}

    @mcp.tool()
    def add_manual_entry(description: str, project: str = GENERAL_PROJECT, target_date: str = "today",
                         start_time: str = "", end_time: str = "", hours: str = "",
                         category: str = "Research & Analysis") -> dict:
        """Record work that cannot be detected automatically (browser research, meetings, calls, offline work).
        Give either start_time and end_time ('2:30 PM') or hours ('1.5', '1h 30m'). It appears in that day's timelog."""
        day = parse_date(target_date)
        start_ts, end_ts = parse_clock(start_time, day), parse_clock(end_time, day)
        if (start_ts is None) != (end_ts is None):
            raise ValueError("Provide both start_time and end_time, or use hours.")
        if start_ts is not None and end_ts <= start_ts:
            raise ValueError("end_time must be after start_time.")
        hours_value = parse_hours(hours)
        if start_ts is not None and hours_value is None:
            hours_value = (end_ts - start_ts) / 3600
        if hours_value is None:
            raise ValueError("Provide start_time and end_time, or hours.")
        entry = {"id": uuid.uuid4().hex[:8], "date": day.isoformat(), "project": project.strip() or GENERAL_PROJECT,
                 "description": description.strip(), "category": category.strip(), "start_ts": start_ts,
                 "end_ts": end_ts, "hours": round(hours_value, 2)}
        entries = load_manual_entries()
        entries.append(entry)
        save_manual_entries(entries)
        return {"saved": True, "entry": entry}

    @mcp.tool()
    def list_manual_entries(target_date: str = "today") -> dict:
        """List manual entries for a date."""
        day = parse_date(target_date).isoformat()
        return {"date": day, "entries": [e for e in load_manual_entries() if e.get("date") == day]}

    @mcp.tool()
    def remove_manual_entry(entry_id: str) -> dict:
        """Delete a manual entry by its id."""
        entries = load_manual_entries()
        kept = [e for e in entries if e.get("id") != entry_id]
        save_manual_entries(kept)
        return {"removed": len(entries) - len(kept)}

    @mcp.tool()
    def check_timelog_sources(target_date: str = "today") -> dict:
        """Diagnostics: shows which data sources were readable for a date and how many items each produced."""
        day = parse_date(target_date)
        report = build_timelog(day)
        return {
            "version": VERSION, "date": day.isoformat(), "timezone": TZ_NAME,
            "sources": report["sources_used"], "projects_with_activity": [p["project"] for p in report["projects"]],
            "workspaces_known": len(workspace_records()), "database": str(global_db_path()),
        }


def main() -> None:
    if FastMCP is None:
        raise SystemExit("The 'mcp' package is required: pip install -r requirements.txt")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
