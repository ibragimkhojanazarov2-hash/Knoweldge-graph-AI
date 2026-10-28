"""Библиотека Marginalia: загрузка книги, список книг, карточка книги.

Только стандартная библиотека: http.server + sqlite3.
Запуск: python3 app/server.py [порт] [--db путь/к/базе.db]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import posixpath
import re
import sqlite3
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from epub import read_epub

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
COVERS = DATA / "covers"
DB = DATA / "library.db"
INDEX = Path(__file__).resolve().parent / "index.html"
MAX_UPLOAD = 100 * 1024 * 1024  # 100 МБ
MAX_NOTE_BYTES = 20000  # тело запроса с заметкой
MAX_NOTE_CHARS = 5000   # длина самой заметки
MAX_IDEA_CHARS = 1000   # длина идеи после правки


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    return db


def init_db() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    COVERS.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS books (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                author TEXT NOT NULL,
                lang TEXT NOT NULL,
                cover TEXT NOT NULL,
                chapter_count INTEGER NOT NULL,
                char_count INTEGER NOT NULL,
                added TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chapters (
                book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                idx INTEGER NOT NULL,
                title TEXT NOT NULL,
                text TEXT NOT NULL,
                PRIMARY KEY (book_id, idx)
            );
            CREATE TABLE IF NOT EXISTS ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                chapter INTEGER NOT NULL,
                idea TEXT NOT NULL,
                quote TEXT NOT NULL,
                why TEXT NOT NULL,
                created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS key_ideas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                rank INTEGER NOT NULL,
                chapter INTEGER NOT NULL,
                idea TEXT NOT NULL,
                quote TEXT NOT NULL,
                why TEXT NOT NULL,
                created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS summary (
                book_id TEXT PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
                about TEXT NOT NULL,
                points TEXT NOT NULL,
                created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_id INTEGER NOT NULL REFERENCES ideas(id) ON DELETE CASCADE,
                to_id INTEGER NOT NULL REFERENCES ideas(id) ON DELETE CASCADE,
                type TEXT NOT NULL,
                explanation TEXT NOT NULL,
                confidence REAL NOT NULL,
                created TEXT NOT NULL,
                UNIQUE(from_id, to_id)
            );
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idea_id INTEGER NOT NULL REFERENCES ideas(id) ON DELETE CASCADE,
                text TEXT NOT NULL,
                created TEXT NOT NULL,
                updated TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS idea_marks (
                idea_id INTEGER PRIMARY KEY REFERENCES ideas(id) ON DELETE CASCADE,
                verdict TEXT NOT NULL,
                updated TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS link_marks (
                from_id INTEGER NOT NULL,
                to_id INTEGER NOT NULL,
                verdict TEXT NOT NULL,
                updated TEXT NOT NULL,
                PRIMARY KEY (from_id, to_id)
            );
            """
        )
        # Короткий заголовок идеи для карты (3–6 слов), его пишет app/titles.py.
        columns = {row["name"] for row in db.execute("PRAGMA table_info(ideas)")}
        if "title" not in columns:
            db.execute("ALTER TABLE ideas ADD COLUMN title TEXT NOT NULL DEFAULT ''")


def read_txt(body: bytes, filename: str) -> dict:
    """TXT: одна глава. UTF-8, если не вышло — Windows-1251."""
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = body.decode("cp1251", "replace")
    lines = [line.rstrip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line.strip())
    if not text.strip():
        raise ValueError("Файл пустой — в нём нет текста.")
    title = posixpath.splitext(posixpath.basename(filename))[0].replace("_", " ").strip()
    return {
        "title": title or "Без названия",
        "author": "",
        "lang": "",
        "cover": None,
        "cover_ext": None,
        "chapters": [{"title": "Текст книги", "text": text}],
    }


def parse_book(filename: str, body: bytes) -> dict:
    ext = posixpath.splitext(filename)[1].lower()
    if ext == ".epub":
        book = read_epub(body)
    elif ext == ".txt":
        book = read_txt(body, filename)
    else:
        raise ValueError("Понимаю только EPUB и TXT.")
    if not book["chapters"]:
        raise ValueError("В книге нет текста.")
    return book


