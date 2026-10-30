"""Замер извлечения идей: сколько идей предлагает модель и сколько цитат находится.

Скрипт проходит по выбранным главам, режет их на куски так же, как extract.py,
и для каждого куска зовёт модель с тем же PROMPT. Каждую предложенную цитату
проверяет та же find_quote: цитата ищется в полном тексте главы.

Итог по куску:
- proposed — сколько идей вернула модель;
- exact — цитата нашлась с похожестью 1.0 (после округления до 0.001);
- near — цитата нашлась, но похожесть меньше 1.0 (не ниже MIN_RATIO = 0.9);
- dropped — цитата не нашлась, идея выбрасывается, как в extract.py.

proposed = exact + near + dropped. В базу скрипт ничего не пишет.

Запуск: python3 app/study.py
Пишет results/extract-log.txt (подробный ход) и results/table1.csv (таблица).
Базу можно подменить переменной окружения MARGINALIA_DB.
"""
from __future__ import annotations

import csv
import time
from datetime import datetime
from pathlib import Path

from extract import MIN_RATIO, PROMPT, find_quote, parse_ideas, split_text
from llm import MODEL, chat
from server import connect, get_chapter

BASE = Path(__file__).resolve().parent.parent
RESULTS = BASE / "results"
LOG = RESULTS / "extract-log.txt"
TABLE = RESULTS / "table1.csv"

# Что разбираем: название книги в базе → номера глав.
PLAN = (
    ("Зеленый свет", (2, 3, 4, 5)),
    ("Наедине с собой", (3, 4)),
)

# Книга, у которой просят посчитать знаки.
COUNT_BOOK = "Наедине с собой"

CSV_HEADER = ("книга", "глава", "pieces", "proposed", "exact", "near", "dropped")

_log = None  # открытый файл журнала


def say(text: str = "") -> None:
    """Печатает строку в терминал и сразу пишет её в журнал."""
    print(text, flush=True)
    _log.write(text + "\n")
    _log.flush()


def num(value: int) -> str:
    """1234567 → «1 234 567»: так числа читаются в статье."""
    return f"{value:,}".replace(",", " ")


