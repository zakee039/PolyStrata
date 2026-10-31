"""PolyStrata V1 local conversations, image/file assets and disposable FIFO KV snapshots.

All persistent content belongs to the deployment directory:
  chat-history/conversations.sqlite3
  chat-history/images/<session-id>/<sha256>.<extension>
  chat-history/attachments/<session-id>/<sha256>.txt
  kv-cache/<session-id>.bin
"""
from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DATA_URL = re.compile(r"^data:(image/(?:png|jpeg|jpg|webp|gif|bmp));base64,(.*)$", re.I | re.S)
IMAGE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
             "image/webp": "webp", "image/gif": "gif", "image/bmp": "bmp"}
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 1024 * 1024
MAX_MESSAGES = 5000


def valid_id(value):
    if not isinstance(value, str) or not SESSION_ID.fullmatch(value):
        raise ValueError("strata_session_id must be 1-64 letters, digits, _ or -")
    return value


def json_value(value):
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("conversation metadata exceeds 8 MiB")
    return raw


def _magic_ok(mime: str, raw: bytes):
    return (raw.startswith(b"\x89PNG\r\n\x1a\n") if mime == "image/png" else
            raw.startswith(b"\xff\xd8\xff") if mime in ("image/jpeg", "image/jpg") else
            raw.startswith(b"RIFF") and raw[8:12] == b"WEBP" if mime == "image/webp" else
            raw.startswith((b"GIF87a", b"GIF89a")) if mime == "image/gif" else
            raw.startswith(b"BM") if mime == "image/bmp" else False)


