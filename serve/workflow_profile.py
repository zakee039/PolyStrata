"""PolyStrata opt-in Markdown Workflow Profiles.

A workflow is a *prompt protocol*, not executable code. Its first-turn section
is supplied to the LLM only on its first successful user reply; continuous
rules are supplied on every turn. Already-started sessions freeze one version
of their Markdown so hot edits don't silently change ongoing conversations.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from serve.character_cards import CharacterStore, SESSION_ID

MAX_WORKFLOW_BYTES = 64 * 1024
MAX_WORKFLOW_CHARS = 16000
MAX_SECTION_CHARS = 9000

CATEGORIES = {
    "initial": ("初始化", "initialization", "first turn", "first message", "startup"),
    "persistent": ("全程行为约束", "全程约束", "持续约束", "行为约束", "全程规则",
                   "always", "global rules", "persistent rules", "behavior"),
    "per_turn": ("每轮工作流程", "每轮流程", "每轮", "处理步骤",
                 "per turn", "every turn", "routine"),
    "tool_policy": ("工具权限", "工具使用", "工具规则", "tool permissions", "tool policy"),
}
HEAD = re.compile(r"^#{1,3}\s+(.+?)\s*#*\s*$")


def read_workflow(path: str) -> dict:
    p = Path(path)
    if (not p.is_absolute() or p.suffix.lower() not in (".md", ".markdown")
            or p.is_symlink() or getattr(p, "is_junction", lambda: False)()):
        raise ValueError("workflow must be a local absolute Markdown file path")
    try:
        if not p.is_file():
            raise ValueError("workflow file does not exist")
        size = p.stat().st_size
        if size < 1 or size > MAX_WORKFLOW_BYTES:
            raise ValueError("workflow Markdown must be between 1 and 65536 bytes")
        raw = p.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise ValueError(f"cannot read workflow: {type(e).__name__}") from e
    except UnicodeError as e:
        raise ValueError("workflow must be UTF-8 Markdown") from e
    if "\x00" in raw or len(raw) > MAX_WORKFLOW_CHARS:
        raise ValueError("workflow is too large or contains binary text")
    return {"source": str(p.resolve()), "sha256": hashlib.sha256(raw.encode("utf8")).hexdigest(),
            "sections": parse_workflow(raw)}


def parse_workflow(markdown: str) -> dict:
    """Keep headings simple and tolerate unstructured existing Markdown files."""
    sections = {name: [] for name in CATEGORIES}
    active = "persistent"
    matched_heading = False
    for line in markdown.splitlines():
        h = HEAD.match(line)
        if h:
            title = h.group(1).strip().casefold()
            choice = next((name for name, words in CATEGORIES.items()
                           if any(word in title for word in words)), None)
            if choice:
                active = choice
                matched_heading = True
                continue
            if matched_heading:
                # Unknown headings inside a recognized section still belong to it.
                sections[active].append(line)
                continue
        sections[active].append(line)
    result = {name: "\n".join(lines).strip() for name, lines in sections.items()}
    for name, text in result.items():
        if len(text) > MAX_SECTION_CHARS:
            raise ValueError(f"workflow section {name} exceeds {MAX_SECTION_CHARS} characters")
    return result


class WorkflowRuntime:
    def __init__(self, cards: CharacterStore):
        self.cards = cards

    def inspect(self, card_id: str) -> dict:
        card = self.cards.get(card_id)
        if not card:
            raise ValueError("character not found")
        path = card.get("workflow_path") or ""
        if not path:
            return {"enabled": False}
        workflow = read_workflow(path)
        return {"enabled": True, **workflow}

    def prepare(self, session_id: str, first_user_turn: bool) -> dict:
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            raise ValueError("invalid session id")
        if not isinstance(first_user_turn, bool):
            raise ValueError("first_user_turn must be boolean")
        card = self.cards.assigned_card(session_id)
        if not card or not card.get("workflow_path"):
            return {"enabled": False}
        card_id = card["id"]
        with self.cards._db() as db:
            row = db.execute("SELECT snapshot,initialized FROM workflow_sessions "
                             "WHERE session_id=? AND card_id=?", (session_id, card_id)).fetchone()
        if row:
            snapshot, initialized = json.loads(row["snapshot"]), bool(row["initialized"])
        else:
            snapshot = read_workflow(card["workflow_path"])
            # Existing chats with more than one user message must not start a
            # surprise first-turn workflow; continuous rules still apply.
            initialized = not first_user_turn or not bool(snapshot["sections"]["initial"])
            with self.cards._db() as db:
                db.execute("INSERT OR IGNORE INTO workflow_sessions "
                           "(session_id,card_id,snapshot,initialized) VALUES(?,?,?,?)",
                           (session_id, card_id, json.dumps(snapshot, ensure_ascii=False), int(initialized)))
                row = db.execute("SELECT snapshot,initialized FROM workflow_sessions "
                                 "WHERE session_id=? AND card_id=?", (session_id, card_id)).fetchone()
                snapshot, initialized = json.loads(row["snapshot"]), bool(row["initialized"])
        return {"enabled": True, "card_id": card_id, "source": snapshot["source"],
                "sha256": snapshot["sha256"], "sections": snapshot["sections"],
                "initial_pending": not initialized}

    def commit(self, session_id: str) -> dict:
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            raise ValueError("invalid session id")
        card = self.cards.assigned_card(session_id)
        if not card:
            return {"committed": False}
        with self.cards._db() as db:
            update = db.execute("UPDATE workflow_sessions SET initialized=1 "
                                "WHERE session_id=? AND card_id=?",
                                (session_id, card["id"]))
        return {"committed": update.rowcount == 1}