def save_book(book: dict) -> str:
    raw = f"{book['title']}\0{book['author']}\0{len(book['chapters'])}".encode()
    book_id = hashlib.sha1(raw).hexdigest()[:12]

    cover_name = ""
    if book["cover"]:
        cover_name = f"{book_id}{book['cover_ext'] or '.jpg'}"
        (COVERS / cover_name).write_bytes(book["cover"])

    chapters = book["chapters"]
    char_count = sum(len(c["text"]) for c in chapters)
    added = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with connect() as db:
        db.execute("DELETE FROM chapters WHERE book_id = ?", (book_id,))
        db.execute("DELETE FROM books WHERE id = ?", (book_id,))
        db.execute(
            "INSERT INTO books (id, title, author, lang, cover, chapter_count,"
            " char_count, added) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (book_id, book["title"], book["author"], book["lang"], cover_name,
             len(chapters), char_count, added),
        )
        db.executemany(
            "INSERT INTO chapters (book_id, idx, title, text) VALUES (?, ?, ?, ?)",
            [(book_id, i, c["title"], c["text"]) for i, c in enumerate(chapters, 1)],
        )
    return book_id


def list_books() -> list[dict]:
    with connect() as db:
        rows = db.execute(
            "SELECT b.id, b.title, b.author, b.lang, b.cover, b.chapter_count, b.char_count,"
            " (SELECT COUNT(*) FROM links l"
            "  WHERE l.from_id IN (SELECT id FROM ideas WHERE book_id = b.id)"
            "     OR l.to_id IN (SELECT id FROM ideas WHERE book_id = b.id)) AS link_count"
            " FROM books b ORDER BY b.added DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def get_book(book_id: str) -> dict | None:
    with connect() as db:
        book = db.execute(
            "SELECT id, title, author, lang, cover, chapter_count, char_count"
            " FROM books WHERE id = ?", (book_id,)
        ).fetchone()
        if book is None:
            return None
        chapters = db.execute(
            "SELECT idx, title, length(text) AS chars FROM chapters"
            " WHERE book_id = ? ORDER BY idx", (book_id,)
        ).fetchall()
        key_count = db.execute(
            "SELECT COUNT(*) FROM key_ideas WHERE book_id = ?", (book_id,)
        ).fetchone()[0]
        link_count, contradiction_count = db.execute(
            "SELECT COUNT(*), COUNT(CASE WHEN type = 'contradicts' THEN 1 END)"
            " FROM links WHERE from_id IN (SELECT id FROM ideas WHERE book_id = ?)"
            " OR to_id IN (SELECT id FROM ideas WHERE book_id = ?)",
            (book_id, book_id),
        ).fetchone()
        idea_counts = {
            row[0]: row[1] for row in db.execute(
                "SELECT chapter, COUNT(*) FROM ideas WHERE book_id = ? GROUP BY chapter",
                (book_id,),
            )
        }
        link_counts = {
            row[0]: row[1] for row in db.execute(
                "SELECT chapter, COUNT(*) FROM ("
                " SELECT l.id, i.chapter FROM links l"
                " JOIN ideas i ON i.id = l.from_id WHERE i.book_id = ?"
                " UNION"
                " SELECT l.id, i.chapter FROM links l"
                " JOIN ideas i ON i.id = l.to_id WHERE i.book_id = ?"
                ") GROUP BY chapter",
                (book_id, book_id),
            )
        }
    result = dict(book)
    result["key_count"] = key_count
    result["link_count"] = link_count
    result["contradiction_count"] = contradiction_count
    result["chapters"] = [
        {**dict(row), "ideas": idea_counts.get(row["idx"], 0),
         "links": link_counts.get(row["idx"], 0)}
        for row in chapters
    ]
    return result


def get_chapter(book_id: str, idx: int) -> dict | None:
    with connect() as db:
        row = db.execute(
            "SELECT idx, title, text FROM chapters WHERE book_id = ? AND idx = ?",
            (book_id, idx),
        ).fetchone()
    return dict(row) if row else None


def get_ideas(book_id: str, chapter: int) -> list[dict]:
    with connect() as db:
        rows = db.execute(
            "SELECT id, idea, quote, why FROM ideas WHERE book_id = ? AND chapter = ? ORDER BY id",
            (book_id, chapter),
        ).fetchall()
    return [dict(row) for row in rows]


def get_all_ideas(book_id: str) -> list[dict]:
    """Все идеи книги по порядку глав — из них выбираются ключевые."""
    with connect() as db:
        rows = db.execute(
            "SELECT chapter, idea, quote, why FROM ideas"
            " WHERE book_id = ? ORDER BY chapter, id", (book_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def get_key_ideas(book_id: str) -> list[dict]:
    with connect() as db:
        rows = db.execute(
            "SELECT k.rank, k.chapter, c.title AS chapter_title, k.idea, k.quote, k.why"
            " FROM key_ideas k"
            " JOIN chapters c ON c.book_id = k.book_id AND c.idx = k.chapter"
            " WHERE k.book_id = ? ORDER BY k.rank", (book_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def get_idea(idea_id: int) -> dict | None:
    """Идея с её связями с идеями из других книг."""
    with connect() as db:
        row = db.execute(
            "SELECT i.id, i.book_id, i.chapter, i.idea, i.quote, i.why,"
            " b.title AS book_title, c.title AS chapter_title"
            " FROM ideas i"
            " JOIN books b ON b.id = i.book_id"
            " LEFT JOIN chapters c ON c.book_id = i.book_id AND c.idx = i.chapter"
            " WHERE i.id = ?", (idea_id,)
        ).fetchone()
        if row is None:
            return None
        links = db.execute(
            "SELECT l.type, l.explanation, l.confidence,"
            " o.id AS idea_id, o.idea, o.quote, o.why, o.chapter,"
            " b.title AS book_title, c.title AS chapter_title"
            " FROM links l"
            " JOIN ideas o ON o.id = l.to_id"
            " JOIN books b ON b.id = o.book_id"
            " LEFT JOIN chapters c ON c.book_id = o.book_id AND c.idx = o.chapter"
            " WHERE l.from_id = ? ORDER BY l.confidence DESC", (idea_id,)
        ).fetchall()
    result = dict(row)
    result["links"] = [dict(link) for link in links]
    result["notes"] = get_notes(idea_id)
    return result


def idea_exists(idea_id: int) -> bool:
    with connect() as db:
        row = db.execute("SELECT 1 FROM ideas WHERE id = ?", (idea_id,)).fetchone()
    return row is not None


def get_notes(idea_id: int) -> list[dict]:
    """Заметки читателя к идее, новые сверху."""
    with connect() as db:
        rows = db.execute(
            "SELECT id, text, created FROM notes WHERE idea_id = ? ORDER BY id DESC",
            (idea_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def add_note(idea_id: int, text: str) -> dict:
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO notes (idea_id, text, created, updated) VALUES (?, ?, ?, ?)",
            (idea_id, text, created, created),
        )
        note_id = cursor.lastrowid
    return {"id": note_id, "text": text, "created": created}


def delete_note(note_id: int) -> bool:
    with connect() as db:
        cursor = db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    return cursor.rowcount > 0


def get_graph(all_ideas: bool = False) -> dict:
    """Идеи и связи для карты: по умолчанию только идеи, у которых есть связи."""
    linked = "i.id IN (SELECT from_id FROM links UNION SELECT to_id FROM links)"
    where = "" if all_ideas else f" WHERE {linked}"
    with connect() as db:
        books = [dict(row) for row in db.execute(
            "SELECT id, title, author, cover FROM books ORDER BY added"
        ).fetchall()]
        ideas = [dict(row) for row in db.execute(
            "SELECT i.id, i.book_id, b.title AS book_title, i.chapter, i.idea, i.quote, i.why,"
            " i.title, COALESCE(m.verdict, '') AS verdict"
            " FROM ideas i"
            " JOIN books b ON b.id = i.book_id"
            " LEFT JOIN idea_marks m ON m.idea_id = i.id"
            + where + " ORDER BY i.id"
        ).fetchall()]
        links = [dict(row) for row in db.execute(
            "SELECT l.from_id, l.to_id, l.type, l.explanation, l.confidence,"
            " COALESCE(m.verdict, '') AS verdict"
            " FROM links l"
            " LEFT JOIN link_marks m ON m.from_id = l.from_id AND m.to_id = l.to_id"
            " ORDER BY l.id"
        ).fetchall()]
    return {"books": books, "ideas": ideas, "links": links}


def set_verdict(idea_id: int, verdict: str) -> None:
    """Оценка идеи читателем: right, wrong — или пустая строка, чтобы снять."""
    if not verdict:
        with connect() as db:
            db.execute("DELETE FROM idea_marks WHERE idea_id = ?", (idea_id,))
        return
    updated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as db:
        db.execute(
            "INSERT INTO idea_marks (idea_id, verdict, updated) VALUES (?, ?, ?)"
            " ON CONFLICT(idea_id) DO UPDATE SET verdict = excluded.verdict,"
            " updated = excluded.updated",
            (idea_id, verdict, updated),
        )


def set_link_verdict(from_id: int, to_id: int, verdict: str) -> bool:
    """Оценка связи читателем. False — такой связи нет."""
    with connect() as db:
        found = db.execute(
            "SELECT 1 FROM links WHERE from_id = ? AND to_id = ?", (from_id, to_id)
        ).fetchone()
        if found is None:
            return False
        if not verdict:
            db.execute(
                "DELETE FROM link_marks WHERE from_id = ? AND to_id = ?", (from_id, to_id)
            )
            return True
        updated = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute(
            "INSERT INTO link_marks (from_id, to_id, verdict, updated) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(from_id, to_id) DO UPDATE SET verdict = excluded.verdict,"
            " updated = excluded.updated",
            (from_id, to_id, verdict, updated),
        )
    return True


def edit_idea(idea_id: int, text: str) -> None:
    with connect() as db:
        db.execute("UPDATE ideas SET idea = ? WHERE id = ?", (text, idea_id))


def get_summary(book_id: str) -> dict | None:
    """Краткий конспект книги: о чём она и главные мысли по пунктам."""
    with connect() as db:
        row = db.execute(
            "SELECT about, points, created FROM summary WHERE book_id = ?", (book_id,)
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["points"] = json.loads(result["points"])
    return result


def strip_prefix(path: str) -> str:
    """Убирает /proxy/8000 из пути: страница живёт под этим префиксом."""
    parts = path.split("/")
    if len(parts) >= 4 and parts[1] == "proxy" and parts[2].isdigit():
        path = "/" + "/".join(parts[3:])
    return path or "/"


class Handler(BaseHTTPRequestHandler):
    server_version = "Marginalia/0.1"

    def do_GET(self) -> None:
        url = urlparse(self.path)
        path = strip_prefix(unquote(url.path))

        if path in ("/", "/index.html"):
            self.send_bytes(INDEX.read_bytes(), "text/html; charset=utf-8")
            return

        if path == "/api/books":
            self.send_json(list_books())
            return

        if path == "/api/graph":
            all_ideas = parse_qs(url.query).get("all", ["0"])[0] == "1"
            self.send_json(get_graph(all_ideas))
            return

        if path.startswith("/api/ideas/"):
            rest = path[len("/api/ideas/"):].rstrip("/")
            if rest.isdigit():
                idea = get_idea(int(rest))
                if idea is None:
                    self.send_error_text(404, "Идея не найдена.")
                    return
                self.send_json(idea)
                return

        if path.startswith("/api/books/"):
            rest = path[len("/api/books/"):].rstrip("/")
            parts = rest.split("/")
            if len(parts) == 3 and parts[1] == "chapters" and parts[2].isdigit():
                chapter = get_chapter(parts[0], int(parts[2]))
                if chapter is None:
                    self.send_error_text(404, "Глава не найдена.")
                    return
                self.send_json(chapter)
                return
            if len(parts) == 4 and parts[1] == "chapters" and parts[2].isdigit() \
                    and parts[3] == "ideas":
                if get_chapter(parts[0], int(parts[2])) is None:
                    self.send_error_text(404, "Глава не найдена.")
                    return
                self.send_json(get_ideas(parts[0], int(parts[2])))
                return
            if len(parts) == 2 and parts[1] == "summary":
                if get_book(parts[0]) is None:
                    self.send_error_text(404, "Книга не найдена.")
                    return
                self.send_json(get_summary(parts[0]))
                return
            if len(parts) == 2 and parts[1] == "key-ideas":
                if get_book(parts[0]) is None:
                    self.send_error_text(404, "Книга не найдена.")
                    return
                self.send_json(get_key_ideas(parts[0]))
                return
            if len(parts) == 1 and parts[0]:
                book = get_book(parts[0])
                if book is None:
                    self.send_error_text(404, "Книга не найдена.")
                    return
                self.send_json(book)
                return
            self.send_error_text(404, "Нет такой страницы.")
            return

        if path.startswith("/covers/"):
            name = posixpath.basename(path)
            file = COVERS / name
            if not name or not file.is_file():
                self.send_error_text(404, "Обложка не найдена.")
                return
            ctype = mimetypes.guess_type(name)[0] or "image/jpeg"
            self.send_bytes(file.read_bytes(), ctype)
            return

        self.send_error_text(404, "Нет такой страницы.")

    def do_POST(self) -> None:
        path = strip_prefix(unquote(urlparse(self.path).path))

        note_route = re.fullmatch(r"/api/ideas/(\d+)/notes", path)
        if note_route:
            self.save_note(int(note_route.group(1)))
            return

        mark_route = re.fullmatch(r"/api/ideas/(\d+)/mark", path)
        if mark_route:
            self.save_mark(int(mark_route.group(1)))
            return

        edit_route = re.fullmatch(r"/api/ideas/(\d+)/edit", path)
        if edit_route:
            self.save_edit(int(edit_route.group(1)))
            return

        link_mark = re.fullmatch(r"/api/links/(\d+)-(\d+)/mark", path)
        if link_mark:
            self.save_link_mark(int(link_mark.group(1)), int(link_mark.group(2)))
            return

        if path != "/api/upload":
            self.send_error_text(404, "Нет такой страницы.")
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self.send_error_text(400, "Пустой файл.")
            return
        if length > MAX_UPLOAD:
            self.send_error_text(413, "Файл больше 100 МБ.")
            return

        filename = unquote(self.headers.get("X-Filename") or "")
        if not filename:
            self.send_error_text(400, "Не вижу имени файла.")
            return
        body = self.rfile.read(length)
        try:
            book = parse_book(filename, body)
            book_id = save_book(book)
        except (ValueError, KeyError) as exc:
            self.send_error_text(400, str(exc) or "Не смог разобрать книгу.")
            return
        except (zipfile.BadZipFile, ET.ParseError):
            self.send_error_text(400, "Файл повреждён или это не EPUB.")
            return
        self.send_json({"id": book_id})

    def do_DELETE(self) -> None:
        path = strip_prefix(unquote(urlparse(self.path).path))
        match = re.fullmatch(r"/api/notes/(\d+)", path)
        if not match:
            self.send_error_text(404, "Нет такой страницы.")
            return
        if not delete_note(int(match.group(1))):
            self.send_error_text(404, "Заметка не найдена.")
            return
        self.send_json({"ok": True})

    def read_json_body(self, empty_text: str) -> dict | None:
        """Читает JSON-тело запроса; при ошибке отвечает и возвращает None."""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self.send_error_text(400, empty_text)
            return None
        if length > MAX_NOTE_BYTES:
            self.send_error_text(413, "Слишком длинный запрос.")
            return None
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_error_text(400, "Не смог прочитать данные.")
            return None
        return data if isinstance(data, dict) else {}

    def save_mark(self, idea_id: int) -> None:
        """Оценка идеи: {"verdict": "right" | "wrong" | ""}."""
        if not idea_exists(idea_id):
            self.send_error_text(404, "Идея не найдена.")
            return
        data = self.read_json_body("Пустой запрос.")
        if data is None:
            return
        verdict = str(data.get("verdict") or "").strip().lower()
        if verdict not in ("", "right", "wrong"):
            self.send_error_text(400, "Оценка должна быть right, wrong или пустой.")
            return
        set_verdict(idea_id, verdict)
        self.send_json({"ok": True, "verdict": verdict})

    def save_link_mark(self, from_id: int, to_id: int) -> None:
        """Оценка связи: {"verdict": "right" | "wrong" | ""}."""
        data = self.read_json_body("Пустой запрос.")
        if data is None:
            return
        verdict = str(data.get("verdict") or "").strip().lower()
        if verdict not in ("", "right", "wrong"):
            self.send_error_text(400, "Оценка должна быть right, wrong или пустой.")
            return
        if not set_link_verdict(from_id, to_id, verdict):
            self.send_error_text(404, "Связь не найдена.")
            return
        self.send_json({"ok": True, "verdict": verdict})

    def save_edit(self, idea_id: int) -> None:
        """Исправление идеи: {"idea": "новый текст"}."""
        if not idea_exists(idea_id):
            self.send_error_text(404, "Идея не найдена.")
            return
        data = self.read_json_body("Пустой запрос.")
        if data is None:
            return
        text = str(data.get("idea") or "").strip()
        if not text:
            self.send_error_text(400, "Идея не может быть пустой.")
            return
        if len(text) > MAX_IDEA_CHARS:
            self.send_error_text(413, f"Идея длиннее {MAX_IDEA_CHARS} символов.")
            return
        edit_idea(idea_id, text)
        self.send_json({"ok": True, "idea": text})

    def save_note(self, idea_id: int) -> None:
        """Новая заметка к идее: JSON {"text": "..."}."""
        if not idea_exists(idea_id):
            self.send_error_text(404, "Идея не найдена.")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self.send_error_text(400, "Пустая заметка.")
            return
        if length > MAX_NOTE_BYTES:
            self.send_error_text(413, "Заметка слишком длинная.")
            return
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_error_text(400, "Не смог прочитать заметку.")
            return
        text = str((data if isinstance(data, dict) else {}).get("text") or "").strip()
        if not text:
            self.send_error_text(400, "Заметка пустая.")
            return
        if len(text) > MAX_NOTE_CHARS:
            self.send_error_text(413, f"Заметка длиннее {MAX_NOTE_CHARS} символов.")
            return
        self.send_json(add_note(idea_id, text))

    def send_json(self, data) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_bytes(body, "application/json; charset=utf-8")

    def send_bytes(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_text(self, code: int, text: str) -> None:
        self.send_bytes(text.encode(), "text/plain; charset=utf-8", code)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def main() -> None:
    global DB, COVERS
    parser = argparse.ArgumentParser(description="Marginalia: библиотека идей из книг.")
    parser.add_argument("port", nargs="?", type=int, default=8000, help="порт (по умолчанию 8000)")
    parser.add_argument("--db", default=str(DB), help="путь к базе (по умолчанию data/library.db)")
    args = parser.parse_args()

    DB = Path(args.db).resolve()
    COVERS = DB.parent / "covers"  # обложки лежат рядом с базой
    existed = DB.exists()
    init_db()
    if not existed:
        print(f"База не найдена — создана пустая: {DB}", flush=True)

    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"Marginalia: http://127.0.0.1:{args.port}/", flush=True)
    print(f"База: {DB}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
