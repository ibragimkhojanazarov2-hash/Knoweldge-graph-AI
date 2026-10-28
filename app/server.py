"""Библиотека Marginalia: загрузка книги, список книг, карточка книги.

Только стандартная библиотека: http.server + sqlite3.
Запуск: python3 app/server.py [порт]
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import posixpath
import sqlite3
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from epub import read_epub

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
COVERS = DATA / "covers"
DB = DATA / "library.db"
INDEX = Path(__file__).resolve().parent / "index.html"
MAX_UPLOAD = 100 * 1024 * 1024  # 100 МБ


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    return db


def init_db() -> None:
    DATA.mkdir(exist_ok=True)
    COVERS.mkdir(exist_ok=True)
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
            """
        )


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
            "SELECT id, title, author, lang, cover, chapter_count, char_count"
            " FROM books ORDER BY added DESC"
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
    result = dict(book)
    result["chapters"] = [dict(row) for row in chapters]
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
            "SELECT idea, quote, why FROM ideas WHERE book_id = ? AND chapter = ? ORDER BY id",
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


def strip_prefix(path: str) -> str:
    """Убирает /proxy/8000 из пути: страница живёт под этим префиксом."""
    parts = path.split("/")
    if len(parts) >= 4 and parts[1] == "proxy" and parts[2].isdigit():
        path = "/" + "/".join(parts[3:])
    return path or "/"


class Handler(BaseHTTPRequestHandler):
    server_version = "Marginalia/0.1"

    def do_GET(self) -> None:
        path = strip_prefix(unquote(urlparse(self.path).path))

        if path in ("/", "/index.html"):
            self.send_bytes(INDEX.read_bytes(), "text/html; charset=utf-8")
            return

        if path == "/api/books":
            self.send_json(list_books())
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
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Marginalia: http://127.0.0.1:{port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