class SessionV1:
    def __init__(self, directory: str, max_snapshots: int = 5, kv_directory: str | None = None):
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.generated_dir = self.root / "generated-images"
        self.generated_dir.mkdir(exist_ok=True)
        self.images_dir = self.root / "images"
        self.attachments_dir = self.root / "attachments"
        self.images_dir.mkdir(exist_ok=True)
        self.attachments_dir.mkdir(exist_ok=True)
        self.kv_dir = Path(kv_directory).resolve() if kv_directory else self.root / "kv-cache"
        self.kv_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "conversations.sqlite3"
        self.lock = threading.RLock()
        self.max_snapshots = max(1, min(int(max_snapshots), 5))
        self.active_id = None
        self.active_dirty = False
        self.active_gen = None
        with self._db() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created REAL NOT NULL,
                updated REAL NOT NULL,
                api_messages TEXT NOT NULL DEFAULT '[]',
                web_messages TEXT NOT NULL DEFAULT '[]',
                snapshot_seq INTEGER NOT NULL DEFAULT 0,
                snapshot_model TEXT NOT NULL DEFAULT '',
                snapshot_tokens INTEGER NOT NULL DEFAULT 0
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS ix_conv_updated ON conversations(updated DESC)")

    @contextmanager
    def _db(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _title(messages):
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "user":
                txt = msg.get("text", msg.get("content", ""))
                if isinstance(txt, list):
                    txt = " ".join(str(x.get("text", "")) for x in txt if isinstance(x, dict))
                if isinstance(txt, str) and txt.strip():
                    return txt.strip().replace("\n", " ")[:70]
        return None

    def _ensure(self, c, ident):
        now = time.time()
        c.execute("INSERT OR IGNORE INTO conversations(id,title,created,updated) VALUES(?,?,?,?)",
                  (ident, "New chat", now, now))

    @staticmethod
    def _write_asset(parent: Path, ident: str, raw: bytes, extension: str):
        if not raw:
            raise ValueError("empty attachment is not supported")
        folder = parent / valid_id(ident)
        folder.mkdir(exist_ok=True)
        digest = hashlib.sha256(raw).hexdigest()
        destination = folder / (digest + "." + extension)
        if not destination.exists():
            tmp = folder / (digest + "." + extension + "." + str(os.getpid()) + ".part")
            try:
                with tmp.open("xb") as f:
                    f.write(raw)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, destination)
            finally:
                tmp.unlink(missing_ok=True)
        return destination

    def _image_ref(self, ident, value):
        match = DATA_URL.match(value)
        if not match:
            return value                       # remote URL or already-referenced local image
        mime, payload = match.groups()
        mime = mime.lower()
        if len(payload) > MAX_IMAGE_BYTES * 4 // 3 + 16:
            raise ValueError("image exceeds 25 MiB")
        try:
            raw = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("invalid base64 image") from None
        if len(raw) > MAX_IMAGE_BYTES or not _magic_ok(mime, raw):
            raise ValueError("invalid image content or image exceeds 25 MiB")
        file = self._write_asset(self.images_dir, ident, raw, IMAGE_EXT[mime])
        return {"_session_image": file.name, "mime": mime}

    def _text_ref(self, ident, value):
        raw = value.encode("utf-8")
        if len(raw) > MAX_ATTACHMENT_BYTES:
            raise ValueError("text attachment exceeds 1 MiB")
        file = self._write_asset(self.attachments_dir, ident, raw, "txt")
        return {"_session_text": file.name}

    def _pack(self, ident, data):
        # Encode embedded images into real files, never huge base64 strings in SQLite.
        if isinstance(data, list):
            return [self._pack(ident, x) for x in data]
        if isinstance(data, dict):
            if isinstance(data.get("url"), str) and DATA_URL.match(data["url"]):
                result = dict(data)
                result["url"] = self._image_ref(ident, result["url"])
                return {k: self._pack(ident, v) for k, v in result.items()}
            return {k: (self._image_ref(ident, v) if k == "url" and isinstance(v, str) else
                         self._text_ref(ident, v) if k == "text" and
                         data.get("kind") == "file" and isinstance(v, str) else
                         self._pack(ident, v)) for k, v in data.items()}
        return data

    def _unpack(self, ident, data):
        if isinstance(data, list):
            return [self._unpack(ident, x) for x in data]
        if isinstance(data, dict):
            if "_session_image" in data:
                name = data["_session_image"]
                if not re.fullmatch(r"[0-9a-f]{64}\.(?:png|jpg|webp|gif|bmp)", name):
                    raise ValueError("invalid stored image reference")
                path = self.images_dir / valid_id(ident) / name
                return "data:" + data["mime"] + ";base64," + base64.b64encode(path.read_bytes()).decode("ascii")
            if "_session_text" in data:
                name = data["_session_text"]
                if not re.fullmatch(r"[0-9a-f]{64}\.txt", name):
                    raise ValueError("invalid stored attachment reference")
                return (self.attachments_dir / valid_id(ident) / name).read_text(encoding="utf-8")
            return {k: self._unpack(ident, v) for k, v in data.items()}
        return data

    def _store(self, ident, messages, field):
        valid_id(ident)
        if not isinstance(messages, list) or len(messages) > MAX_MESSAGES:
            raise ValueError("messages must be an array of at most 5000 entries")
        with self.lock:
            packed = json_value(self._pack(ident, messages))
            with self._db() as c:
                self._ensure(c, ident)
                current = c.execute("SELECT title FROM conversations WHERE id=?", (ident,)).fetchone()
                title = self._title(messages) if current["title"] == "New chat" else None
                c.execute(f"UPDATE conversations SET {field}=?,updated=?,title=COALESCE(?,title) WHERE id=?",
                          (packed, time.time(), title, ident))

    def store_api(self, ident, messages):
        self._store(ident, messages, "api_messages")

    def store_reply(self, ident, prompt, content, reasoning="", tool_calls=None):
        if not isinstance(prompt, list) or not (content or reasoning or tool_calls):
            return
        reply = {"role": "assistant", "content": content}
        if reasoning:
            reply["reasoning_content"] = reasoning
        if tool_calls:
            reply["tool_calls"] = tool_calls
        self.store_api(ident, [*prompt, reply])

    def store_web(self, ident, messages):
        self._store(ident, messages, "web_messages")

    def get(self, ident):
        valid_id(ident)
        with self.lock, self._db() as c:
            row = c.execute("SELECT * FROM conversations WHERE id=?", (ident,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["api_messages"] = self._unpack(ident, json.loads(d["api_messages"]))
        d["web_messages"] = self._unpack(ident, json.loads(d["web_messages"]))
        d["kv_cached"] = bool(d["snapshot_seq"] and self.snapshot_path(ident).is_file())
        return d

    def list(self):
        with self.lock, self._db() as c:
            rows = c.execute("SELECT id,title,created,updated,snapshot_seq FROM conversations "
                             "ORDER BY updated DESC LIMIT 500").fetchall()
        return [{"id": x["id"], "title": x["title"], "created": x["created"],
                 "updated": x["updated"], "kv_cached": bool(x["snapshot_seq"] and
                 self.snapshot_path(x["id"]).is_file())} for x in rows]

    def delete(self, ident):
        valid_id(ident)
        with self.lock, self._db() as c:
            c.execute("DELETE FROM conversations WHERE id=?", (ident,))
            self.snapshot_path(ident).unlink(missing_ok=True)
            if self.active_id == ident:
                self.active_id = None
                self.active_dirty = False
        # Asset garbage collection is intentionally separate from cache eviction.

    def snapshot_path(self, ident):
        return self.kv_dir / (valid_id(ident) + ".bin")

    def restore_path(self, ident, model):
        with self.lock, self._db() as c:
            row = c.execute("SELECT snapshot_seq,snapshot_model FROM conversations WHERE id=?",
                            (valid_id(ident),)).fetchone()
        if row and row["snapshot_seq"] and row["snapshot_model"] == model:
            path = self.snapshot_path(ident)
            if path.is_file():
                return str(path)
        return None

    def abandon_snapshot(self, ident):
        with self.lock, self._db() as c:
            c.execute("UPDATE conversations SET snapshot_seq=0,snapshot_model='',snapshot_tokens=0 "
                      "WHERE id=?", (valid_id(ident),))
            self.snapshot_path(ident).unlink(missing_ok=True)

    def registered_snapshot(self, ident, model, tokens):
        valid_id(ident)
        with self.lock, self._db() as c:
            self._ensure(c, ident)
            cur = c.execute("SELECT snapshot_seq FROM conversations WHERE id=?", (ident,)).fetchone()
            seq = cur["snapshot_seq"]
            if not seq:
                seq = c.execute("SELECT COALESCE(MAX(snapshot_seq),0)+1 FROM conversations").fetchone()[0]
            c.execute("UPDATE conversations SET snapshot_seq=?,snapshot_model=?,snapshot_tokens=? "
                      "WHERE id=?", (seq, model, int(tokens), ident))
            victims = c.execute("SELECT id FROM conversations WHERE snapshot_seq>0 "
                                "ORDER BY snapshot_seq DESC LIMIT -1 OFFSET ?",
                                (self.max_snapshots,)).fetchall()
            for victim in victims:
                name = victim["id"]
                c.execute("UPDATE conversations SET snapshot_seq=0,snapshot_model='',snapshot_tokens=0 "
                          "WHERE id=?", (name,))
                if name != ident:
                    self.snapshot_path(name).unlink(missing_ok=True)

    def snapshot_count(self):
        with self.lock, self._db() as c:
            return c.execute("SELECT COUNT(*) FROM conversations WHERE snapshot_seq>0").fetchone()[0]
