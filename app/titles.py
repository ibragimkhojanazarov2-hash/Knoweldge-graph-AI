"""Короткие заголовки идей для карты: 3–6 слов вместо обрезанного текста.

Подпись на карте — не первая строка идеи, а её суть: «Удача не отменяет
подготовки». Такую подпись модель пишет по самому тексту идеи, по 12 идей
за один запрос. Заголовок сохраняется в ideas.title и попадает в API карты.

Скрипт доделывает только идеи без заголовка — можно запускать повторно.

Запуск: python3 app/titles.py <book_id|all>
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone

from llm import chat
from server import connect, get_book, init_db, list_books

BATCH = 12      # идей в одном запросе
MAX_WORDS = 7   # длиннее — модель не послушалась, берём первые слова сами
MAX_CHARS = 30  # длиннее — подпись не влезает в колонку карты
TAIL = set("и в во на с со к по о об от до из за для при над под без что как или".split())

PROMPT = """Ты придумываешь короткие подписи для карты идей книги «{title}».

Вот идеи:
{items}

Для каждой идеи напиши заголовок из 3–6 слов: её суть, а не начало фразы.
Заголовок без кавычек, без точки в конце, с большой буквы, по-русски.
Он должен быть понятен сам по себе, вне книги и не длиннее 30 символов.

Ответь только JSON-массивом, без другого текста:
[{{"n": 1, "title": "Удача не отменяет подготовки"}}]"""


def clean(title: str) -> str:
    """Убираем кавычки, точку и лишние пробелы: заголовок — одна строка."""
    text = str(title or "").strip().strip("«»\"'").strip()
    text = re.sub(r"\s+", " ", text).rstrip(".")
    return text


def shorten(title: str) -> str:
    """Отбрасываем хвост, пока подпись не влезет в колонку карты."""
    words = title.split()
    while len(words) > 3 and len(" ".join(words)) > MAX_CHARS:
        words.pop()
    while len(words) > 2 and words[-1].lower().strip(",;:—-") in TAIL:
        words.pop()
    return " ".join(words)


def fallback(idea: str) -> str:
    """Если модель не дала заголовок: первые слова самой идеи."""
    words = re.findall(r"\S+", idea)
    return shorten(" ".join(words[:6]).rstrip(",;:—-") or idea[:40])


def parse(answer: str, batch: list[dict]) -> dict[int, str]:
    """Достаёт заголовки из ответа модели: номер в запросе → текст."""
    start, end = answer.find("["), answer.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("В ответе модели нет JSON-массива:\n" + answer[:400])
    data = json.loads(answer[start:end + 1])
    if not isinstance(data, list):
        raise ValueError("Модель вернула не список заголовков.")
    result: dict[int, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            number = int(item.get("n"))
        except (TypeError, ValueError):
            continue
        if not 1 <= number <= len(batch):
            continue
        title = clean(item.get("title"))
        if not title:
            continue
        words = title.split()
        if len(words) > MAX_WORDS:
            title = " ".join(words[:6])
        result[number] = shorten(title)
    return result


def make_batches(items: list[dict]) -> list[list[dict]]:
    return [items[start:start + BATCH] for start in range(0, len(items), BATCH)]


def build_prompt(title: str, batch: list[dict]) -> str:
    items = "\n".join(
        f"{number}. {idea['idea']}" for number, idea in enumerate(batch, 1)
    )
    return PROMPT.format(title=title, items=items)


def save_titles(titles: dict[int, str]) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as db:
        db.executemany(
            "UPDATE ideas SET title = ?, created = COALESCE(created, ?) WHERE id = ?",
            [(text, now, idea_id) for idea_id, text in titles.items()],
        )


def load_ideas(book_id: str) -> tuple[str, list[dict]]:
    book = get_book(book_id)
    if book is None:
        raise SystemExit(f"Книга {book_id} не найдена.")
    with connect() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT id, idea FROM ideas WHERE book_id = ?"
            " AND (title = '' OR length(title) > ?) ORDER BY id",
            (book_id, MAX_CHARS),
        ).fetchall()]
    return book["title"], rows


def titles_for(book_id: str) -> int:
    title, items = load_ideas(book_id)
    print(f"Книга «{title}»: без заголовка {len(items)} идей", flush=True)
    if not items:
        return 0
    saved = 0
    batches = make_batches(items)
    for number, batch in enumerate(batches, 1):
        found = parse(chat([{"role": "user", "content": build_prompt(title, batch)}]), batch)
        titles = {batch[n - 1]["id"]: text for n, text in found.items()}
        for item in batch:
            if item["id"] not in titles:
                titles[item["id"]] = fallback(item["idea"])
        save_titles(titles)
        saved += len(titles)
        print(f"  запрос {number} из {len(batches)}: заголовков {len(found)} из {len(batch)}",
              flush=True)
    return saved


def main() -> None:
    if len(sys.argv) != 2:
        print("Запуск: python3 app/titles.py <book_id|all>", file=sys.stderr)
        raise SystemExit(2)
    where = sys.argv[1]

    init_db()
    book_ids = [book["id"] for book in list_books()] if where == "all" else [where]
    total = 0
    for book_id in book_ids:
        print(f"\n[{book_id}]", flush=True)
        total += titles_for(book_id)
    print(f"\nГотово заголовков: {total}", flush=True)


if __name__ == "__main__":
    main()
