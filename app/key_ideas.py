"""Выбор главных идей всей книги из готовых идей глав.

Берёт идеи, которые уже извлечены для каждой главы, и просит модель выбрать
самые важные из них. Модель отвечает номерами — значит, у каждой ключевой
идеи остаётся настоящая цитата из книги, найденная при извлечении.

Для книги с 20–40 главами выходит 20–40 ключевых идей, для короткого отрывка —
5–10. Старый набор книги при запуске заменяется новым.

Запуск: python3 app/key_ideas.py <book_id|all>
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from llm import chat
from server import connect, get_all_ideas, get_book, init_db, list_books

BATCH = 50      # идей в одном запросе при первом отборе
SHORTLIST = 12  # сколько идей оставить из одного куска
MAX_IDEAS = 30  # больше 40 ключевые идеи не бывают

PROMPT = """Ты помогаешь читателю понять книгу целиком.

Вот пронумерованные идеи из книги «{title}»:
{items}

Выбери {count} самых главных идей — те, без которых книгу не понять.
Нужны разные стороны книги, без повторов одной мысли.
Ответь только JSON-массивом номеров по порядку важности, например: [{example}]"""


def target_count(total: int) -> int:
    """Сколько ключевых идей просить: 5–10 для отрывка, 20–40 для книги."""
    return min(total, max(5, min(MAX_IDEAS, round(total * 0.14))))


def parse_numbers(answer: str, limit: int) -> list[int]:
    """Достаёт из ответа модели номера идей, оставляет существующие."""
    start, end = answer.find("["), answer.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("В ответе модели нет списка номеров:\n" + answer[:400])
    data = json.loads(answer[start:end + 1])
    if not isinstance(data, list):
        raise ValueError("Модель вернула не список номеров.")
    numbers = []
    for item in data:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= limit:
            numbers.append(number)
    return numbers


def dedupe(ideas: list[dict]) -> list[dict]:
    seen, result = set(), []
    for item in ideas:
        key = " ".join(item["idea"].lower().split())
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def ask_model(title: str, ideas: list[dict], count: int) -> list[dict]:
    """Один запрос к модели: выбрать count идей из списка."""
    items = "\n".join(f"{number}. {item['idea']}" for number, item in enumerate(ideas, 1))
    prompt = PROMPT.format(title=title, items=items, count=count, example="1, 7, 12")
    numbers = parse_numbers(chat([{"role": "user", "content": prompt}]), len(ideas))
    picked, seen = [], set()
    for number in numbers:
        if number in seen:
            continue
        seen.add(number)
        picked.append(ideas[number - 1])
    if not picked:
        raise RuntimeError("Модель не выбрала ни одной идеи.")
    return picked


def choose(title: str, ideas: list[dict], count: int) -> list[dict]:
    """Выбирает ключевые идеи. Длинный список сначала сокращает по кускам."""
    if len(ideas) > BATCH * 2:
        shortlist: list[dict] = []
        for start in range(0, len(ideas), BATCH):
            part = ideas[start:start + BATCH]
            shortlist.extend(ask_model(title, part, SHORTLIST))
            print(f"  отобрано из куска {start // BATCH + 1}: {len(shortlist)} идей", flush=True)
        print(f"Короткий список: {len(shortlist)} идей", flush=True)
        ideas = dedupe(shortlist)
    return dedupe(ask_model(title, ideas, count))[:count]


def save_key_ideas(book_id: str, ideas: list[dict]) -> None:
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as db:
        db.execute("DELETE FROM key_ideas WHERE book_id = ?", (book_id,))
        db.executemany(
            "INSERT INTO key_ideas (book_id, rank, chapter, idea, quote, why, created)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(book_id, rank, i["chapter"], i["idea"], i["quote"], i["why"], created)
             for rank, i in enumerate(ideas, 1)],
        )


def key_ideas_for(book_id: str) -> int:
    book = get_book(book_id)
    if book is None:
        print(f"Книга {book_id} не найдена.", file=sys.stderr)
        return 0
    ideas = get_all_ideas(book_id)
    print(f"Книга «{book['title']}»: идей в главах {len(ideas)}", flush=True)
    if not ideas:
        print("Идей нет — сначала запустите extract.py.", file=sys.stderr)
        return 0

    count = target_count(len(ideas))
    print(f"Прошу у модели {count} ключевых идей", flush=True)
    chosen = choose(book["title"], ideas, count)
    save_key_ideas(book_id, chosen)
    print(f"Сохранено ключевых идей: {len(chosen)}", flush=True)
    for number, item in enumerate(chosen[:3], 1):
        print(f"{number}. {item['idea']}\n   «{item['quote'][:100]}…»")
    return len(chosen)


def main() -> None:
    if len(sys.argv) != 2:
        print("Запуск: python3 app/key_ideas.py <book_id|all>", file=sys.stderr)
        raise SystemExit(2)
    where = sys.argv[1]

    init_db()
    if where == "all":
        book_ids = [book["id"] for book in list_books()]
    else:
        book_ids = [where]

    total = 0
    for book_id in book_ids:
        print(f"\n[{book_id}]", flush=True)
        total += key_ideas_for(book_id)
    if where == "all":
        print(f"\nИтого ключевых идей: {total}", flush=True)


if __name__ == "__main__":
    main()
