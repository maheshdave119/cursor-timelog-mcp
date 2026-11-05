"""Turn a full list of chat turns into developer-style timelog sentences.

Every user turn is kept. Related follow-ups in the same tab are combined into one
summary. A new bug, exception, or feature starts its own summary instead of
replacing the earlier work.
"""
from __future__ import annotations

import re

GENERIC = {
    "cron", "crons", "job", "jobs", "exception", "exceptions", "error", "errors",
    "warning", "issue", "issues", "bug", "bugs", "module", "modules", "fix", "fixed",
    "update", "updated", "change", "changes", "project", "code", "file", "files",
    "please", "thanks", "thank", "work", "working", "test", "tested", "check",
    "hotfix", "revert", "reverted", "local", "run", "running",
}
FOLLOWUP = re.compile(
    r"^(thanks|thank you|so\b|ok\b|okay|yes\b|please (?:give|share|send|show)|"
    r"what is the fix|so what|if i\b|we can\b|ui also|how much|estimation)\b",
    re.I,
)
PROBLEM = re.compile(r"\b(exception|undefined property|typeerror|warning:|bug|not work|issue with)\b", re.I)
SHELL = re.compile(r"\b(ddev|bin/magento|n98-magerun|sys:cron:run)\b", re.I)


def _clean(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    text = re.sub(r"@(?=\S)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def identifiers(text: str) -> set[str]:
    found: set[str] = set()
    for match in re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", text or ""):
        word = match.lower()
        if word not in GENERIC:
            found.add(word)
    for match in re.findall(r"[A-Za-z0-9_]+(?:\\[A-Za-z0-9_]+)+", text or ""):
        found.add(match.lower())
    for match in re.findall(r"[\w.-]+\.(?:php|xml|js|phtml|json|md)", text or "", flags=re.I):
        found.add(match.lower())
    return found


def _is_shell(text: str) -> bool:
    return bool(SHELL.search(text))


def _is_problem(text: str) -> bool:
    return bool(PROBLEM.search(text))


def _group_ids(group: list[dict]) -> set[str]:
    ids: set[str] = set()
    for beat in group:
        ids |= identifiers(beat.get("request") or "")
    return ids


def _about_crons(group: list[dict]) -> bool:
    blob = " ".join(beat.get("request") or "" for beat in group).lower()
    return "cron" in blob


def cluster_beats(beats: list[dict]) -> list[list[dict]]:
    """Keep every turn. Fold follow-ups into the current topic; start a new topic for a new bug."""
    groups: list[list[dict]] = []
    for beat in beats:
        text = _clean(beat.get("request") or "")
        if not text:
            if groups:
                groups[-1].append(beat)
            continue
        if not groups:
            groups.append([beat])
            continue
        ids = identifiers(text)
        previous = _group_ids(groups[-1])
        new_ids = ids - previous
        follow = bool(FOLLOWUP.search(text))
        if _is_shell(text) and _about_crons(groups[-1]):
            groups[-1].append(beat)
        elif re.search(r"\b(revert|hotfix|hotfi|m2hotfix)\b", text, re.I) and re.search(
                r"\b(revert|hotfix|hotfi|m2hotfix)\b", " ".join(b.get("request") or "" for b in groups[-1]), re.I):
            groups[-1].append(beat)
        elif re.search(r"\b(renew|is_processing|status to \d|store ?view)\b", text, re.I) and re.search(
                r"\b(renew|membership|auto_renew)\b", " ".join(b.get("request") or "" for b in groups[-1]), re.I):
            groups[-1].append(beat)
        elif follow and not (_is_problem(text) and new_ids):
            groups[-1].append(beat)
        elif new_ids and _is_problem(text):
            groups.append([beat])
        elif ids & previous and not new_ids:
            groups[-1].append(beat)
        else:
            groups.append([beat])
    return [group for group in groups if any(_clean(b.get("request") or "") for b in group)]


def _human_name(token: str) -> str:
    token = token.replace("\\", " ").replace("_", " ").replace(".php", "")
    token = re.sub(r"\s+", " ", token).strip()
    return token


def _join(items: list[str]) -> str:
    items = [item.strip() for item in items if item and item.strip()]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + ", and " + items[-1]


def _sentence(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip(" ,;")
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    if text[-1] not in ".!?":
        text += "."
    return text


def clause_from_request(text: str) -> str | None:
    """One past-tense clause from a single user message, or None when it adds no new work."""
    raw = _clean(text)
    if len(raw) > 320:
        raw = raw[:320].rsplit(" ", 1)[0]
    if not raw or FOLLOWUP.search(raw):
        return None
    low = raw.lower()

    if _is_shell(raw) and not re.search(r"\bissue with\b", low):
        job = re.search(r"cron:run\s+([A-Za-z0-9_]+)", raw)
        if not job:
            return None
        name = re.sub(r"\s+cron$", "", _human_name(job.group(1)))
        clause = "ran the " + name + " cron locally"
        if re.search(r"fatal error|undefined method", low):
            clause += " and found a fatal error from an undefined method"
        elif re.search(r"deprecated|dynamic property", low):
            clause += " and found a dynamic-property deprecation"
        return clause

    codes = list(dict.fromkeys(re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", low)))
    if len(codes) >= 3 and not re.search(r"\b(warning:|fatal error|undefined property)\b", low):
        names = [_human_name(code) for code in codes[:4]]
        more = " and the other selected jobs" if len(codes) > 4 else ""
        return "reviewed the " + _join(names) + more + " before running them"

    warning = re.search(
        r"undefined property:\s*([A-Za-z0-9_\\]+)::\$?([A-Za-z0-9_]+)", raw, re.I
    )
    if warning:
        owner = _human_name(warning.group(1).split("\\")[-2] + " " + warning.group(1).split("\\")[-1]
                            if "\\" in warning.group(1) else warning.group(1))
        # Prefer Vendor Feature from the class path: Zynovo\Stripe\Model\Config -> Zynovo Stripe
        parts = warning.group(1).split("\\")
        if len(parts) >= 2:
            owner = _human_name(parts[0] + " " + parts[1])
        return f"investigated and fixed the undefined {warning.group(2).lstrip('$')} property warning in {owner}"

    if re.search(r"\b(not renew|only update status|status to \d|is_processing)\b", low):
        return ("investigated the membership renewal cron, which ran but only updated the status "
                "and did not complete the renewal or place an order")

    if re.search(r"\bdeleted\b", low) and "module" in low:
        name = re.search(r"deleted (?:the )?([a-z0-9 ]{3,40}?) module", low)
        label = name.group(1).strip() if name else "unused"
        return f"removed the {label} module to avoid a conflict"

    if re.search(r"store ?view", low):
        return "updated the same configuration in the default store view and reran the cron, which still only changed the status"

    if re.search(r"\b(exception|typeerror|fatal error)\b", low) and not re.search(r"\b(no|without|any)\s+(?:any\s+)?exception", low):
        module = re.search(r"\bin the ([a-z0-9][a-z0-9 /-]{2,40}?)(?: module)?\b", low)
        subject = re.search(r"\b(?:related to|about|for|in)\s+(?:the\s+)?([a-z0-9][a-z0-9 /-]{2,40})", low)
        if module:
            return "investigated an exception in the " + module.group(1).strip() + " module"
        if subject and subject.group(1).strip() not in {"this", "that", "it"}:
            return "investigated and fixed the " + subject.group(1).strip() + " exception"
        named = re.search(r"exception[^A-Za-z0-9]{0,20}([A-Z][A-Za-z0-9_\\]+)", raw)
        if named:
            return "investigated and fixed the " + _human_name(named.group(1)) + " exception"
        return "investigated and fixed an exception"

    issue = re.search(r"issue with (?:the )?([a-z0-9_\\ ]{3,60}?)(?: cron\b|,|\.|$)", low)
    if issue:
        name = _human_name(issue.group(1))
        extra = ""
        if "status" in low and re.search(r"\bnot\b", low):
            extra = ", which was updating a status instead of completing the renewal"
        return f"investigated the {name} cron{extra}"

    if re.search(r"\brevert", low):
        file_name = re.search(r"([\w.-]+\.(?:php|xml|js|phtml))", raw, re.I)
        where = f" in {file_name.group(1)}" if file_name else ""
        return f"reverted the direct code changes{where} so the fix can be delivered as a hotfix"

    if "hotfix" in low or "m2hotfix" in low or "m2-hotfix" in low:
        return "prepared the Magento hotfix for the change"

    if re.search(r"\bdry[- ]?run\b|\bwithout (?:perform|sending|any real)\b|\bno any email\b|\bsafe way\b", low):
        return "worked out a safe way to verify the cron jobs without sending customer emails or changing live data"

    if re.search(r"\b(list of|prepare list|which are currently)\b", low) and "cron" in low:
        return "prepared a list of the custom cron jobs, including their schedule, code, and purpose"

    if re.search(r"\b(does not send email|not sensitive|simple one)\b", low):
        return "separated the cron jobs that are safe to check first from the ones that send email or change live data"

    if re.search(r"\bfix(?:ed)?\b", low):
        what = re.search(r"\bfix(?:ed)?(?: the| a| an)? ([a-z0-9][a-z0-9 _/-]{2,50})", low)
        if what:
            return "fixed the " + what.group(1).strip().rstrip(".")
        return "fixed the reported issue"

    related = re.search(r"related to (?:the )?([a-z0-9][a-z0-9 /-]{2,40})", low)
    if related and re.search(r"\b(exception|issue|error)\b", low):
        kind = "exception" if "exception" in low else "issue"
        return f"resolved the {related.group(1).strip()} {kind}"

    if re.search(r"\bresolv", low):
        what = re.search(r"\bresolv\w*(?: the| another)? ([a-z0-9][a-z0-9 _/-]{2,50})", low)
        if what:
            return "resolved the " + what.group(1).strip().rstrip(".")
        return "resolved the reported issue"

    if re.search(r"\berror handling\b", low):
        return "improved the error handling"

    if re.search(r"\bupdated\b", low):
        what = re.search(r"\bupdated (?:the )?([a-z0-9][a-z0-9 _/-]{2,40})", low)
        if what:
            return "updated the " + what.group(1).strip().rstrip(".")
        return "updated the related implementation"

    if re.search(r"\binvestigat", low):
        module = re.search(r"\bin the ([a-z0-9][a-z0-9 /-]{2,40})", low)
        if module:
            return "investigated an exception in the " + module.group(1).strip()
        return "investigated the reported problem"

    if re.search(r"\b(test|tested|validate|verified)\b", low):
        flow = re.search(r"\b(?:test(?:ed)?|validate[d]?|verified) (?:the )?([a-z0-9][a-z0-9 _/-]{2,40})", low)
        if flow:
            return "tested the " + flow.group(1).strip().rstrip(".")
        return "tested the affected functionality"

    if re.search(r"\b(document|user story|readme)\b", low):
        return "documented the requirement and the expected behaviour"

    return None


def _similar(left: str, right: str) -> bool:
    a, b = set(left.lower().split()), set(right.lower().split())
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) >= 0.7


def _unique(clauses: list[str]) -> list[str]:
    kept: list[str] = []
    for clause in clauses:
        if any(_similar(clause, item) for item in kept):
            continue
        kept.append(clause)
    runs = [c for c in kept if c.startswith("ran the ") and c.endswith(" cron locally")]
    if len(runs) >= 2:
        names = list(dict.fromkeys(c[len("ran the "):-len(" cron locally")] for c in runs))
        shown = names[:4]
        extra = len(names) - len(shown)
        summary = "ran the " + _join(shown) + " cron jobs locally"
        if extra:
            summary += f", along with {extra} more,"
        summary += " to confirm they execute"
        kept = [summary] + [c for c in kept if c not in runs]
    return kept


def _issue_label(clause: str) -> str:
    text = re.sub(
        r"^(investigated and fixed|investigated|fixed|resolved) (?:the |an |a )?",
        "",
        clause,
        flags=re.I,
    ).rstrip(".")
    text = re.sub(r"^exception in the ", "", text)
    return text.strip()


def narrate_commits(messages: list[str]) -> str:
    cleaned = [re.sub(r"\s+", " ", item).strip().rstrip(".") for item in messages if item and item.strip()]
    if not cleaned:
        return ""
    return _sentence("Committed these updates: " + _join(cleaned[:6]))


def narrate(requests: list[str]) -> str:
    """One or two natural sentences covering every distinct action in the requests."""
    clauses = _unique([c for c in (clause_from_request(item) for item in requests) if c])
    if not clauses:
        return ""
    modules: list[str] = []
    issues: list[str] = []
    extras: list[str] = []
    for clause in clauses:
        low = clause.lower()
        module = re.search(r"\bin the ([a-z0-9][a-z0-9 /-]{2,40}?) module\b", low)
        if module and module.group(1) not in modules:
            modules.append(module.group(1))
        if re.match(r"^(investigated|fixed|resolved)\b", low) and " which " not in low and len(clause) < 90:
            label = _issue_label(clause)
            if label.lower().endswith(" module") or label.lower() in modules:
                pass
            elif label and label not in issues and not label.startswith("an exception in the"):
                issues.append(label)
        else:
            extras.append(clause)
    parts: list[str] = []
    if modules:
        names = _join([name + " module" for name in modules])
        parts.append("Worked on the " + names)
    if issues:
        labelled = [item if item.startswith(("the ", "a ", "an ")) else "the " + item for item in issues]
        parts.append("investigated and resolved " + _join(labelled))
    for extra in extras:
        if modules and extra.lower().startswith("investigated an exception in the"):
            continue
        parts.append(extra)
    if not parts:
        parts = clauses
    sentences: list[str] = []
    current: list[str] = []
    for part in parts:
        if current and len(" ".join(current)) + len(part) > 200:
            sentences.append(_sentence(_join(current)))
            current = []
        current.append(part)
    if current:
        sentences.append(_sentence(_join(current)))
    return " ".join(sentences)
