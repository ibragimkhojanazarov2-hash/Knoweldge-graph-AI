"""Замер извлечения идей и слепая проверка цитат.

Режимы:
  python3 app/study.py        — замер: results/extract-log.txt и results/table1.csv;
  python3 app/study.py blind  — слепой лист results/blind-sheet.csv, ключ
                                results/blind-key.csv и results/faithfulness.csv;
  python3 app/study.py score  — precision по источникам после разметки → results/table2.csv.

В замере скрипт проходит по выбранным главам, режет их на куски так же, как
extract.py, и для каждого куска зовёт модель с тем же PROMPT. Каждую
предложенную цитату проверяет та же find_quote: цитата ищется в полном тексте
главы.

Итог по куску:
- proposed — сколько идей вернула модель;
- exact — цитата нашлась с похожестью 1.0 (после округления до 0.001);
- near — цитата нашлась, но похожесть меньше 1.0 (не ниже MIN_RATIO = 0.9);
- dropped — цитата не нашлась, идея выбрасывается, как в extract.py.

proposed = exact + near + dropped. В базу скрипт ничего не пишет.

Слепой лист: в каждой главе поровну цитат идей, первых предложений главы
и случайных предложений (seed=42), вперемешку. Источника в листе нет — он
в blind-key.csv; колонку main_idea заполняет читатель, скрипт её не трогает.
score считает precision как «main_idea = yes» / «размечено» по каждому источнику.

Базу можно подменить переменной окружения MARGINALIA_DB.
"""
from __future__ import annotations

import csv
import random
import re
import sys
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
BLIND_SHEET = RESULTS / "blind-sheet.csv"
BLIND_KEY = RESULTS / "blind-key.csv"
TABLE2 = RESULTS / "table2.csv"
FAITHFUL = RESULTS / "faithfulness.csv"

SEED = 42  # зерно для случайных предложений и перемешивания
MAX_PER_SOURCE = 10  # не больше стольких цитат каждого источника на главу
SENTENCE_MIN = 60  # короче — обрывок, а не предложение
SENTENCE_MAX = 400  # длиннее — скорее абзац

SOURCE_IDEA = "цитата идеи"
SOURCE_FIRST = "первое предложение"
SOURCE_RANDOM = "случайное предложение"
SOURCES = (SOURCE_IDEA, SOURCE_FIRST, SOURCE_RANDOM)

SHEET_HEADER = ("id", "глава", "предложение", "main_idea")
KEY_HEADER = ("id", "книга", "глава", "источник")
FAITH_HEADER = ("книга", "глава", "идея", "цитата", "same")
TABLE2_HEADER = ("источник", "всего", "размечено", "main_idea_да", "precision")

YES = {"yes", "y", "да", "1", "true", "+", "верно"}
NO = {"no", "n", "нет", "0", "false", "-", "неверно"}

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
    """Печатает строку в терминал и сразу пишет её в журнал, если он открыт."""
    print(text, flush=True)
    if _log is not None:
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


def norm(text: str) -> str:
    """Текст в одну строку: все пробелы и переносы схлопнуты в один пробел."""
    return " ".join((text or "").replace("\u00a0", " ").split())


def split_sentences(text: str) -> list[str]:
    """Предложения главы подходящей длины, без склейки соседних абзацев.

    Абзацы режутся по знаку конца предложения; кусок, который начинается
    со строчной буквы, приклеивается к предыдущему (так бывает после
    сокращения или кавычки). Слишком короткие и слишком длинные куски
    в лист не попадают: по ним нельзя судить о главной мысли.
    """
    sentences: list[str] = []
    for paragraph in re.split(r"\n+", text):
        paragraph = norm(paragraph)
        if not paragraph:
            continue
        merged: list[str] = []
        for part in re.split(r"(?<=[.!?…])\s+", paragraph):
            part = part.strip()
            if not part:
                continue
            if merged and part[0].islower():
                merged[-1] += " " + part
            else:
                merged.append(part)
        sentences.extend(merged)
    return [s for s in sentences if SENTENCE_MIN <= len(s) <= SENTENCE_MAX]


def unique_texts(texts: list[str]) -> list[str]:
    """Убирает повторы, сохраняя порядок."""
    seen, result = set(), []
    for text in texts:
        if text not in seen:
            seen.add(text)
            result.append(text)
    return result


