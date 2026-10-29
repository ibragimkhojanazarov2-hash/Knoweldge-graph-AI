"""Экспорт библиотеки в архив Markdown для Obsidian.

В архиве:
- README.md — как открыть папку в Obsidian;
- Книги/<название>.md — конспект и ключевые идеи книги;
- Идеи/<мысль> (<id>).md — цитата, мысль, почему, заметки и связи.

Связи записываются как [[Идеи/файл|подпись]] с типом связи. Идеи, которые
читатель отметил как «неверная связь», в экспорт не попадают: их нет и на
карте после такой отметки.
"""
from __future__ import annotations

import io
import json
import re
import zipfile

LINK_LABELS = {
    "supports": "поддерживает",
    "contradicts": "противоречит",
    "extends": "развивает",
    "example": "пример к",
    "same": "то же самое",
}
BAD_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]')


def _db():
    from server import connect

    return connect()


def _safe(name: str, fallback: str = "Без названия", limit: int = 70) -> str:
    """Имя файла Obsidian: без служебных символов, не длиннее limit."""
    text = BAD_CHARS.sub(" ", name or "").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip(" .")
    if len(text) > limit:
        text = text[:limit].rstrip(" .,;:—-")
    return text or fallback


def _short(idea: str, limit: int = 60) -> str:
    """Первая фраза мысли — для заголовка и имени файла."""
    text = re.sub(r"\s+", " ", idea or "").strip()
    cut = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    if len(cut) > limit:
        cut = cut[:limit].rstrip(" .,;:—-")
    return cut or "Идея"


def _quote(text: str) -> str:
    lines = (text or "").strip().splitlines() or [""]
    return "\n".join("> " + line.strip() for line in lines)


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _idea_filename(idea: str, idea_id: int) -> str:
    return f"{_safe(_short(idea))} ({idea_id})"


def _book_filename(title: str, book_id: str, used: set[str]) -> str:
    base = _safe(title)
    name = base
    tail = book_id[:4]
    n = 2
    while name.casefold() in used:
        name = f"{base} ({tail})" if n == 2 else f"{base} ({tail} {n})"
        n += 1
    used.add(name.casefold())
    return name