def short(text: str, limit: int = 100) -> str:
    """Цитата в одну строку, без переносов, не длиннее limit знаков."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def diff_hint(wanted: str, real: str) -> str:
    """Где расходятся цитата модели и найденный текст книги (для near)."""
    a = " ".join(wanted.split())
    b = " ".join(real.split())
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            left, right = max(0, i - 40), i + 40
            return (f"первое расхождение, знак {i + 1}: модель «…{a[left:right]}…», "
                    f"книга «…{b[left:right]}…»")
    if len(a) != len(b):
        edge = min(len(a), len(b))
        return f"цитаты совпадают до знака {edge}, дальше длина разная: {len(a)} и {len(b)}"
    return "после схлопывания пробелов цитаты совпадают — разница только в пробелах"


def find_book(title: str) -> str:
    with connect() as db:
        row = db.execute(
            "SELECT id FROM books WHERE title = ? ORDER BY added DESC LIMIT 1", (title,)
        ).fetchone()
    if row is None:
        raise SystemExit(f"Книга «{title}» не найдена в базе.")
    return row["id"]


def study_chapter(chapter: dict) -> dict:
    """Разбирает одну главу и возвращает счётчики. В базу ничего не пишет."""
    pieces = split_text(chapter["text"])
    counts = {"pieces": len(pieces), "proposed": 0, "exact": 0, "near": 0, "dropped": 0}
    say(f"\nГлава {chapter['idx']} «{chapter['title']}»: "
        f"{num(len(chapter['text']))} знаков, кусков {len(pieces)}")

    for number, piece in enumerate(pieces, 1):
        started = time.monotonic()
        try:
            prompt = PROMPT.format(title=chapter["title"], text=piece)
            answer = chat([{"role": "user", "content": prompt}])
            ideas = parse_ideas(answer)
        except Exception as exc:  # ошибку показываем, но остальные куски не бросаем
            counts["errors"] = counts.get("errors", 0) + 1
            say(f"\n  кусок {number}/{len(pieces)}: ОШИБКА: {exc}")
            continue
        spent = time.monotonic() - started
        counts["proposed"] += len(ideas)
        say(f"\n  кусок {number}/{len(pieces)}: {num(len(piece))} знаков, "
            f"модель предложила идей: {len(ideas)}, {spent:.1f} с")

        for idea in ideas:
            found = find_quote(idea["quote"], chapter["text"])
            if found is None:
                counts["dropped"] += 1
                say(f"    нет цитаты ({len(idea['quote'])} знаков): «{short(idea['quote'])}»")
                say(f"      идея: {short(idea['idea'], 140)}")
                continue
            quote, ratio = found
            if ratio >= 1.0:
                counts["exact"] += 1
                say(f"    exact: «{short(quote)}»")
            else:
                counts["near"] += 1
                say(f"    near {ratio} (порог {MIN_RATIO}): «{short(quote, 400)}»")
                say(f"      модель прислала: «{short(idea['quote'], 400)}»")
                say(f"      {diff_hint(idea['quote'], quote)}")

    say(f"  итог главы: кусков {counts['pieces']}, идей {counts['proposed']}, "
        f"exact {counts['exact']}, near {counts['near']}, dropped {counts['dropped']}")
    return counts


def count_chars(title: str) -> None:
    """Печатает, сколько знаков текста в книге: всего и по главам."""
    with connect() as db:
        book = db.execute(
            "SELECT id, char_count FROM books WHERE title = ? ORDER BY added DESC LIMIT 1",
            (title,),
        ).fetchone()
        if book is None:
            raise SystemExit(f"Книга «{title}» не найдена в базе.")
        chapters = db.execute(
            "SELECT idx, title, length(text) AS chars FROM chapters"
            " WHERE book_id = ? ORDER BY idx", (book["id"],)
        ).fetchall()
    total = sum(c["chars"] for c in chapters)
    say(f"\nЗнаки в «{title}»")
    say(f"  всего в тексте глав: {num(total)} знаков, {len(chapters)} глав")
    if total != book["char_count"]:
        say(f"  в карточке книги записано {num(book['char_count'])} (посчитано при загрузке), "
            f"разница {num(book['char_count'] - total)}")
    for chapter in chapters:
        say(f"  глава {chapter['idx']:>2} «{chapter['title']}»: {num(chapter['chars'])}")
    service = sum(c["chars"] for c in chapters if c["idx"] in (1, len(chapters)))
    say(f"  без первой и последней служебных глав: {num(total - service)} знаков")


def main() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    global _log
    _log = LOG.open("w", encoding="utf-8")
    started = datetime.now()
    start_time = time.monotonic()

    with connect() as db:
        db_path = db.execute("PRAGMA database_list").fetchone()["file"]

    say("Замер извлечения идей и проверки цитат")
    say(f"Модель: {MODEL}; порог похожести цитаты: {MIN_RATIO}")
    say(f"Начало: {started:%Y-%m-%d %H:%M:%S}")
    say(f"База: {db_path}")

    rows, errors = [], 0
    totals = {"pieces": 0, "proposed": 0, "exact": 0, "near": 0, "dropped": 0}

    for title, indexes in PLAN:
        book_id = find_book(title)
        say(f"\n{'=' * 70}\nКнига «{title}» (id {book_id})")
        for idx in indexes:
            chapter = get_chapter(book_id, idx)
            if chapter is None:
                errors += 1
                say(f"\nГлава {idx}: ОШИБКА: глава не найдена в базе")
                continue
            counts = study_chapter(chapter)
            errors += counts.get("errors", 0)
            rows.append([
                title, f"{idx}. {chapter['title']}", counts["pieces"], counts["proposed"],
                counts["exact"], counts["near"], counts["dropped"],
            ])
            for key in totals:
                totals[key] += counts[key]
        book_rows = [r for r in rows if r[0] == title]
        say(f"\nИтог книги «{title}»: " + ", ".join(
            f"{key} {num(sum(r[i] for r in book_rows))}"
            for i, key in enumerate(("pieces", "proposed", "exact", "near", "dropped"), 2)
        ))

    with TABLE.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(CSV_HEADER)
        writer.writerows(rows)

    say(f"\n{'=' * 70}")
    say("Итог по всем главам: "
        + ", ".join(f"{key} {num(value)}" for key, value in totals.items()))
    if totals["proposed"]:
        exact = totals["exact"] / totals["proposed"]
        found = (totals["exact"] + totals["near"]) / totals["proposed"]
        say(f"Доля exact: {exact:.1%}; exact + near: {found:.1%} от всех идей модели")

    count_chars(COUNT_BOOK)

    spent = time.monotonic() - start_time
    say(f"\nТаблица: {TABLE.relative_to(BASE)}")
    say(f"Журнал: {LOG.relative_to(BASE)}")
    say(f"Готово за {spent:.0f} с, ошибок: {errors}")
    _log.close()

    if errors:
        print(f"Результаты неполные: ошибок {errors}, подробности в {LOG}.")
        raise SystemExit(3)


if __name__ == "__main__":
    main()
