"""On-demand Markdown/Markdown-directory retrieval for character worldbooks.

A bounded CPU retrieval worker acts as a specialist *search task*, not as a second
generative model. Sources stay in their original OneDrive/local locations.
Only explicitly configured paths are scanned; no automatic filesystem discovery.
"""
from __future__ import annotations

import functools
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path

EXTENSIONS = {".md", ".markdown"}
MAX_FILES = 200
MAX_BYTES_PER_FILE = 256 * 1024
MAX_CHUNKS_PER_FILE = 240
MAX_CHUNK_CHARS = 900
MAX_SNIPPET_CHARS = 2300
MAX_RESULTS = 4
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="polystrata-lore-search")
_search_slots = threading.BoundedSemaphore(2)

_zh = re.compile(r"[\u4e00-\u9fff]{2,}")
_latin = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{1,}")
_stop = {"请问", "一下", "可以", "什么", "如何", "怎么", "知道", "告诉", "我们", "这个", "那个",
         "现在", "关于", "里面", "一个", "一下", "是否", "哪些", "帮我", "看看", "好的", "谢谢"}
_heading = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)


def _terms(text: str) -> set[str]:
    terms = {m.group(0).casefold() for m in _latin.finditer(text)}
    for m in _zh.finditer(text):
        s = m.group(0)
        if len(s) <= 7 and s not in _stop:
            terms.add(s)
        for i in range(len(s) - 1):
            if s[i:i+2] not in _stop:
                terms.add(s[i:i+2])
    return terms


def _candidate_files(source: dict, errors: list[str]):
    root = Path(source["path"])
    if not root.is_absolute():
        errors.append("请使用服务器本机的绝对路径：" + source["path"][:100])
        return []
    if source["kind"] == "file":
        if (root.is_symlink() or getattr(root, "is_junction", lambda: False)()
                or not root.is_file() or root.suffix.lower() not in EXTENSIONS):
            errors.append("Markdown 文件不存在：" + source["path"][:100])
            return []
        return [root]
    if root.is_symlink() or getattr(root, "is_junction", lambda: False)() or not root.is_dir():
        errors.append("Markdown 目录不存在：" + source["path"][:100])
        return []
    # No symlink/junction traversal outside the selected directory.
    try:
        folder = root.resolve(strict=True)
        found = []
        stack = [folder]
        while stack and len(found) < MAX_FILES:
            parent = stack.pop()
            try:
                items = sorted(parent.iterdir(), key=lambda x: x.name.lower())
            except OSError:
                continue
            for child in items:
                if child.name.startswith(".") or child.is_symlink() or getattr(child, "is_junction", lambda: False)():
                    continue
                try:
                    if child.is_dir() and source.get("recursive", True):
                        if child.resolve().is_relative_to(folder):
                            stack.append(child)
                    elif child.is_file() and child.suffix.lower() in EXTENSIONS:
                        if child.resolve().is_relative_to(folder):
                            found.append(child)
                            if len(found) >= MAX_FILES:
                                break
                except OSError:
                    continue
        return found
    except OSError:
        errors.append("无法读取目录：" + source["path"][:100])
        return []


def _chunks(text: str):
    # Headings are retained as search signals; cap chunk lengths and split long sections.
    title = ""
    body = []
    for line in text.splitlines():
        matched = _heading.match(line)
        if matched and body:
            section = "\n".join(body).strip()
            for pos in range(0, len(section), MAX_CHUNK_CHARS):
                chunk = section[pos:pos + MAX_CHUNK_CHARS].strip()
                if chunk:
                    yield title, chunk
            body = []
        if matched:
            title = matched.group(1).strip()[:150]
        else:
            body.append(line)
    if body:
        section = "\n".join(body).strip()
        for pos in range(0, len(section), MAX_CHUNK_CHARS):
            chunk = section[pos:pos + MAX_CHUNK_CHARS].strip()
            if chunk:
                yield title, chunk


@functools.lru_cache(maxsize=400)
def _read_chunks(path: str, mtime_ns: int, size: int):
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        text = fh.read(MAX_BYTES_PER_FILE + 1)
    if "\x00" in text:
        return ()
    return tuple(list(_chunks(text))[:MAX_CHUNKS_PER_FILE])


def _retrieve(sources: list[dict], question: str, *, budget: int):
    start = time.monotonic()
    errors = []
    terms = _terms(question[:1000])
    if not terms:
        return {"snippets": [], "scanned_files": 0, "errors": [], "elapsed_ms": 0}
    candidates = []
    seen = set()
    for src in sources[:12]:
        for file in _candidate_files(src, errors):
            identifier = str(file.resolve()).casefold()
            if identifier not in seen and len(candidates) < MAX_FILES:
                candidates.append(file)
                seen.add(identifier)
    hits = []
    scanned = 0
    for path in candidates:
        try:
            stat = path.stat()
            if stat.st_size > MAX_BYTES_PER_FILE or stat.st_size == 0:
                continue
            chunks = _read_chunks(str(path), stat.st_mtime_ns, stat.st_size)
            scanned += 1
            file_tokens = _terms(path.stem)
            for title, content in chunks:
                lowered = content.casefold()
                title_tokens = _terms(title)
                matches = {t for t in terms if t in lowered}
                meta = terms.intersection(file_tokens | title_tokens)
                if not matches and not meta:
                    continue
                # Prefer substantial content overlap, then heading and filename.
                score = sum(3 if len(t) >= 4 else 1 for t in matches) + len(meta) * 3
                if not matches: score *= 0.6
                hits.append((score, str(path), title, content))
        except (OSError, UnicodeError):
            errors.append("无法读取：" + str(path)[:100])
    hits.sort(key=lambda x: (-x[0], x[1], x[2]))
    selected = []
    remaining = max(0, min(int(budget), MAX_SNIPPET_CHARS))
    for score, path, title, content in hits:
        if len(selected) >= MAX_RESULTS or remaining < 80:
            break
        chunk = content[:min(remaining, MAX_CHUNK_CHARS)]
        selected.append({"file": path, "heading": title, "content": chunk, "score": round(score, 2)})
        remaining -= len(chunk)
    return {"snippets": selected, "scanned_files": scanned,
            "candidate_files": len(candidates), "errors": errors[:8],
            "elapsed_ms": round((time.monotonic() - start) * 1000)}


def search_sources(sources: list[dict], question: str, *, budget=2300, timeout=3.0):
    """Isolated retrieval task with concurrency and time budgets.

    The worker only reads Markdown and returns grounded excerpts. It NEVER
    invokes the currently loaded LLM or alters its KV cache.
    """
    if not sources:
        return {"snippets": [], "scanned_files": 0, "errors": [], "elapsed_ms": 0}
    if not _search_slots.acquire(blocking=False):
        return {"snippets": [], "scanned_files": 0, "errors": ["检索忙，请稍后重试"], "elapsed_ms": 0}
    try:
        future = _executor.submit(_retrieve, sources, question, budget=budget)
    except Exception:
        _search_slots.release()
        raise
    # Keep this slot occupied until the worker actually exits. A timeout of
    # future.result() cannot stop a thread doing slow OneDrive I/O.
    future.add_done_callback(lambda _done: _search_slots.release())
    try:
        try:
            return future.result(timeout=timeout)
        except FutureTimeout:
            future.cancel()
            return {"snippets": [], "scanned_files": 0, "errors": ["检索超时，已跳过本次 Markdown 引用"], "elapsed_ms": int(timeout*1000)}
    except Exception:
        future.cancel()
        raise
