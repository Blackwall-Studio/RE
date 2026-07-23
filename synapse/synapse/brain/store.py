"""The second brain: a persistent, searchable knowledge base.

SQLite + FTS5 full-text search. Zero external services, lives in one file.
Every chat session, RE analysis, and manual note feeds it; it dedupes and
merges so it grows instead of bloating.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'fact',
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.7,
    dedupe TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    title, content, tags, content='knowledge', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge BEGIN
    INSERT INTO knowledge_fts(rowid, title, content, tags)
    VALUES (new.id, new.title, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content, tags)
    VALUES ('delete', old.id, old.title, old.content, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_au AFTER UPDATE ON knowledge BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content, tags)
    VALUES ('delete', old.id, old.title, old.content, old.tags);
    INSERT INTO knowledge_fts(rowid, title, content, tags)
    VALUES (new.id, new.title, new.content, new.tags);
END;
"""


def _key(kind: str, title: str) -> str:
    return hashlib.sha1(f"{kind}|{title.strip().lower()}".encode()).hexdigest()


class Brain:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.executescript(SCHEMA)

    # ------------------------------------------------------------------ write
    def add(
        self,
        title: str,
        content: str,
        kind: str = "fact",
        tags: str = "",
        source: str = "",
        confidence: float = 0.7,
    ) -> dict:
        """Insert a new entry, or merge into an existing one with the same
        (kind, title). Returns {"id", "merged": bool, "title": str}."""
        title = (title or "").strip()[:300]
        content = (content or "").strip()
        if not title or not content:
            raise ValueError("title and content are required")
        dk = _key(kind, title)
        now = time.time()
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT id, content, confidence FROM knowledge WHERE dedupe=?", (dk,)
            ).fetchone()
            if row:
                # grow the existing entry instead of duplicating it
                if content not in row["content"]:
                    new_content = row["content"] + "\n\n---\n\n" + content
                else:
                    new_content = row["content"]
                new_conf = min(1.0, row["confidence"] + 0.05)
                self._db.execute(
                    "UPDATE knowledge SET content=?, confidence=?, updated_at=? WHERE id=?",
                    (new_content, new_conf, now, row["id"]),
                )
                return {"id": row["id"], "merged": True, "title": title}
            cur = self._db.execute(
                "INSERT INTO knowledge (kind,title,content,tags,source,confidence,dedupe,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (kind, title, content, tags, source, confidence, dk, now, now),
            )
            return {"id": cur.lastrowid, "merged": False, "title": title}

    def delete(self, entry_id: int) -> bool:
        with self._lock, self._db:
            cur = self._db.execute("DELETE FROM knowledge WHERE id=?", (entry_id,))
            return cur.rowcount > 0

    # ------------------------------------------------------------------- read
    def search(self, query: str, limit: int = 20) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return self.list(limit=limit)
        with self._lock:
            try:
                # FTS5 match; quote each token to survive special chars
                match = " OR ".join(f'"{t}"' for t in query.split() if t)
                rows = self._db.execute(
                    "SELECT k.* FROM knowledge_fts f JOIN knowledge k ON k.id = f.rowid"
                    " WHERE knowledge_fts MATCH ? ORDER BY rank LIMIT ?",
                    (match, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                like = f"%{query}%"
                rows = self._db.execute(
                    "SELECT * FROM knowledge WHERE title LIKE ? OR content LIKE ? LIMIT ?",
                    (like, like, limit),
                ).fetchall()
        return [dict(r) for r in rows]

    def list(self, kind: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
        with self._lock:
            if kind:
                rows = self._db.execute(
                    "SELECT * FROM knowledge WHERE kind=? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (kind, limit, offset),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM knowledge ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
        return [dict(r) for r in rows]

    def get(self, entry_id: int) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM knowledge WHERE id=?", (entry_id,)).fetchone()
        return dict(row) if row else None

    def stats(self) -> dict:
        with self._lock:
            total = self._db.execute("SELECT COUNT(*) c FROM knowledge").fetchone()["c"]
            kinds = {
                r["kind"]: r["c"]
                for r in self._db.execute(
                    "SELECT kind, COUNT(*) c FROM knowledge GROUP BY kind"
                ).fetchall()
            }
            last = self._db.execute("SELECT MAX(updated_at) m FROM knowledge").fetchone()["m"]
        return {"total": total, "by_kind": kinds, "last_updated": last or 0}

    def context_snippets(self, query: str, limit: int = 5, max_chars: int = 2500) -> str:
        """Top-k relevant knowledge, formatted for injection into a prompt."""
        hits = self.search(query, limit=limit)
        out, used = [], 0
        for h in hits:
            snippet = f"[{h['kind']}] {h['title']}\n{h['content'][:600]}"
            if used + len(snippet) > max_chars:
                break
            out.append(snippet)
            used += len(snippet)
        return "\n\n".join(out)

    def close(self):
        with self._lock:
            self._db.close()
