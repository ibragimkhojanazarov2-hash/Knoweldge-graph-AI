"""Демо-база: все идеи, связи и обложки, но главы — только первые абзацы.

Запуск: python3 app/export_demo.py
Результат: demo/library-demo.db и demo/covers/
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SOURCE = BASE / "data" / "library.db"
DEMO = BASE / "demo"
TARGET = DEMO / "library-demo.db"
SOURCE_COVERS = BASE / "data" / "covers"
TARGET_COVERS = DEMO / "covers"
PARAGRAPHS = 3  # столько первых абзацев оставляем в каждой главе


def shorten(text: str) -> str:
    """Первые 2–3 абзаца; в базе абзацы разделены переводами строк."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) <= PARAGRAPHS:
        return "\n".join(lines)
    return "\n".join(lines[:PARAGRAPHS]) + "\n…"


def main() -> None:
    if not SOURCE.is_file():
        raise SystemExit(f"Нет базы: {SOURCE}")

    DEMO.mkdir(exist_ok=True)
    TARGET_COVERS.mkdir(exist_ok=True)
    if TARGET.exists():
        TARGET.unlink()

    with sqlite3.connect(SOURCE) as source, sqlite3.connect(TARGET) as db:
        source.backup(db)
        chapters = db.execute("SELECT rowid, text FROM chapters").fetchall()
        before = sum(len(text) for _, text in chapters)
        for rowid, text in chapters:
            db.execute("UPDATE chapters SET text = ? WHERE rowid = ?", (shorten(text), rowid))

        for book_id, in db.execute("SELECT id FROM books").fetchall():
            chars = db.execute(
                "SELECT COALESCE(SUM(LENGTH(text)), 0) FROM chapters WHERE book_id = ?",
                (book_id,),
            ).fetchone()[0]
            db.execute("UPDATE books SET char_count = ? WHERE id = ?", (chars, book_id))

        after = db.execute("SELECT COALESCE(SUM(LENGTH(text)), 0) FROM chapters").fetchone()[0]
        db.commit()
        counts = {
            "books": db.execute("SELECT COUNT(*) FROM books").fetchone()[0],
            "chapters": len(chapters),
            "ideas": db.execute("SELECT COUNT(*) FROM ideas").fetchone()[0],
            "key_ideas": db.execute("SELECT COUNT(*) FROM key_ideas").fetchone()[0],
            "summaries": db.execute("SELECT COUNT(*) FROM summary").fetchone()[0],
            "links": db.execute("SELECT COUNT(*) FROM links").fetchone()[0],
        }
        covers = [row[0] for row in db.execute("SELECT cover FROM books WHERE cover != ''")]

    for name in covers:
        shutil.copy2(SOURCE_COVERS / name, TARGET_COVERS / name)
    for stale in TARGET_COVERS.glob("*.jpg"):
        if stale.name not in covers:
            stale.unlink()

    print(f"Готово: {TARGET.relative_to(BASE)}")
    print(f"  книг: {counts['books']}, глав: {counts['chapters']}, идей: {counts['ideas']},"
          f" ключевых идей: {counts['key_ideas']}, конспектов: {counts['summaries']},"
          f" связей: {counts['links']}")
    print(f"  текст глав: {before} → {after} символов (первые {PARAGRAPHS} абзаца)")
    print(f"  обложек: {len(covers)} в {TARGET_COVERS.relative_to(BASE)}")


if __name__ == "__main__":
    main()