def build_zip() -> tuple[bytes, dict]:
    """Собирает архив в памяти. Возвращает (байты, счётчики)."""
    with _db() as db:
        books = [dict(row) for row in db.execute(
            "SELECT id, title, author, chapter_count FROM books ORDER BY title"
        ).fetchall()]
        summaries = {row["book_id"]: dict(row) for row in db.execute(
            "SELECT book_id, about, points FROM summary"
        ).fetchall()}
        key_rows = [dict(row) for row in db.execute(
            "SELECT id, book_id, rank, chapter, idea, quote FROM key_ideas"
            " ORDER BY book_id, rank"
        ).fetchall()]
        idea_rows = [dict(row) for row in db.execute(
            "SELECT i.id, i.book_id, i.chapter, i.idea, i.quote, i.why,"
            "       c.title AS chapter_title"
            "  FROM ideas i LEFT JOIN chapters c"
            "    ON c.book_id = i.book_id AND c.idx = i.chapter"
            " ORDER BY i.book_id, i.chapter, i.id"
        ).fetchall()]
        note_rows = [dict(row) for row in db.execute(
            "SELECT id, idea_id, text, created FROM notes ORDER BY idea_id, created, id"
        ).fetchall()]
        link_rows = [dict(row) for row in db.execute(
            "SELECT l.from_id, l.to_id, l.explanation, l.confidence,"
            "       COALESCE(NULLIF(lm.type, ''), l.type) AS link_type"
            "  FROM links l"
            "  LEFT JOIN link_marks lm"
            "    ON lm.from_id = l.from_id AND lm.to_id = l.to_id"
            " WHERE COALESCE(lm.verdict, '') <> 'wrong'"
            " ORDER BY l.from_id, l.id"
        ).fetchall()]

    by_id = {row["id"]: row for row in idea_rows}
    files: dict[str, str] = {}

    filenames = {row["id"]: _idea_filename(row["idea"], row["id"]) for row in idea_rows}
    notes: dict[int, list[dict]] = {}
    for row in note_rows:
        notes.setdefault(row["idea_id"], []).append(row)
    outgoing: dict[int, list[dict]] = {}
    for row in link_rows:
        if row["from_id"] in by_id and row["to_id"] in by_id:
            outgoing.setdefault(row["from_id"], []).append(row)

    # Книги: конспект и ключевые идеи.
    used_books: set[str] = set()
    book_files: dict[str, str] = {}
    for book in books:
        name = _book_filename(book["title"], book["id"], used_books)
        book_files[book["id"]] = name
        parts = [f"# {book['title']}", ""]
        meta = []
        if book["author"]:
            meta.append(book["author"])
        meta.append(f"{book['chapter_count']} глав")
        parts.append("*" + " · ".join(meta) + "*")
        parts.append("")
        summary = summaries.get(book["id"])
        if summary and (summary["about"] or summary["points"]):
            parts += ["## Конспект", ""]
            if summary["about"]:
                parts += [_one_line(summary["about"]), ""]
            try:
                points = json.loads(summary["points"] or "[]")
            except (json.JSONDecodeError, TypeError):
                points = []
            if isinstance(points, list):
                for point in points:
                    text = _one_line(str(point))
                    if text:
                        parts.append(f"- {text}")
            parts.append("")
        keys = [row for row in key_rows if row["book_id"] == book["id"]]
        parts += ["## Ключевые идеи", ""]
        if not keys:
            parts.append("_Ключевых идей пока нет._")
        for row in keys:
            parts.append(f"{row['rank']}. **{_one_line(row['idea'])}**")
            if row["quote"]:
                parts.append("")
                parts.append(_quote(row["quote"]))
            parts.append("")
        parts.append("## Все идеи")
        parts.append("")
        for row in idea_rows:
            if row["book_id"] == book["id"]:
                parts.append(f"- [[Идеи/{filenames[row['id']]}|{_short(row['idea'])}]]")
        parts.append("")
        files[f"Книги/{name}.md"] = "\n".join(parts)

    # Идеи: цитата, мысль, заметки и связи.
    for row in idea_rows:
        parts = [f"# {_short(row['idea'], 90)}", ""]
        book = next((b for b in books if b["id"] == row["book_id"]), None)
        where = []
        if book:
            where.append(f"[[Книги/{book_files[book['id']]}|{book['title']}]]")
        chapter = row["chapter_title"] or f"глава {row['chapter']}"
        where.append(chapter)
        parts += ["*" + " · ".join(where) + "*", ""]
        if row["quote"]:
            parts += ["## Цитата", "", _quote(row["quote"]), ""]
        parts += ["## Мысль", "", _one_line(row["idea"]), ""]
        if row["why"]:
            parts += ["**Почему это важно.** " + _one_line(row["why"]), ""]
        parts += ["## Связи", ""]
        links = outgoing.get(row["id"], [])
        if not links:
            parts.append("_Связей пока нет._")
        for link in links:
            other = by_id[link["to_id"]]
            label = LINK_LABELS.get(link["link_type"], link["link_type"] or "связь")
            line = (f"- [[Идеи/{filenames[other['id']]}|{_short(other['idea'])}]]"
                    f" — **{label}**")
            if link["confidence"] not in (None, ""):
                try:
                    line += f" (уверенность {float(link['confidence']):.2f})"
                except (TypeError, ValueError):
                    pass
            if link["explanation"]:
                line += ": " + _one_line(link["explanation"])
            parts.append(line)
        parts.append("")
        if notes.get(row["id"]):
            parts += ["## Заметки", ""]
            for note in notes[row["id"]]:
                stamp = (note["created"] or "")[:10]
                parts.append(f"- {stamp + ' — ' if stamp else ''}{_one_line(note['text'])}")
            parts.append("")
        files[f"Идеи/{filenames[row['id']]}.md"] = "\n".join(parts)

    files["README.md"] = "\n".join([
        "# Marginalia — библиотека идей",
        "",
        f"Книг: {len(books)}, идей: {len(idea_rows)}, связей: {len(link_rows)}.",
        "",
        "## Как открыть в Obsidian",
        "",
        "1. Распакуйте архив в папку, например `Marginalia`.",
        "2. В Obsidian: «Open folder as vault» → выберите эту папку.",
        "3. Откройте «Graph view» — связи идей появятся как граф.",
        "",
        "Папка `Книги` — конспект и ключевые идеи каждой книги.",
        "Папка `Идеи` — по файлу на идею: цитата, мысль, заметки и связи.",
        "",
        "Связи записаны ссылками вида `[[Идеи/файл|подпись]] — **тип связи**`.",
        "Типы: поддерживает, противоречит, развивает, пример к, то же самое.",
        "",
    ])

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.writestr(path, files[path])
    stats = {"books": len(books), "ideas": len(idea_rows), "links": len(link_rows),
             "files": len(files)}
    return buffer.getvalue(), stats


def main() -> None:
    data, stats = build_zip()
    print(f"Архив: {len(data)} байт, файлов {stats['files']},"
          f" книг {stats['books']}, идей {stats['ideas']}, связей {stats['links']}")


if __name__ == "__main__":
    main()
