"""Краткий конспект книги по её ключевым идеям.

Берёт ключевые идеи книги и просит модель написать короткий конспект: пару
фраз о том, о чём книга, и главные мысли по пунктам. Старый конспект книги
при запуске заменяется новым.

Запуск: python3 app/summary.py <book_id|all>
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from extract import chat
from server import connect, get_book, get_key_ideas, init_db, list_books

POINTS_SHORT = 5  # пунктов для отрывка
POINTS_LONG = 8   # пунктов для целой книги

PROMPT = """Ты помогаешь читателю вспомнить книгу целиком.

Книга «{title}». Её ключевые идеи:
{items}

Напиши краткий конспект книги:
- about — 2–3 предложения о том, о чём книга и для чего она написана;
- points — {count} главных мыслей по порядку, каждая одной ясной фразой своими словами,
  на языке книги, без повторов.

Ответь только JSON-объектом, без другого текста:
{{"about": "...", "points": ["...", "..."]}}"""


def points_count(book: dict) -> int:
    """Сколько пунктов просить: 5 для короткого отрывка, 8 для целой книги."""
    return POINTS_SHORT if book["char_count"] < 60000 else POINTS_LONG


def parse_summary(answer: str) -> dict:
    """Достаёт из ответа модели объект с about и points."""
    start, end = answer.find("{"), answer.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("В ответе модели нет JSON-объекта:\n" + answer[:400])
    data = json.loads(answer[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("Модель вернула не объект.")
    about = str(data.get("about") or "").strip()
    if not about:
        raise ValueError("В конспекте нет вступления.")
    points = []
    for item in data.get("points") or []:
        text = str(item).strip()
        if text:
            points.append(text)
    if len(points) < 2:
        raise ValueError("Модель дала меньше двух пунктов конспекта.")
    return {"about": about, "points": points[:POINTS_LONG]}


def save_summary(book_id: str, summary: dict) -> None:
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as db:
        db.execute("DELETE FROM summary WHERE book_id = ?", (book_id,))
        db.execute(
            "INSERT INTO summary (book_id, about, points, created) VALUES (?, ?, ?, ?)",
            (book_id, summary["about"], json.dumps(summary["points"], ensure_ascii=False), created),
        )


def summary_for(book_id: str) -> bool:
    book = get_book(book_id)
    if book is None:
        print(f"Книга {book_id} не найдена.", file=sys.stderr)
        return False
    ideas = get_key_ideas(book_id)
    print(f"Книга «{book['title']}»: ключевых идей {len(ideas)}", flush=True)
    if not ideas:
        print("Ключевых идей нет — сначала запустите key_ideas.py.", file=sys.stderr)
        return False

    count = points_count(book)
    items = "\n".join(f"{item['rank']}. {item['idea']}" for item in ideas)
    prompt = PROMPT.format(title=book["title"], items=items, count=count)
    summary = parse_summary(chat([{"role": "user", "content": prompt}]))
    save_summary(book_id, summary)
    print(f"Сохранено: вступление и {len(summary['points'])} пунктов", flush=True)
    print(f"О чём книга: {summary['about'][:300]}", flush=True)
    for number, point in enumerate(summary["points"], 1):
        print(f"  {number}. {point}")
    return True


def main() -> None:
    if len(sys.argv) != 2:
        print("Запуск: python3 app/summary.py <book_id|all>", file=sys.stderr)
        raise SystemExit(2)
    where = sys.argv[1]

    init_db()
    if where == "all":
        book_ids = [book["id"] for book in list_books()]
    else:
        book_ids = [where]

    done = 0
    for book_id in book_ids:
        print(f"\n[{book_id}]", flush=True)
        if summary_for(book_id):
            done += 1
    if where == "all":
        print(f"\nКонспектов готово: {done} из {len(book_ids)}", flush=True)


if __name__ == "__main__":
    main()
