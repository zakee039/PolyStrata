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
import shutil
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
            columns = {x["name"] for x in c.execute("PRAGMA table_info(conversations)")}
            if "pinned" not in columns:
                c.execute("ALTER TABLE conversations ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
            if "content_rev" not in columns:
                c.execute("ALTER TABLE conversations ADD COLUMN content_rev INTEGER NOT NULL DEFAULT 0")
            c.execute("""CREATE TABLE IF NOT EXISTS kv_snapshots (
                session_id TEXT NOT NULL,
                model_key TEXT NOT NULL,
                seq INTEGER NOT NULL DEFAULT 0,
                tokens INTEGER NOT NULL DEFAULT 0,
                locked INTEGER NOT NULL DEFAULT 0,
                content_rev INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(session_id, model_key)
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS ix_kv_model_seq ON kv_snapshots(model_key,seq)")
            # Import old global 5-slot metadata. Move only known snapshot files;
            # never discard unrelated binaries during a schema upgrade.
            legacy = c.execute("SELECT id,snapshot_model,snapshot_seq,snapshot_tokens,content_rev "
                               "FROM conversations WHERE snapshot_seq>0 AND snapshot_model!=''").fetchall()
            for row in legacy:
                if c.execute("SELECT 1 FROM kv_snapshots WHERE session_id=? AND model_key=?",
                             (row["id"], row["snapshot_model"])).fetchone():
                    continue
                old = self.kv_dir / (row["id"] + ".bin")
                if not old.is_file():
                    continue
                target = self._model_path(row["id"], row["snapshot_model"])
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    os.replace(old, target)
                c.execute("INSERT OR IGNORE INTO kv_snapshots VALUES(?,?,?,?,?,?)",
                          (row["id"],row["snapshot_model"],row["snapshot_seq"],
                           row["snapshot_tokens"],0,row["content_rev"]))
            # Old metadata no longer owns the KV file.
            c.execute("UPDATE conversations SET snapshot_seq=0,snapshot_model='',snapshot_tokens=0 "
                      "WHERE snapshot_seq>0")

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
                previous = c.execute(f"SELECT {field} FROM conversations WHERE id=?", (ident,)).fetchone()
                prior_turns = json.loads(previous[0]) if previous else []
                next_turns = json.loads(packed)
                # A later turn can reuse the saved KV prefix; edits to earlier
                # messages must invalidate the saved state and re-Prefill.
                changed = (len(next_turns) < len(prior_turns) or
                           next_turns[:len(prior_turns)] != prior_turns)
                c.execute(f"UPDATE conversations SET {field}=?,updated=?,title=COALESCE(?,title),"
                          "content_rev=content_rev+? WHERE id=?",
                          (packed, time.time(), title, int(changed), ident))

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

    def _model_path(self, ident, model):
        name = hashlib.sha256(str(model).encode("utf-8")).hexdigest()[:24]
        return self.kv_dir / name / (valid_id(ident) + ".bin")

    def snapshot_path(self, ident, model=None):
        valid_id(ident)
        if model is not None:
            return self._model_path(ident, model)
        # Compatibility for callers of the original single-model API.
        with self.lock, self._db() as c:
            row = c.execute("SELECT model_key FROM kv_snapshots WHERE session_id=? "
                            "ORDER BY locked DESC,seq DESC LIMIT 1", (ident,)).fetchone()
        return self._model_path(ident, row[0]) if row else self.kv_dir / (ident + ".bin")

    def _kv_rows(self, c, ident, model=None):
        if model is None:
            return c.execute("SELECT * FROM kv_snapshots WHERE session_id=?", (ident,)).fetchall()
        return c.execute("SELECT * FROM kv_snapshots WHERE session_id=? AND model_key=?",
                         (ident, model)).fetchall()

    def _summary(self, c, row, model=None):
        snapshots = self._kv_rows(c, row["id"])
        available = [x for x in snapshots if x["seq"] and
                     x["content_rev"] == row["content_rev"] and
                     self._model_path(row["id"], x["model_key"]).is_file()]
        current = next((x for x in available if x["model_key"] == model), None)
        pending = next((x for x in snapshots if x["locked"] and not x["seq"]), None)
        return {"id": row["id"], "title": row["title"], "created": row["created"],
                "updated": row["updated"], "pinned": bool(row["pinned"]),
                "kv_cached": bool(available), "kv_current": bool(current),
                "kv_locked": any(x["locked"] for x in snapshots),
                "kv_locked_current": any(x["locked"] and x["model_key"] == model for x in snapshots),
                "kv_pending": bool(pending),
                "kv_bytes": sum(self._model_path(row["id"], x["model_key"]).stat().st_size
                                for x in available if x["locked"])}

    def get(self, ident, model=None):
        valid_id(ident)
        with self.lock, self._db() as c:
            row = c.execute("SELECT * FROM conversations WHERE id=?", (ident,)).fetchone()
            if row is None:
                return None
            metadata = self._summary(c, row, model)
        record = dict(row)
        record.update(metadata)
        record["api_messages"] = self._unpack(ident, json.loads(record["api_messages"]))
        record["web_messages"] = self._unpack(ident, json.loads(record["web_messages"]))
        return record

    def list(self, model=None):
        with self.lock, self._db() as c:
            rows = c.execute("SELECT * FROM conversations ORDER BY pinned DESC,updated DESC LIMIT 500").fetchall()
            return [self._summary(c, row, model) for row in rows]

    def rename(self, ident, title):
        valid_id(ident)
        if not isinstance(title, str) or not title.strip() or len(title) > 120:
            raise ValueError("title must contain 1-120 characters")
        with self.lock, self._db() as c:
            if not c.execute("SELECT 1 FROM conversations WHERE id=?", (ident,)).fetchone():
                raise KeyError("session not found")
            c.execute("UPDATE conversations SET title=? WHERE id=?", (title.strip(), ident))

    def pin(self, ident, pinned):
        valid_id(ident)
        if not isinstance(pinned, bool):
            raise ValueError("pinned must be boolean")
        with self.lock, self._db() as c:
            if not c.execute("SELECT 1 FROM conversations WHERE id=?", (ident,)).fetchone():
                raise KeyError("session not found")
            c.execute("UPDATE conversations SET pinned=? WHERE id=?", (int(pinned), ident))

    def delete(self, ident):
        valid_id(ident)
        with self.lock, self._db() as c:
            snapshots = self._kv_rows(c, ident)
            c.execute("DELETE FROM kv_snapshots WHERE session_id=?", (ident,))
            c.execute("DELETE FROM conversations WHERE id=?", (ident,))
            if self.active_id == ident:
                self.active_id = None
                self.active_dirty = False
            for row in snapshots:
                self._model_path(ident, row["model_key"]).unlink(missing_ok=True)
            (self.kv_dir / (ident + ".bin")).unlink(missing_ok=True)
        # Only files belonging to this deleted session are removed.
        for folder in (self.images_dir, self.attachments_dir):
            shutil.rmtree(folder / ident, ignore_errors=True)

    def lock_snapshot(self, ident, model, locked=True):
        valid_id(ident)
        if not isinstance(locked, bool):
            raise ValueError("locked must be boolean")
        if not isinstance(model, str) or not model:
            raise ValueError("model fingerprint required")
        with self.lock, self._db() as c:
            row = c.execute("SELECT content_rev FROM conversations WHERE id=?", (ident,)).fetchone()
            if row is None:
                raise KeyError("session not found")
            if locked:
                # Allow a pending lock: the next generated/saved KV for this
                # model will be permanent, without consuming a dynamic slot.
                c.execute("INSERT INTO kv_snapshots(session_id,model_key,locked,content_rev) "
                          "VALUES(?,?,1,?) ON CONFLICT(session_id,model_key) DO UPDATE "
                          "SET locked=1", (ident, model, row["content_rev"]))
            else:
                c.execute("UPDATE kv_snapshots SET locked=0 WHERE session_id=? AND model_key=?",
                          (ident, model))
                self._evict_dynamic(c, model)
        return self.get(ident, model)

    def _evict_dynamic(self, c, model):
        rows = c.execute("SELECT session_id FROM kv_snapshots WHERE model_key=? AND seq>0 "
                         "AND locked=0 ORDER BY seq DESC LIMIT -1 OFFSET ?",
                         (model, self.max_snapshots)).fetchall()
        for victim in rows:
            ident = victim["session_id"]
            c.execute("UPDATE kv_snapshots SET seq=0,tokens=0 WHERE session_id=? AND model_key=?",
                      (ident, model))
            self._model_path(ident, model).unlink(missing_ok=True)

    def restore_path(self, ident, model):
        valid_id(ident)
        with self.lock, self._db() as c:
            row = c.execute("SELECT content_rev FROM conversations WHERE id=?", (ident,)).fetchone()
            snap = c.execute("SELECT seq,content_rev FROM kv_snapshots WHERE session_id=? "
                             "AND model_key=?", (ident,model)).fetchone()
        if row and snap and snap["seq"] and row["content_rev"] == snap["content_rev"]:
            path = self._model_path(ident, model)
            return str(path) if path.is_file() else None
        return None

    def abandon_snapshot(self, ident, model=None):
        valid_id(ident)
        with self.lock, self._db() as c:
            snapshots = self._kv_rows(c, ident, model)
            for row in snapshots:
                c.execute("UPDATE kv_snapshots SET seq=0,tokens=0 WHERE session_id=? AND model_key=?",
                          (ident, row["model_key"]))
                self._model_path(ident, row["model_key"]).unlink(missing_ok=True)

    def registered_snapshot(self, ident, model, tokens):
        valid_id(ident)
        with self.lock, self._db() as c:
            self._ensure(c, ident)
            rev = c.execute("SELECT content_rev FROM conversations WHERE id=?", (ident,)).fetchone()[0]
            path = self._model_path(ident, model)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Preserve older session_file callers that saved to the unscoped path.
            legacy = self.kv_dir / (ident + ".bin")
            if legacy.is_file() and not path.is_file():
                os.replace(legacy, path)
            if not path.is_file():
                raise FileNotFoundError("KV snapshot not written by engine")
            row = c.execute("SELECT seq FROM kv_snapshots WHERE session_id=? AND model_key=?",
                            (ident, model)).fetchone()
            seq = row["seq"] if row else 0
            if not seq:
                seq = c.execute("SELECT COALESCE(MAX(seq),0)+1 FROM kv_snapshots WHERE model_key=?",
                                (model,)).fetchone()[0]
            c.execute("INSERT INTO kv_snapshots(session_id,model_key,seq,tokens,content_rev) "
                      "VALUES(?,?,?,?,?) ON CONFLICT(session_id,model_key) DO UPDATE "
                      "SET seq=excluded.seq,tokens=excluded.tokens,content_rev=excluded.content_rev",
                      (ident,model,seq,int(tokens),rev))
            self._evict_dynamic(c, model)

    def snapshot_count(self, model=None):
        with self.lock, self._db() as c:
            if model is None:
                return c.execute("SELECT COUNT(*) FROM kv_snapshots WHERE seq>0 AND locked=0").fetchone()[0]
            return c.execute("SELECT COUNT(*) FROM kv_snapshots WHERE seq>0 AND locked=0 AND model_key=?",
                             (model,)).fetchone()[0]
