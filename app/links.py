"""Связи идей одной книги с идеями из других книг.

Сравнивать все идеи со всеми дорого, поэтому сначала ищем кандидатов по
общим словам: у каждой идеи остаются три идеи из других книг с наибольшим
числом общих слов. Только эти пары уходят модели — она называет тип связи
(поддерживает / противоречит / развивает / пример / то же самое), объясняет
её в 1–2 предложениях и ставит уверенность от 0 до 1. Сохраняются связи
с уверенностью не ниже 0.6. Старые связи книги при запуске заменяются.

Запуск: python3 app/links.py <book_id|all>
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone

from llm import chat
from server import connect, get_book, init_db, list_books

TOP_CANDIDATES = 3   # сколько идей из других книг проверять у одной идеи
BATCH_IDEAS = 6      # сколько идей-источников в одном запросе
VERIFY_BATCH = 8     # сколько связей «противоречит» проверять за один запрос
MIN_CONFIDENCE = 0.75  # ниже — связь не сохраняем
MAX_LINKS_PER_IDEA = 3  # не больше трёх лучших связей у одной идеи

TYPES = {
    "supports": "поддерживает",
    "contradicts": "противоречит",
    "extends": "развивает",
    "example": "пример к",
    "same": "то же самое",
}

STOP = set("""это что как для или при над под без через между чтобы когда если так все всё еще ещё уже
только очень быть есть был была были будет можно нужно надо этот эта эти того тому чем тем такой такая
такие своих своей своего люди человек книга автор жизни время может который которая которое которые себя
свою свой они она оно его ее её их мы вы он но не ни да нет же ли бы то там тут где чего почему потому
также тоже более менее самый самая самое многих многие каждый каждая каждое другая другой другие другое
новая новый новые целое целая целый лишь даже ведь вот""".split())

PROMPT = """Ты ищешь связи между идеями из разных книг.

Идеи книги «{title}»:
{items}

Возможные пары — идеи из других книг:
{candidates}

Для каждой пары «источник-кандидат» реши, есть ли содержательная связь:
- supports — идея из другой книги подтверждает мысль источника;
- contradicts — ТОЛЬКО если авторы утверждают противоположное по одному и тому же
  вопросу: одно утверждает то, что другое отрицает. Разные темы, разный контекст,
  другой оттенок мысли — это НЕ противоречие;
- extends — развивает, углубляет её;
- example — служит примером к ней;
- same — говорит то же самое.
Если связи нет, пару не включай.

Будь строгим: связь должна быть очевидной, если прочитать обе идеи рядом.
Если сомневаешься — пару не включай. Не больше одной связи из каждого источника.

Оценивай уверенность по одной шкале:
- 0.6 — слабая тематическая перекличка: общая тема, но общей мысли нет;
- 0.8 — явная связь: идеи действительно подтверждают, развивают или
  иллюстрируют друг друга;
- 0.95 — почти одна и та же мысль, сформулированная по-разному.
Ставь промежуточное значение, если связь между этими точками.
В список включай только пары с уверенностью не ниже 0.75.

Ответь только JSON-массивом, без другого текста:
[{{"pair": "S1-C1", "type": "supports", "explanation": "1–2 предложения по-русски", "confidence": 0.8}}]

В explanation не упоминай номера S1 и C1 — пиши сразу про сами идеи."""

VERIFY_PROMPT = """Ты проверяешь связи типа «противоречит» между идеями из разных книг.

Пары:
{pairs}

Для каждой пары сначала сформулируй question — ОДИН общий вопрос, на который
отвечают обе идеи. Затем реши: говорит ли одна идея «да», а другая «нет»
на этот же вопрос?
- opposite: true — только если общий вопрос действительно один и ответы
  прямо противоположны (одно утверждает X, другое — не-X);
- opposite: false — если идеи отвечают на разные вопросы, говорят о разных
  сторонах одного явления, разный контекст или общая только тема.
- Разная оценка одного и того же (один хвалит, другой ругает) — это НЕ прямое
  противоречие, если они не утверждают взаимоисключающее.
- Если сомневаешься — false.
Если opposite=false, назови настоящий тип связи: supports (подтверждает),
extends (развивает), example (пример к), same (то же самое) или none
(связи нет — пару надо удалить).
why — 1–2 предложения по-русски: почему да или нет; если нет, объясни,
в чём настоящая связь.

