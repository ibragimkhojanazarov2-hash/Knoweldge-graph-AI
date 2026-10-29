"""Извлечение идей из главы книги через класс-API чата.

Идея — короткая мысль из главы, у которой есть настоящая цитата из текста.
После ответа модели каждая цитата ищется в тексте главы: пробелы и неразрывные
пробелы не в счёт, остальное должно совпасть почти посимвольно. Цитата
заменяется на настоящий кусок текста книги, а идея без цитаты выбрасывается.

Запуск: python3 app/extract.py <book_id> <глава|all>
        all — все главы книги по порядку; идеи каждой главы сохраняются сразу.
        clean — удалить идеи служебных глав книги и все связи с ними.

Служебные главы (титул, благодарности, об авторе, примечания, содержание,
а также любая глава короче 1500 знаков) не разбираются на идеи: мыслей книги
в них нет, а модель всё равно что-нибудь да придумает.
"""
from __future__ import annotations

import difflib
import json
import sys
from datetime import datetime, timezone

from llm import chat
from server import connect, get_book, get_chapter, init_db

CHUNK = 8000  # знаков текста в одном запросе

PROMPT = """Ты помогаешь читателю разобрать книгу на идеи.

Вот кусок главы «{title}»:

<<<
{text}
>>>

Выдели 3–5 главных идей этого куска. Для каждой идеи:
- idea — одна ясная фраза своими словами, на языке книги;
- quote — дословная цитата из текста выше, 1–3 предложения (не больше 400 знаков).
  Копируй её посимвольно, как в тексте: те же слова, те же знаки, без многоточий;
- why — одна фраза о том, почему это важная мысль книги.

Ответь только JSON-массивом объектов, без другого текста:
[{{"idea": "...", "quote": "...", "why": "..."}}]"""


def parse_ideas(answer: str) -> list[dict]:
    """Достаёт JSON-массив из ответа модели, даже если он обёрнут в ```json."""
    start, end = answer.find("["), answer.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("В ответе модели нет JSON-массива:\n" + answer[:400])
    data = json.loads(answer[start:end + 1])
    if not isinstance(data, list):
        raise ValueError("Модель вернула не список.")
    ideas = []
    for item in data:
        if not isinstance(item, dict):
            continue
        idea = str(item.get("idea") or "").strip()
        quote = str(item.get("quote") or "").strip()
        why = str(item.get("why") or "").strip()
        if idea and quote:
            ideas.append({"idea": idea, "quote": quote, "why": why})
    return ideas


MIN_RATIO = 0.9  # насколько цитата должна быть похожа на текст главы

SERVICE_MIN = 1500  # глава короче этого — служебная
SERVICE_TITLEPAGE_MAX = 4000  # титул длиннее этого не бывает

# Метки выходных данных на титульной странице.
SERVICE_MARKS = ("copyright", "isbn", "все права защищены", "©")

# Служебные главы: заголовок говорит сам за себя.
SERVICE_TITLES = (
    "титул", "благодар", "об авторе", "примечани", "содержание", "оглавление",
    "посвящение", "колофон", "copyright", "contents", "acknowledg", "about the author",
    "notes", "title page", "index",
)


def plain(text: str) -> str:
    """Для сравнения заголовков: нижний регистр, ё → е, без знаков препинания."""
    text = (text or "").lower().replace("ё", "е")
    return " ".join("".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text).split())


def service_reason(chapter: dict, book_title: str | None = None) -> str | None:
    """Почему глава служебная (None — обычная глава с текстом книги).

    chapter — строка из get_book (idx, title, chars) или из get_chapter (idx, title, text).
    book_title — название книги: первая глава, чей заголовок повторяет его, — титул.
    """
    if not isinstance(chapter, dict):
        chapter = dict(chapter)
    title = " ".join((chapter.get("title") or "").lower().split())
    for word in SERVICE_TITLES:
        if word in title:
            return f"служебный заголовок «{chapter['title']}»"
    chars = chapter.get("chars")
    if chars is None:
        chars = len((chapter.get("text") or "").strip())
    if chars < SERVICE_MIN:
        return f"короткая глава, {chars} знаков"
    if chars < SERVICE_TITLEPAGE_MAX and chapter.get("idx") == 1:
        book = plain(book_title or "")
        if book and book in plain(chapter.get("title")):
            return f"титул: заголовок главы повторяет название книги, {chars} знаков"
        head = (chapter.get("text") or chapter.get("head") or "")[:300].lower()
        if any(mark in head for mark in SERVICE_MARKS):
            return f"титул: выходные данные в начале главы, {chars} знаков"
    return None


def squash(text: str) -> tuple[str, list[int]]:
    """Схлопывает все пробелы в один и запоминает, откуда взялся каждый знак."""
    letters: list[str] = []
    places: list[int] = []
    after_space = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if not after_space:
                letters.append(" ")
                places.append(i)
            after_space = True
        else:
            letters.append(ch)
            places.append(i)
            after_space = False
    return "".join(letters), places


