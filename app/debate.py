"""«Спор авторов»: что две книги говорят одинаково, в чём расходятся,
и какие ключевые идеи есть только у одной из них.

Как считается:
- «Согласны» — связи между идеями двух книг типа поддерживает / то же самое /
  развивает / пример (значок на карточке говорит, какая именно).
- «Спорят» — связи типа противоречит.
- «Только у А» и «Только у Б» — ключевые идеи, которым нет похожей в ключевых
  идеях другой книги. Похожесть — доля веса слов идеи, которая нашлась
  в идеях соседней книги (редкие слова весят больше).
- Резюме пишет модель по найденным связям, 3–4 предложения. Оно лежит
  в таблице debates и пересчитывается кнопкой «Пересчитать».

Запуск для проверки: python3 app/debate.py <id книги А> <id книги Б>
"""
from __future__ import annotations

import json
import math
import re
import sys
from datetime import datetime, timezone

from llm import chat
from ask import similar, words

MAX_LINKS = 8     # карточек в колонке «Согласны» / «Спорят»
MAX_ONLY = 6      # ключевых идей в колонке «Только у…»
SIMILAR_SHARE = 0.3  # ниже этой похожести идея считается своей, а не общей
MAX_VERDICT = 1200

AGREE_TYPES = ("supports", "same", "extends", "example")
DISAGREE_TYPES = ("contradicts",)

PROMPT = """Ты помогаешь читателю библиотеки Marginalia понять, в чём две книги расходятся.

Книга А: «{title_a}» — {author_a}.
Книга Б: «{title_b}» — {author_b}.

Что у книг общего:
{agree}

Где книги расходятся:
{disagree}

Напиши 3–4 предложения по-русски: в чём суть разногласия, в чём авторы всё же сходятся
и от чего зависит, кто прав. Без заголовков, списков и вступлений.
Опирайся только на список выше: не придумывай цитат и мыслей, которых там нет.
Если расхождений нет, так и скажи и объясни, чем книги дополняют друг друга.

Ответь только JSON-объектом, без другого текста:
{{"verdict": "..."}}"""


def load_books(book_a: str, book_b: str) -> tuple[dict, dict] | None:
    from server import connect

    with connect() as db:
        rows = db.execute(
            "SELECT id, title, author FROM books WHERE id IN (?, ?)", (book_a, book_b)
        ).fetchall()
    found = {row["id"]: dict(row) for row in rows}
    if book_a not in found or book_b not in found:
        return None
    return found[book_a], found[book_b]


def load_links(book_a: str, book_b: str) -> list[dict]:
    """Связи между идеями двух книг, приведённые к сторонам А и Б."""
    from server import connect

    with connect() as db:
        rows = db.execute(
            "SELECT l.from_id, l.to_id,"
            " COALESCE(NULLIF(lm.type, ''), l.type) AS type,"
            " l.explanation, l.confidence,"
            " fi.book_id AS from_book, fi.chapter AS from_chapter,"
            " fi.idea AS from_idea, fi.quote AS from_quote,"
            " fc.title AS from_chapter_title,"
            " ti.book_id AS to_book, ti.chapter AS to_chapter,"
            " ti.idea AS to_idea, ti.quote AS to_quote,"
            " tc.title AS to_chapter_title"
            " FROM links l"
            " JOIN ideas fi ON fi.id = l.from_id"
            " JOIN ideas ti ON ti.id = l.to_id"
            " LEFT JOIN chapters fc ON fc.book_id = fi.book_id AND fc.idx = fi.chapter"
            " LEFT JOIN chapters tc ON tc.book_id = ti.book_id AND tc.idx = ti.chapter"
            " LEFT JOIN link_marks lm ON lm.from_id = l.from_id AND lm.to_id = l.to_id"
            " WHERE ((fi.book_id = ? AND ti.book_id = ?)"
            "     OR (fi.book_id = ? AND ti.book_id = ?))"
            "   AND COALESCE(lm.verdict, '') <> 'wrong'"
            " ORDER BY l.confidence DESC, l.from_id, l.to_id",
            (book_a, book_b, book_b, book_a),
        ).fetchall()

    links = []
    for row in rows:
        item = dict(row)
        if item["from_book"] != book_a:  # связь записана от Б к А — переворачиваем
            item = {
                "from_id": item["to_id"], "to_id": item["from_id"],
                "type": item["type"], "explanation": item["explanation"],
                "confidence": item["confidence"],
                "a_idea": item["to_idea"], "a_quote": item["to_quote"],
                "a_chapter": item["to_chapter"], "a_chapter_title": item["to_chapter_title"],
                "b_idea": item["from_idea"], "b_quote": item["from_quote"],
                "b_chapter": item["from_chapter"],
                "b_chapter_title": item["from_chapter_title"],
            }
        else:
            item = {
                "from_id": item["from_id"], "to_id": item["to_id"],
                "type": item["type"], "explanation": item["explanation"],
                "confidence": item["confidence"],
                "a_idea": item["from_idea"], "a_quote": item["from_quote"],
                "a_chapter": item["from_chapter"],
                "a_chapter_title": item["from_chapter_title"],
                "b_idea": item["to_idea"], "b_quote": item["to_quote"],
                "b_chapter": item["to_chapter"],
                "b_chapter_title": item["to_chapter_title"],
            }
        item["explanation"] = re.sub(r"\s+", " ", item["explanation"] or "").strip()
        links.append(item)
    return links