Уверенность — по той же шкале: 0.6 — слабая тематическая перекличка,
0.8 — явная связь, 0.95 — почти одна и та же мысль.

Ответь только JSON-массивом, без другого текста:
[{{"pair": "P1", "question": "общий вопрос", "opposite": true, "why": "1–2 предложения", "type": "contradicts", "confidence": 0.8}}]"""


def word_keys(text: str) -> set[str]:
    """Ключевые слова для дешёвого поиска кандидатов: основы слов от 5 букв."""
    return {word[:5] for word in re.findall(r"[а-яёa-z]{5,}", text.lower()) if word not in STOP}


def pick_candidates(source: dict, others: list[dict]) -> list[dict]:
    """Три идеи из других книг с наибольшим числом общих слов."""
    keys = word_keys(source["idea"])
    scored = []
    for other in others:
        score = len(keys & word_keys(other["idea"]))
        if score:
            scored.append((score, other))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [other for _, other in scored[:TOP_CANDIDATES]]


def make_batches(pairs: list[tuple[dict, list[dict]]]) -> list[list[tuple[dict, list[dict]]]]:
    return [pairs[start:start + BATCH_IDEAS] for start in range(0, len(pairs), BATCH_IDEAS)]


def build_prompt(title: str, batch: list[tuple[dict, list[dict]]]) -> tuple[str, dict]:
    """Текст запроса и ярлыки пар: «S1» — источник, «C3» — кандидат."""
    labels: dict[str, int] = {}
    items, candidate_lines = [], []
    cand_number = 0
    for source_number, (source, candidates) in enumerate(batch, 1):
        labels[f"S{source_number}"] = source["id"]
        items.append(f"S{source_number}. {source['idea']}")
        for other in candidates:
            cand_number += 1
            labels[f"C{cand_number}"] = other["id"]
            candidate_lines.append(f"C{cand_number}. «{other['book_title']}»: {other['idea']}")
    prompt = PROMPT.format(
        title=title,
        items="\n".join(items),
        candidates="\n".join(candidate_lines),
    )
    return prompt, labels


def build_verify_prompt(batch: list[dict], ideas: dict[int, str]) -> tuple[str, dict[str, tuple[int, int]]]:
    """Текст второго запроса и ярлыки проверяемых связей."""
    labels: dict[str, tuple[int, int]] = {}
    lines = []
    for number, link in enumerate(batch, 1):
        pair = (link["from_id"], link["to_id"])
        labels[f"P{number}"] = pair
        lines.append(
            f"P{number}.\n"
            f"Источник: {ideas[link['from_id']]}\n"
            f"Кандидат: {ideas[link['to_id']]}\n"
            f"Почему модель считает это противоречием: {link['explanation']}"
        )
    return VERIFY_PROMPT.format(pairs="\n\n".join(lines)), labels


def parse_verdicts(answer: str, labels: dict[str, tuple[int, int]]) -> dict[tuple[int, int], dict]:
    """Достаёт из ответа модели решения по парам «противоречит»."""
    start, end = answer.find("["), answer.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("В ответе модели нет JSON-массива:\n" + answer[:400])
    data = json.loads(answer[start:end + 1])
    if not isinstance(data, list):
        raise ValueError("Модель вернула не список решений.")
    verdicts: dict[tuple[int, int], dict] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        match = re.fullmatch(r"\s*P(\d+)\s*", str(item.get("pair") or ""))
        if not match:
            continue
        pair = labels.get(f"P{match.group(1)}")
        if pair is None:
            continue
        opposite = item.get("opposite")
        if isinstance(opposite, str):
            opposite = opposite.strip().lower() in {"true", "да", "yes", "1"}
        if not isinstance(opposite, bool):
            continue
        kind = str(item.get("type") or "").strip().lower()
        why = str(item.get("why") or "").strip()
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            continue
        if not why:
            continue
        verdicts[pair] = {
            "opposite": opposite,
            "type": kind,
            "why": why,
            "confidence": round(min(max(confidence, 0.0), 1.0), 2),
        }
    return verdicts


def verify_contradictions(links: list[dict]) -> list[dict]:
    """Второй запрос для «противоречит»: правда ли авторы говорят обратное.

    Если модель отвечает «нет», связь удаляется: тип не меняем, иначе
    отклонённые противоречия оседают в «развивает» и раздувают его.
    """
    targets = [link for link in links if link["type"] == "contradicts"]
    if not targets:
        return links
    with connect() as db:
        ideas = {row["id"]: row["idea"] for row in db.execute("SELECT id, idea FROM ideas")}
    verdicts: dict[tuple[int, int], dict] = {}
    batches = [targets[start:start + VERIFY_BATCH] for start in range(0, len(targets), VERIFY_BATCH)]
    print(f"Проверяю «противоречит»: {len(targets)} связей, запросов {len(batches)}", flush=True)
    for number, batch in enumerate(batches, 1):
        prompt, labels = build_verify_prompt(batch, ideas)
        found = parse_verdicts(chat([{"role": "user", "content": prompt}]), labels)
        verdicts.update(found)
        print(f"  проверка {number} из {len(batches)}: решений {len(found)}", flush=True)

    result: list[dict] = []
    kept = deleted = 0
    for link in links:
        pair = (link["from_id"], link["to_id"])
        verdict = verdicts.get(pair)
        if link["type"] != "contradicts" or verdict is None:
            result.append(link)
            continue
        confidence = verdict["confidence"]
        if confidence < MIN_CONFIDENCE or not verdict["opposite"]:
            deleted += 1
            continue
        result.append({**link, "explanation": verdict["why"], "confidence": confidence})
        kept += 1
    print(f"«Противоречит»: осталось {kept}, удалено {deleted}", flush=True)
    return result


def parse_links(answer: str, labels: dict[str, int]) -> list[dict]:
    """Достаёт из ответа модели связи и проверяет их поля."""
    start, end = answer.find("["), answer.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("В ответе модели нет JSON-массива:\n" + answer[:400])
    data = json.loads(answer[start:end + 1])
    if not isinstance(data, list):
        raise ValueError("Модель вернула не список связей.")
    links = []
    for item in data:
        if not isinstance(item, dict):
            continue
        match = re.fullmatch(r"\s*S(\d+)\s*[-–]\s*C(\d+)\s*", str(item.get("pair") or ""))
        if not match:
            continue
        from_id = labels.get(f"S{match.group(1)}")
        to_id = labels.get(f"C{match.group(2)}")
        kind = str(item.get("type") or "").strip().lower()
        explanation = str(item.get("explanation") or "").strip()
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            continue
        if from_id is None or to_id is None or kind not in TYPES or not explanation:
            continue
        if confidence < MIN_CONFIDENCE:
            continue
        links.append({
            "from_id": from_id,
            "to_id": to_id,
            "type": kind,
            "explanation": explanation,
            "confidence": round(min(confidence, 1.0), 2),
        })
    return links


def save_links(book_id: str, links: list[dict]) -> None:
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as db:
        db.execute(
            "DELETE FROM links WHERE from_id IN (SELECT id FROM ideas WHERE book_id = ?)",
            (book_id,),
        )
        db.executemany(
            "INSERT OR IGNORE INTO links (from_id, to_id, type, explanation, confidence, created)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(link["from_id"], link["to_id"], link["type"], link["explanation"],
              link["confidence"], created) for link in links],
        )


def trim_links(max_per_idea: int = MAX_LINKS_PER_IDEA) -> int:
    """Оставляет лучшие связи: одна пара не дважды, не больше трёх связей у идеи."""
    with connect() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT from_id, to_id, type, explanation, confidence, created FROM links"
        ).fetchall()]
        rows.sort(key=lambda link: link["confidence"], reverse=True)
        unique: dict[tuple[int, int], dict] = {}
        for link in rows:
            unique.setdefault(tuple(sorted((link["from_id"], link["to_id"]))), link)
        kept: list[dict] = []
        used: dict[int, int] = {}
        for link in unique.values():
            ideas = (link["from_id"], link["to_id"])
            if any(used.get(idea, 0) >= max_per_idea for idea in ideas):
                continue
            kept.append(link)
            for idea in ideas:
                used[idea] = used.get(idea, 0) + 1
        db.execute("DELETE FROM links")
        db.executemany(
            "INSERT INTO links (from_id, to_id, type, explanation, confidence, created)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(link["from_id"], link["to_id"], link["type"], link["explanation"],
              link["confidence"], link["created"]) for link in kept],
        )
    print(f"После отбора: {len(kept)} связей (не больше {max_per_idea} у идеи)", flush=True)
    return len(kept)


def load_ideas(book_id: str) -> tuple[list[dict], list[dict]]:
    with connect() as db:
        mine = [dict(row) for row in db.execute(
            "SELECT id, idea FROM ideas WHERE book_id = ? ORDER BY id", (book_id,)
        ).fetchall()]
        others = [dict(row) for row in db.execute(
            "SELECT i.id, i.idea, b.title AS book_title FROM ideas i"
            " JOIN books b ON b.id = i.book_id WHERE i.book_id != ? ORDER BY i.id",
            (book_id,),
        ).fetchall()]
    return mine, others


def links_for(book_id: str) -> int:
    book = get_book(book_id)
    if book is None:
        print(f"Книга {book_id} не найдена.", file=sys.stderr)
        return 0
    if len(list_books()) < 2:
        print("В библиотеке одна книга — сравнивать не с чем.", file=sys.stderr)
        return 0
    mine, others = load_ideas(book_id)
    pairs = []
    for source in mine:
        candidates = pick_candidates(source, others)
        if candidates:
            pairs.append((source, candidates))
    print(f"Книга «{book['title']}»: идей {len(mine)}, с кандидатами {len(pairs)}", flush=True)

    links: list[dict] = []
    batches = make_batches(pairs)
    for number, batch in enumerate(batches, 1):
        prompt, labels = build_prompt(book["title"], batch)
        found = parse_links(chat([{"role": "user", "content": prompt}]), labels)
        links.extend(found)
        print(f"  запрос {number} из {len(batches)}: связей {len(found)}", flush=True)

    unique = {(link["from_id"], link["to_id"]): link for link in links}
    checked = verify_contradictions(list(unique.values()))
    unique = {(link["from_id"], link["to_id"]): link for link in checked}
    save_links(book_id, list(unique.values()))
    print(f"Сохранено связей: {len(unique)}", flush=True)
    for link in list(unique.values())[:3]:
        print(f"- {TYPES[link['type']]} ({link['confidence']}): {link['explanation'][:160]}", flush=True)
    return len(unique)


def print_distribution(label: str = "Итог") -> None:
    """Показывает, сколько связей каждого типа и как легли уверенности."""
    with connect() as db:
        total = db.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        by_type = db.execute(
            "SELECT type, COUNT(*) AS n FROM links GROUP BY type ORDER BY n DESC"
        ).fetchall()
        by_conf = db.execute(
            "SELECT confidence, COUNT(*) AS n FROM links GROUP BY confidence ORDER BY confidence"
        ).fetchall()
        low, high = db.execute("SELECT MIN(confidence), MAX(confidence) FROM links").fetchone()
    if not total:
        print(f"{label}: связей нет", flush=True)
        return
    print(f"\n{label}: связей {total}", flush=True)
    print("  по типам: " + ", ".join(
        f"{TYPES[row['type']]} {row['n']} ({row['n'] * 100 / total:.0f}%)" for row in by_type
    ), flush=True)
    print("  уверенность: " + ", ".join(
        f"{row['confidence']:.2f} — {row['n']}" for row in by_conf
    ), flush=True)
    buckets = [(0.75, 0.79), (0.80, 0.89), (0.90, 1.00)]
    print("  по диапазонам: " + ", ".join(
        f"{lo:.2f}–{hi:.2f}: "
        f"{sum(row['n'] for row in by_conf if lo <= row['confidence'] <= hi)}"
        for lo, hi in buckets
    ), flush=True)
    print(f"  минимум {low:.2f}, максимум {high:.2f}", flush=True)


def main() -> None:
    if len(sys.argv) != 2:
        print("Запуск: python3 app/links.py <book_id|all>", file=sys.stderr)
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
        total += links_for(book_id)
    left = trim_links()
    if where == "all":
        print(f"\nИтого связей: {total}, после отбора: {left}", flush=True)
    print_distribution()


if __name__ == "__main__":
    main()