def write_csv(path: Path, header: tuple[str, ...], rows: list[list]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(header)
        writer.writerows(rows)


def read_csv_rows(path: Path) -> list[dict]:
    """Читает CSV так, как его мог сохранить Excel: UTF-8, UTF-16 или cp1251."""
    rows = None
    for encoding in ("utf-8-sig", "utf-16", "cp1251"):
        try:
            with path.open(encoding=encoding, newline="") as file:
                rows = list(csv.DictReader(file))
            break
        except (UnicodeError, csv.Error):
            continue
    if rows is None:
        raise SystemExit(f"Не удалось прочитать {path.name}: неизвестная кодировка.")
    return [
        {(key or "").strip().lower(): (value or "").strip() for key, value in row.items()}
        for row in rows
    ]


def sheet_is_annotated(path: Path) -> bool:
    """Есть ли в листе хоть одна отметка в main_idea."""
    if not path.exists():
        return False
    return any(row.get("main_idea") for row in read_csv_rows(path))


def load_ideas(book_id: str, chapter: int) -> list[dict]:
    """Идеи главы из базы: id, текст идеи и настоящая цитата."""
    with connect() as db:
        rows = db.execute(
            "SELECT id, idea, quote FROM ideas WHERE book_id = ? AND chapter = ? ORDER BY id",
            (book_id, chapter),
        ).fetchall()
    return [
        {"id": row["id"], "idea": norm(row["idea"]), "quote": norm(row["quote"])}
        for row in rows
        if norm(row["quote"])
    ]


def blind() -> None:
    """Слепой лист и ключ к нему для глав из PLAN.

    В каждой главе одинаковое число цитат идей, первых предложений главы
    и случайных предложений; строки перемешаны (seed=42). В листе нет
    источника, а main_idea оставлен пустым: его заполняет читатель.
    """
    RESULTS.mkdir(parents=True, exist_ok=True)
    if sheet_is_annotated(BLIND_SHEET):
        raise SystemExit(
            f"{BLIND_SHEET.name} уже размечен — не перезаписываю. "
            "Уберите отметки или переименуйте файл, если нужен новый лист."
        )

    rng = random.Random(SEED)
    sheet, key, faithful = [], [], []
    next_id = 1
    say(f"Слепой лист: seed={SEED}, не больше {MAX_PER_SOURCE} строк "
        f"каждого источника на главу")

    for title, indexes in PLAN:
        book_id = find_book(title)
        for idx in indexes:
            chapter = get_chapter(book_id, idx)
            if chapter is None:
                raise SystemExit(f"Глава {idx} книги «{title}» не найдена в базе.")
            chapter_label = f"{idx}. {chapter['title']}"
            sheet_label = f"{title}, глава {chapter_label}"

            ideas = load_ideas(book_id, idx)
            for idea in ideas:  # все идеи главы — для faithfulness.csv
                faithful.append([title, chapter_label, idea["idea"], idea["quote"], ""])

            chosen_ideas, seen = [], set()
            for idea in ideas:  # одна и та же цитата дважды в лист не попадает
                if idea["quote"] not in seen:
                    seen.add(idea["quote"])
                    chosen_ideas.append(idea)

            sentences = unique_texts(split_sentences(chapter["text"]))
            count = min(len(chosen_ideas), MAX_PER_SOURCE, len(sentences) // 2)
            while count > 0:
                chosen = rng.sample(chosen_ideas, count)
                used = {idea["quote"] for idea in chosen}
                pool = [s for s in sentences if s not in used]
                if len(pool) >= 2 * count:
                    break
                count -= 1
            if count == 0:
                say(f"  глава {chapter_label}: мало данных "
                    f"(идей {len(chosen_ideas)}, предложений {len(sentences)}), пропускаю")
                continue

            items = [(SOURCE_IDEA, idea["quote"]) for idea in chosen]
            items += [(SOURCE_FIRST, s) for s in pool[:count]]
            items += [(SOURCE_RANDOM, s) for s in rng.sample(pool[count:], count)]
            rng.shuffle(items)
            for source, text in items:
                sheet.append([next_id, sheet_label, text, ""])
                key.append([next_id, title, chapter_label, source])
                next_id += 1
            say(f"  глава {chapter_label}: идей {len(ideas)}, строк на источник {count}, "
                f"предложений для сравнения {len(sentences)}")

    if not sheet:
        raise SystemExit("Нечего размечать: в главах нет идей или предложений.")

    write_csv(BLIND_SHEET, SHEET_HEADER, sheet)
    write_csv(BLIND_KEY, KEY_HEADER, key)
    write_csv(FAITHFUL, FAITH_HEADER, faithful)

    say(f"\nЛист: {BLIND_SHEET.relative_to(BASE)} — {len(sheet)} строк, main_idea пустой")
    for source in SOURCES:
        say(f"  {source}: {sum(1 for row in key if row[3] == source)}")
    say(f"Ключ: {BLIND_KEY.relative_to(BASE)} — при разметке его не открывать")
    say(f"Проверка цитат: {FAITHFUL.relative_to(BASE)} — {len(faithful)} идей, same пустой")
    say("\nДальше: заполните main_idea (yes/no) в листе и same (yes/no) в faithfulness.csv, "
        "затем: python3 app/study.py score")


def score() -> None:
    """Precision по источникам после ручной разметки листа."""
    for path in (BLIND_SHEET, BLIND_KEY):
        if not path.exists():
            raise SystemExit(f"Нет {path.name}. Сначала: python3 app/study.py blind")

    source_by_id = {}
    for row in read_csv_rows(BLIND_KEY):
        item_id = row.get("id", "")
        if item_id:
            source_by_id[item_id] = row.get("источник", "")

    total: dict[str, int] = {}
    marked: dict[str, int] = {}
    yes: dict[str, int] = {}
    sheet_ids, blank, bad_marks, unknown = set(), 0, [], []

    for row in read_csv_rows(BLIND_SHEET):
        item_id = row.get("id", "")
        sheet_ids.add(item_id)
        source = source_by_id.get(item_id)
        if source is None:
            unknown.append(item_id)
            continue
        total[source] = total.get(source, 0) + 1
        value = row.get("main_idea", "").strip().lower()
        if not value:
            blank += 1
        elif value in YES:
            marked[source] = marked.get(source, 0) + 1
            yes[source] = yes.get(source, 0) + 1
        elif value in NO:
            marked[source] = marked.get(source, 0) + 1
        else:
            bad_marks.append((item_id, value))

    missing = [row.get("id", "") for row in read_csv_rows(BLIND_KEY)
               if row.get("id", "") and row.get("id", "") not in sheet_ids]
    marked_all = sum(marked.values())
    if marked_all == 0:
        raise SystemExit("В blind-sheet.csv нет ни одной понятной отметки main_idea "
                         "(yes/no). Заполните колонку и запустите score снова.")

    rows = []
    for source in SOURCES:
        total_n, marked_n, yes_n = total.get(source, 0), marked.get(source, 0), yes.get(source, 0)
        rows.append([source, total_n, marked_n, yes_n,
                     f"{yes_n / marked_n:.3f}" if marked_n else ""])
    total_all = sum(total.get(s, 0) for s in SOURCES)
    yes_all = sum(yes.get(s, 0) for s in SOURCES)
    rows.append(["все источники", total_all, marked_all, yes_all,
                 f"{yes_all / marked_all:.3f}"])
    write_csv(TABLE2, TABLE2_HEADER, rows)

    say(f"Размечено {marked_all} из {total_all}: пусто {blank}, "
        f"непонятных отметок {len(bad_marks)}")
    for source in SOURCES:
        total_n, marked_n, yes_n = total.get(source, 0), marked.get(source, 0), yes.get(source, 0)
        precision = f"{yes_n / marked_n:.1%}" if marked_n else "—"
        say(f"  {source}: да {yes_n} из {marked_n} (precision {precision}), всего {total_n}")
    say(f"Все источники: precision {yes_all / marked_all:.1%}")
    say(f"Таблица: {TABLE2.relative_to(BASE) if TABLE2.is_relative_to(BASE) else TABLE2}")

    problems = False
    if blank:
        say(f"Не размечено строк: {blank} — они не вошли в precision.")
        problems = True
    if bad_marks:
        say("Непонятные отметки (нужно yes или no): "
            + ", ".join(f"id {i}: «{v}»" for i, v in bad_marks[:10])
            + (" …" if len(bad_marks) > 10 else ""))
        problems = True
    if unknown:
        say("Строки листа без id в ключе: " + ", ".join(unknown[:10])
            + (" …" if len(unknown) > 10 else ""))
        problems = True
    if missing:
        say("Строки ключа, которых нет в листе: " + ", ".join(missing[:10])
            + (" …" if len(missing) > 10 else ""))
        problems = True
    if problems:
        raise SystemExit(3)


def run_study() -> None:
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


def main() -> None:
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "study"
    if len(sys.argv) > 2 or mode not in ("study", "blind", "score"):
        raise SystemExit("Запуск: python3 app/study.py [blind|score]")
    if mode == "blind":
        blind()
    elif mode == "score":
        score()
    else:
        run_study()


if __name__ == "__main__":
    main()