def find_quote(quote: str, text: str) -> tuple[str, float] | None:
    """Ищет цитату в тексте главы.

    Возвращает настоящий кусок текста книги и похожесть (1.0 — точное
    совпадение) или None, если цитаты в тексте нет.
    """
    short, places = squash(text)
    wanted = squash(quote)[0]
    if len(wanted) < 20:
        return None
    best: tuple[float, int, int] | None = None
    for part in (0.5, 0.3, 0.7, 0.15, 0.85):
        offset = int(len(wanted) * part)
        seed = wanted[offset:offset + 40]
        if len(seed) < 20:
            continue
        pos = short.find(seed)
        if pos == -1:
            continue
        start = pos - offset
        for shift in range(-8, 9):
            begin = start + shift
            if begin < 0 or begin >= len(short):
                continue
            window = short[begin:begin + len(wanted)]
            matcher = difflib.SequenceMatcher(None, wanted, window, autojunk=False)
            if matcher.quick_ratio() < 0.8:
                continue
            ratio = matcher.ratio()
            if ratio >= MIN_RATIO and (best is None or ratio > best[0]):
                best = (ratio, begin, begin + len(wanted))
        if best and best[0] > 0.98:
            break
    if best is None:
        return None
    ratio, begin, end = best
    while end > begin and short[end - 1] == " ":
        end -= 1
    while begin < end and short[begin] == " ":
        begin += 1
    real = text[places[begin]:places[end - 1] + 1].strip()
    return real, round(ratio, 3)


def check_ideas(ideas: list[dict], text: str) -> tuple[list[dict], int]:
    """Оставляет только идеи, чья цитата нашлась в главе. Цитату берёт из книги."""
    kept, dropped = [], 0
    for idea in ideas:
        found = find_quote(idea["quote"], text)
        if found is None:
            dropped += 1
            print(f"  цитата не найдена в главе, идея отброшена: «{idea['quote'][:80]}…»")
            continue
        quote, ratio = found
        if ratio < 1.0:
            print(f"  цитата чуть отличалась от книги ({ratio}), взял текст книги")
        kept.append({"idea": idea["idea"], "quote": quote, "why": idea["why"]})
    return kept, dropped


def split_text(text: str, limit: int = CHUNK) -> list[str]:
    """Режет главу по абзацам на куски не длиннее limit знаков."""
    chunks, current = [], ""
    for paragraph in text.split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        while len(paragraph) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        if len(current) + len(paragraph) + 1 > limit and current:
            chunks.append(current)
            current = ""
        current = f"{current}\n{paragraph}" if current else paragraph
    if current:
        chunks.append(current)
    return chunks


def save_ideas(book_id: str, chapter: int, ideas: list[dict]) -> None:
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as db:
        db.execute("DELETE FROM ideas WHERE book_id = ? AND chapter = ?", (book_id, chapter))
        db.executemany(
            "INSERT INTO ideas (book_id, chapter, idea, quote, why, created)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(book_id, chapter, i["idea"], i["quote"], i["why"], created) for i in ideas],
        )


def extract_chapter(book_id: str, chapter: dict) -> tuple[int, int]:
    """Делает идеи одной главы и сохраняет их. Возвращает (сохранено, отброшено)."""
    chapter_idx = chapter["idx"]
    chunks = split_text(chapter["text"])
    print(f"Глава «{chapter['title']}»: {len(chapter['text'])} знаков, кусков: {len(chunks)}", flush=True)

    ideas: list[dict] = []
    dropped = 0
    for number, chunk in enumerate(chunks, 1):
        prompt = PROMPT.format(title=chapter["title"], text=chunk)
        answer = chat([{"role": "user", "content": prompt}])
        found = parse_ideas(answer)
        kept, lost = check_ideas(found, chapter["text"])
        dropped += lost
        print(f"  кусок {number}: идей {len(found)}, с цитатой из книги {len(kept)}", flush=True)
        ideas.extend(kept)

    save_ideas(book_id, chapter_idx, ideas)
    print(f"Сохранено идей: {len(ideas)}" + (f", без цитаты отброшено: {dropped}" if dropped else ""),
          flush=True)
    for idea in ideas[:3]:
        print(f"- {idea['idea']}\n  «{idea['quote'][:120]}»")
    return len(ideas), dropped