def load_key_ideas(book_id: str) -> list[dict]:
    from server import connect

    with connect() as db:
        rows = db.execute(
            "SELECT k.rank, k.chapter, c.title AS chapter_title, k.idea, k.quote"
            " FROM key_ideas k"
            " LEFT JOIN chapters c ON c.book_id = k.book_id AND c.idx = k.chapter"
            " WHERE k.book_id = ? ORDER BY k.rank", (book_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def idf_of(ideas: list[dict]) -> dict[str, float]:
    """Чем реже слово встречается в ключевых идеях, тем оно важнее."""
    seen: dict[str, int] = {}
    for idea in ideas:
        for token in set(idea["_words"]):
            seen[token] = seen.get(token, 0) + 1
    total = len(ideas)
    return {token: math.log((total + 1) / (count + 1)) + 1 for token, count in seen.items()}


def share_found(left: list[str], right: list[str], idf: dict[str, float]) -> float:
    """Доля веса слов левой идеи, которая нашлась в правой (0…1)."""
    weight = sum(idf.get(token, 1.0) for token in left)
    if not weight:
        return 0.0
    found = sum(idf.get(token, 1.0) for token in left
                if any(similar(token, other) for other in right))
    return found / weight


def only_here(own: list[dict], other: list[dict]) -> list[dict]:
    """Ключевые идеи, которым нет похожей в другой книге."""
    idf = idf_of(own + other)
    lonely = []
    for idea in own:
        best = max((share_found(idea["_words"], o["_words"], idf) for o in other),
                   default=0.0)
        if best < SIMILAR_SHARE:
            lonely.append({**idea, "own_share": round(1 - best, 2)})
    lonely.sort(key=lambda item: (-item["own_share"], item["rank"]))
    return [{k: v for k, v in idea.items() if k != "_words"}
            for idea in lonely[:MAX_ONLY]]


def build_groups(book_a: str, book_b: str) -> dict:
    links = load_links(book_a, book_b)
    agree = [link for link in links if link["type"] in AGREE_TYPES][:MAX_LINKS]
    disagree = [link for link in links if link["type"] in DISAGREE_TYPES][:MAX_LINKS]

    keys_a = [dict(idea, _words=words(idea["idea"])) for idea in load_key_ideas(book_a)]
    keys_b = [dict(idea, _words=words(idea["idea"])) for idea in load_key_ideas(book_b)]
    return {
        "agree": agree,
        "disagree": disagree,
        "only_a": only_here(keys_a, keys_b),
        "only_b": only_here(keys_b, keys_a),
    }


def link_line(link: dict) -> str:
    return (f"- «{link['a_quote']}» ({link['a_chapter_title'] or '—'}) — "
            f"«{link['b_quote']}» ({link['b_chapter_title'] or '—'}): {link['explanation']}")


def ask_verdict(book_a: dict, book_b: dict, groups: dict) -> str:
    if not groups["agree"] and not groups["disagree"]:
        return ("Между этими книгами пока нет связей: модели не нашли общих мест, "
                "поэтому сравнивать нечего. Попробуйте другую пару книг.")

    agree = "\n".join(link_line(link) for link in groups["agree"]) or "— общих мест не найдено."
    disagree = "\n".join(link_line(link) for link in groups["disagree"]) or "— прямых противоречий нет."
    prompt = PROMPT.format(
        title_a=book_a["title"], author_a=book_a["author"] or "автор неизвестен",
        title_b=book_b["title"], author_b=book_b["author"] or "автор неизвестен",
        agree=agree, disagree=disagree,
    )
    raw = chat([{"role": "user", "content": prompt}], max_tokens=700)
    return parse_verdict(raw)


def parse_verdict(raw: str) -> str:
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and str(data.get("verdict") or "").strip():
            raw = str(data["verdict"])
    text = re.sub(r"^[\s\-•#>]+", "", raw.strip())
    text = re.sub(r"\[(\d+)\]", "", text)
    return re.sub(r"\s+", " ", text).strip()[:MAX_VERDICT]


def read_cache(book_a: str, book_b: str) -> dict | None:
    from server import connect

    with connect() as db:
        row = db.execute(
            "SELECT book_a, groups, verdict, created FROM debates"
            " WHERE (book_a = ? AND book_b = ?) OR (book_a = ? AND book_b = ?)",
            (book_a, book_b, book_b, book_a),
        ).fetchone()
    if row is None:
        return None
    groups = json.loads(row["groups"])
    if row["book_a"] != book_a:  # в базе пара лежит в обратном порядке — меняем стороны
        groups = {
            "agree": [swap_sides(link) for link in groups["agree"]],
            "disagree": [swap_sides(link) for link in groups["disagree"]],
            "only_a": groups["only_b"],
            "only_b": groups["only_a"],
        }
    return {"groups": groups, "verdict": row["verdict"], "created": row["created"]}


def swap_sides(link: dict) -> dict:
    swapped = dict(link)
    for left, right in (("a_idea", "b_idea"), ("a_quote", "b_quote"),
                        ("a_chapter", "b_chapter"),
                        ("a_chapter_title", "b_chapter_title"),
                        ("from_id", "to_id")):
        swapped[left], swapped[right] = link.get(right), link.get(left)
    return swapped


def write_cache(book_a: str, book_b: str, groups: dict, verdict: str) -> str:
    from server import connect

    created = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with connect() as db:
        db.execute("DELETE FROM debates WHERE book_a = ? AND book_b = ?", (book_b, book_a))
        db.execute(
            "INSERT INTO debates (book_a, book_b, groups, verdict, created)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(book_a, book_b) DO UPDATE SET groups = excluded.groups,"
            " verdict = excluded.verdict, created = excluded.created",
            (book_a, book_b, json.dumps(groups, ensure_ascii=False), verdict, created),
        )
    return created


def get_debate(book_a: str, book_b: str, refresh: bool = False) -> dict | None:
    """Сравнение двух книг из кэша или заново. None — если книги нет."""
    books = load_books(book_a, book_b)
    if books is None:
        return None
    found_a, found_b = books

    cached = None if refresh else read_cache(book_a, book_b)
    if cached:
        groups, verdict, created = cached["groups"], cached["verdict"], cached["created"]
    else:
        groups = build_groups(book_a, book_b)
        verdict = ask_verdict(found_a, found_b, groups)
        created = write_cache(book_a, book_b, groups, verdict)

    return {
        "book_a": found_a,
        "book_b": found_b,
        "groups": groups,
        "verdict": verdict,
        "created": created,
        "cached": cached is not None,
    }


def main() -> None:
    if len(sys.argv) < 3:
        print("Запуск: python3 app/debate.py <id книги А> <id книги Б>", file=sys.stderr)
        raise SystemExit(2)
    result = get_debate(sys.argv[1], sys.argv[2], refresh=True)
    if result is None:
        print("Книга не найдена.", file=sys.stderr)
        raise SystemExit(1)
    print(f"«{result['book_a']['title']}» ↔ «{result['book_b']['title']}»")
    for key, title in (("agree", "Согласны"), ("disagree", "Спорят"),
                       ("only_a", "Только у А"), ("only_b", "Только у Б")):
        print(f"\n{title}: {len(result['groups'][key])}")
        for item in result["groups"][key]:
            print("  - " + (item.get("a_idea") or item.get("idea"))[:100])
    print("\nРезюме:\n" + result["verdict"])


if __name__ == "__main__":
    main()
