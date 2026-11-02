"""Small persistent character-card and keyword-lorebook store for the first-party chat.

No model engine changes. Existing OpenAI-compatible requests remain untouched.
SQLite file sits next to SessionV1's server-owned conversations database.
"""
from __future__ import annotations

import base64
import binascii
import io
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

CARD_ID = re.compile(r"^[a-f0-9]{32}$")
SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FIELDS = {"name": 80, "description": 6000, "personality": 4000,
          "scenario": 4000, "first_mes": 3000, "mes_example": 4000}
MAX_CARDS = 200
MAX_LORE = 100
MAX_SOURCES = 12
MAX_JSON_BYTES = 256 * 1024


def _text(value, limit: int, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field}: expected text")
    if len(value) > limit:
        raise ValueError(f"{field}: exceeds {limit} characters")
    return value.strip()


def valid_card(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("card must be an object")
    card = {f: _text(data.get(f, ""), limit, f) for f, limit in FIELDS.items()}
    if not card["name"]:
        raise ValueError("character name is required")
    ident = data.get("id") or uuid4().hex
    if not isinstance(ident, str) or not CARD_ID.fullmatch(ident):
        raise ValueError("invalid character id")
    card["id"] = ident
    mode = data.get("persona_mode", "global")
    if mode not in ("none", "global", "independent"):
        raise ValueError("persona_mode must be none, global, or independent")
    card["persona_mode"] = mode
    card["user_name"] = _text(data.get("user_name", ""), 80, "user_name")
    card["user_description"] = _text(data.get("user_description", ""), 4000, "user_description")
    raw_lore = data.get("lorebook", [])
    if not isinstance(raw_lore, list) or len(raw_lore) > MAX_LORE:
        raise ValueError("lorebook must be a list of at most 100 entries")
    lore = []
    for entry in raw_lore:
        if not isinstance(entry, dict):
            raise ValueError("lore entry must be an object")
        keys = _text(entry.get("keys", ""), 300, "lore keys")
        content = _text(entry.get("content", ""), 2500, "lore content")
        constant = entry.get("constant", False)
        if not isinstance(constant, bool):
            raise ValueError("lore constant must be boolean")
        if content and (keys or constant):
            lore.append({"keys": keys, "content": content, "constant": constant})
    card["lorebook"] = lore
    raw_sources = data.get("sources", [])
    if not isinstance(raw_sources, list) or len(raw_sources) > MAX_SOURCES:
        raise ValueError("Markdown sources must be a list of at most 12 items")
    sources = []
    for item in raw_sources:
        if not isinstance(item, dict) or item.get("kind") not in ("file", "directory"):
            raise ValueError("source kind must be file or directory")
        path = _text(item.get("path", ""), 1024, "source path")
        if not path:
            continue
        recursive = item.get("recursive", True)
        if not isinstance(recursive, bool):
            raise ValueError("source recursive must be boolean")
        sources.append({"kind": item["kind"], "path": path, "recursive": recursive})
    card["sources"] = sources
    workflow_path = _text(data.get("workflow_path", ""), 1024, "workflow path")
    card["workflow_path"] = workflow_path
    if len(json.dumps(card, ensure_ascii=False).encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("card is too large")
    return card


def card_from_tavern(raw: object) -> dict:
    """Import standard JSON character cards (V1, V2 and V3 data wrapper).
    PNG metadata is intentionally not parsed in this lightweight first version.
    """
    if not isinstance(raw, dict):
        raise ValueError("import file must contain a JSON object")
    data = raw.get("data", raw)
    if not isinstance(data, dict):
        raise ValueError("character data must be a JSON object")
    out = {k: data.get(k, "") for k in FIELDS}
    out["lorebook"] = []
    book = data.get("character_book") or {}
    if isinstance(book, dict):
        for entry in book.get("entries", []):
            if not isinstance(entry, dict) or not entry.get("enabled", True):
                continue
            keys = entry.get("keys") or entry.get("key") or []
            out["lorebook"].append({
                "keys": ", ".join(str(k) for k in keys) if isinstance(keys, list) else str(keys),
                "content": entry.get("content", ""),
                "constant": bool(entry.get("constant", False)),
            })
    return valid_card(out)


def card_to_tavern(card: dict) -> dict:
    data = {k: card.get(k, "") for k in FIELDS}
    data["character_book"] = {
        "name": data["name"] + " lorebook",
        "entries": [{"keys": [k.strip() for k in item["keys"].split(",") if k.strip()],
                     "content": item["content"], "enabled": True,
                     "constant": item["constant"]}
                    for item in card.get("lorebook", [])],
    }
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}


class CharacterStore:
    def __init__(self, root: str | Path):
        self.path = Path(root) / "character_cards.sqlite3"
        self.image_dir = Path(root) / "character_images"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS cards (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS options (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS assignments (session_id TEXT PRIMARY KEY, card_id TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS workflow_sessions ("
                       "session_id TEXT NOT NULL, card_id TEXT NOT NULL, "
                       "snapshot TEXT NOT NULL, initialized INTEGER NOT NULL DEFAULT 0, "
                       "PRIMARY KEY (session_id, card_id))")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def snapshot(self) -> dict:
        with self._db() as db:
            cards = [json.loads(row[0]) for row in db.execute("SELECT data FROM cards ORDER BY rowid")]
            rows = db.execute("SELECT session_id,card_id FROM assignments").fetchall()
            settings = {r[0]:r[1] for r in db.execute(
                "SELECT key,value FROM options WHERE key IN "
                "('user_name','user_description','default_role','chat_image_profile')")}
        return {"cards": cards, "assignments": {r[0]:r[1] for r in rows},
                "global": {"user_name": settings.get("user_name", ""),
                           "user_description": settings.get("user_description", ""),
                           "default_role": settings.get("default_role", "stateless"),
                           "chat_image_profile": settings.get("chat_image_profile", "default")}}

    def save(self, raw: object) -> dict:
        card = valid_card(raw)
        with self._db() as db:
            n = db.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
            existing = db.execute("SELECT data FROM cards WHERE id=?", (card["id"],)).fetchone()
            exists = existing is not None
            # Do not trust reference_image from form JSON: only validated uploads set it.
            card["reference_image"] = bool(existing and json.loads(existing[0]).get("reference_image"))
            if not exists and n >= MAX_CARDS:
                raise ValueError("maximum 200 character cards reached")
            db.execute("INSERT OR REPLACE INTO cards(id,data) VALUES(?,?)",
                       (card["id"], json.dumps(card, ensure_ascii=False)))
        return card

    def image_file(self, ident: str) -> Path | None:
        if not isinstance(ident, str) or not CARD_ID.fullmatch(ident):
            raise ValueError("invalid character id")
        card = self.get(ident)
        file = self.image_dir / (ident + ".png")
        return file if card and card.get("reference_image") and file.is_file() else None

    def upload_reference(self, ident: str, encoded: object) -> dict:
        """Normalize a bounded image to a private, model-independent PNG."""
        if not isinstance(ident, str) or not CARD_ID.fullmatch(ident):
            raise ValueError("invalid character id")
        if not isinstance(encoded, str) or len(encoded) > 7_200_000:
            raise ValueError("image must be base64 and no larger than 5 MB")
        if not self.get(ident):
            raise ValueError("save the character before uploading an image")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as e:
            raise ValueError("invalid image encoding") from e
        if not raw or len(raw) > 5 * 1024 * 1024:
            raise ValueError("image must be between 1 byte and 5 MB")
        try:
            from PIL import Image, ImageOps, UnidentifiedImageError
            with Image.open(io.BytesIO(raw)) as probe:
                if probe.format not in ("PNG", "JPEG", "WEBP") or probe.width * probe.height > 16_000_000:
                    raise ValueError("use PNG, JPEG or WebP up to 16 megapixels")
                probe.verify()
            with Image.open(io.BytesIO(raw)) as image:
                image = ImageOps.exif_transpose(image)
                image.thumbnail((1600, 1600))
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                result = io.BytesIO()
                image.save(result, format="PNG", optimize=True)
                payload = result.getvalue()
        except (OSError, ValueError) as e:
            raise ValueError("invalid or unsupported image") from e
        if len(payload) > 8 * 1024 * 1024:
            raise ValueError("processed image exceeds 8 MB")
        self.image_dir.mkdir(parents=True, exist_ok=True)
        dest = self.image_dir / (ident + ".png")
        temp = self.image_dir / (ident + ".tmp")
        try:
            temp.write_bytes(payload)
            temp.replace(dest)
            with self._db() as db:
                existing = db.execute("SELECT data FROM cards WHERE id=?", (ident,)).fetchone()
                if not existing:
                    raise ValueError("character was deleted during upload")
                card = json.loads(existing[0]); card["reference_image"] = True
                db.execute("UPDATE cards SET data=? WHERE id=?", (json.dumps(card, ensure_ascii=False), ident))
            return card
        finally:
            temp.unlink(missing_ok=True)

    def remove_reference(self, ident: str) -> dict:
        if not isinstance(ident, str) or not CARD_ID.fullmatch(ident):
            raise ValueError("invalid character id")
        with self._db() as db:
            current = db.execute("SELECT data FROM cards WHERE id=?", (ident,)).fetchone()
            if not current:
                raise ValueError("character not found")
            card = json.loads(current[0]);card["reference_image"] = False
            db.execute("UPDATE cards SET data=? WHERE id=?",(json.dumps(card,ensure_ascii=False),ident))
        (self.image_dir / (ident + ".png")).unlink(missing_ok=True)
        return card

    def remove(self, ident: str) -> None:
        if not isinstance(ident, str) or not CARD_ID.fullmatch(ident):
            raise ValueError("invalid character id")
        with self._db() as db:
            db.execute("DELETE FROM cards WHERE id=?", (ident,))
            db.execute("DELETE FROM assignments WHERE card_id=?", (ident,))
            db.execute("DELETE FROM workflow_sessions WHERE card_id=?", (ident,))
            db.execute("UPDATE options SET value='stateless' WHERE key='default_role' AND value=?", (ident,))
        (self.image_dir / (ident + ".png")).unlink(missing_ok=True)

    def assign(self, session: str, ident: str | None) -> None:
        if not isinstance(session, str) or not SESSION_ID.fullmatch(session):
            raise ValueError("invalid session id")
        if ident is not None and ident not in ("stateless", "ordinary") and (
                not isinstance(ident, str) or not CARD_ID.fullmatch(ident)):
            raise ValueError("invalid character id")
        with self._db() as db:
            if ident is not None and ident not in ("stateless", "ordinary") and not db.execute(
                    "SELECT 1 FROM cards WHERE id=?", (ident,)).fetchone():
                raise ValueError("character not found")
            db.execute("DELETE FROM assignments WHERE session_id=?", (session,))
            if ident is not None:
                db.execute("INSERT INTO assignments VALUES(?,?)", (session, ident))

    def set_global(self, raw: object, *, allowed_image_profiles: set[str] | None = None) -> dict:
        if not isinstance(raw, dict):
            raise ValueError("global settings must be an object")
        name = _text(raw.get("user_name", ""), 80, "user_name")
        description = _text(raw.get("user_description", ""), 4000, "user_description")
        default_role = raw.get("default_role", "stateless")
        if default_role not in ("stateless", "ordinary"):
            if not isinstance(default_role, str) or not CARD_ID.fullmatch(default_role):
                raise ValueError("invalid default role")
            if not self.get(default_role):
                raise ValueError("default role does not exist")
        # Older clients may omit the field; preserve the previous image model.
        with self._db() as db:
            prior = db.execute("SELECT value FROM options WHERE key='chat_image_profile'").fetchone()
        chat_image_profile = raw.get("chat_image_profile", prior[0] if prior else "default")
        if not isinstance(chat_image_profile, str) or len(chat_image_profile) > 64:
            raise ValueError("invalid chat image profile")
        if (allowed_image_profiles is not None
                and chat_image_profile not in allowed_image_profiles):
            raise ValueError("chat image profile is not configured")
        with self._db() as db:
            db.executemany("INSERT OR REPLACE INTO options VALUES(?,?)", [
                ("user_name", name), ("user_description", description),
                ("default_role", default_role),
                ("chat_image_profile", chat_image_profile)])
        return {"user_name": name, "user_description": description,
                "default_role": default_role, "chat_image_profile": chat_image_profile}

    def chat_image_profile(self) -> str:
        """Current global image preference; never returns a user file path."""
        with self._db() as db:
            row = db.execute("SELECT value FROM options WHERE key='chat_image_profile'").fetchone()
        return row[0] if row else "default"

    def assigned_card(self, session: str) -> dict | None:
        if not isinstance(session, str) or not SESSION_ID.fullmatch(session):
            raise ValueError("invalid session id")
        with self._db() as db:
            row = db.execute(
                "SELECT cards.data FROM cards JOIN assignments ON cards.id=assignments.card_id "
                "WHERE assignments.session_id=?", (session,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def get(self, ident: str) -> dict | None:
        if not isinstance(ident, str) or not CARD_ID.fullmatch(ident):
            raise ValueError("invalid character id")
        with self._db() as db:
            row = db.execute("SELECT data FROM cards WHERE id=?", (ident,)).fetchone()
        return json.loads(row[0]) if row else None


def compose(card: dict | None, persona: str, messages: list[dict],
            *, lore_limit: int = 3500) -> tuple[str, list[str]]:
    """Deterministic, bounded first-party prompt section; no extra model calls."""
    if not card and not persona:
        return "", []
    parts = []
    if card:
        parts.append(f"You are roleplaying as {card['name']}. Stay consistent with the character.")
        for label, field in (("Description", "description"), ("Personality", "personality"),
                             ("Scenario", "scenario"), ("Dialogue examples", "mes_example")):
            if card.get(field):
                parts.append(f"{label}:\n{card[field]}")
    if persona:
        parts.append("User persona:\n" + persona[:4000])
    # Search just the recent chat, not generated system text or activated lore.
    searchable = "\n".join(
        str(m.get("content", "")) for m in messages[-12:]
        if isinstance(m, dict) and m.get("role") in ("user", "assistant")
        and isinstance(m.get("content"), str)
    ).casefold()
    matched = []
    total = 0
    for entry in (card or {}).get("lorebook", []):
        keys = [k.strip().casefold() for k in entry["keys"].split(",") if k.strip()]
        if not (entry["constant"] or any(k in searchable for k in keys)):
            continue
        content = entry["content"]
        if total + len(content) > lore_limit:
            continue
        matched.append(entry["keys"] if keys else "(always)")
        total += len(content)
        parts.append(f"World information ({matched[-1]}):\n{content}")
    return "\n\n".join(parts), matched