def clean_service(book_id: str) -> None:
    """Удаляет идеи служебных глав книги и все связи, которые на них ссылаются."""
    with connect() as db:
        row = db.execute("SELECT title FROM books WHERE id = ?", (book_id,)).fetchone()
        book_title = row["title"] if row else None
        chapters = db.execute(
            "SELECT idx, title, length(text) AS chars, substr(text, 1, 300) AS head"
            " FROM chapters WHERE book_id = ? ORDER BY idx", (book_id,)
        ).fetchall()
        if not chapters:
            print(f"Книга {book_id} не найдена.", file=sys.stderr)
            raise SystemExit(1)
        service = [(c["idx"], service_reason(dict(c), book_title)) for c in chapters]
        service = [(idx, reason) for idx, reason in service if reason]
        if not service:
            print("Служебных глав нет, чистить нечего.", flush=True)
            return
        for idx, reason in service:
            print(f"Служебная глава {idx}: {reason}", flush=True)
        marks = ",".join("?" * len(service))
        idea_ids = [row[0] for row in db.execute(
            f"SELECT id FROM ideas WHERE book_id = ? AND chapter IN ({marks})",
            (book_id, *(idx for idx, _ in service)),
        )]
        links = key_ideas = 0
        if idea_ids:
            id_marks = ",".join("?" * len(idea_ids))
            link_ids = [r[0] for r in db.execute(
                f"SELECT id FROM links WHERE from_id IN ({id_marks}) OR to_id IN ({id_marks})",
                (*idea_ids, *idea_ids),
            )]
            sources = [f"idea:{i}" for i in idea_ids] + [f"link:{i}" for i in link_ids]
            db.execute(
                f"DELETE FROM reviews WHERE source IN ({','.join('?' * len(sources))})",
                sources,
            )
            links = db.execute(
                f"DELETE FROM links WHERE from_id IN ({id_marks}) OR to_id IN ({id_marks})",
                (*idea_ids, *idea_ids),
            ).rowcount
            db.execute(
                f"DELETE FROM link_marks WHERE from_id IN ({id_marks}) OR to_id IN ({id_marks})",
                (*idea_ids, *idea_ids),
            )
            db.execute(f"DELETE FROM idea_marks WHERE idea_id IN ({id_marks})", idea_ids)
            db.execute(f"DELETE FROM notes WHERE idea_id IN ({id_marks})", idea_ids)
            db.execute(f"DELETE FROM ideas WHERE id IN ({id_marks})", idea_ids)
        key_ideas = db.execute(
            f"DELETE FROM key_ideas WHERE book_id = ? AND chapter IN ({marks})",
            (book_id, *(idx for idx, _ in service)),
        ).rowcount
        # Отметки проверки на идеи и связи, которых уже нет, показывать некому.
        dangling = db.execute(
            "DELETE FROM reviews WHERE"
            " (source LIKE 'idea:%' AND CAST(substr(source, 6) AS INTEGER) NOT IN (SELECT id FROM ideas))"
            " OR (source LIKE 'link:%' AND CAST(substr(source, 6) AS INTEGER) NOT IN (SELECT id FROM links))"
        ).rowcount
    print(f"Удалено: идей {len(idea_ids)}, связей {links}, ключевых идей {key_ideas}"
          + (f", осиротевших отметок {dangling}" if dangling else "") + ".", flush=True)


def main() -> None:
    if len(sys.argv) != 3:
        print("Запуск: python3 app/extract.py <book_id> <глава|all|clean>", file=sys.stderr)
        raise SystemExit(2)
    book_id, where = sys.argv[1], sys.argv[2]

    init_db()
    book = get_book(book_id)
    if book is None:
        print(f"Книга {book_id} не найдена.", file=sys.stderr)
        raise SystemExit(1)
    print(f"Книга «{book['title']}»: глав {len(book['chapters'])}", flush=True)

    if where == "clean":
        clean_service(book_id)
        return

    if where == "all":
        indexes = []
        for chapter in book["chapters"]:
            reason = service_reason(chapter, book["title"])
            if reason:
                print(f"Пропускаю главу {chapter['idx']} «{chapter['title']}»: {reason}", flush=True)
                continue
            indexes.append(chapter["idx"])
        print(f"К разбору глав: {len(indexes)}", flush=True)
    else:
        indexes = [int(where)]

    total, dropped = 0, 0
    for number, chapter_idx in enumerate(indexes, 1):
        chapter = get_chapter(book_id, chapter_idx)
        if chapter is None:
            print(f"Глава {chapter_idx} книги {book_id} не найдена.", file=sys.stderr)
            raise SystemExit(1)
        reason = service_reason(chapter, book["title"])
        if reason:
            print(f"\n[{number}/{len(indexes)}] глава {chapter_idx}: пропускаю, {reason}", flush=True)
            continue
        print(f"\n[{number}/{len(indexes)}] глава {chapter_idx}", flush=True)
        kept, lost = extract_chapter(book_id, chapter)
        total += kept
        dropped += lost

    if where == "all":
        print(f"\nИтого идей по книге: {total}"
              + (f", без цитаты отброшено: {dropped}" if dropped else ""), flush=True)


if __name__ == "__main__":
    main()
