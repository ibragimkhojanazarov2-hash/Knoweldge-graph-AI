"""Строгая перепроверка типов уже найденных связей.

Отклонённое противоречие раньше превращалось в «развивает», и этот тип
раздувался. Здесь модель для каждой связи отвечает, верен ли заявленный тип;
если нет — называет настоящий или говорит, что связи нет. Связи с уверенностью
ниже 0.75 удаляются.

Запуск: python3 app/recheck_links.py [--type extends]
"""
from __future__ import annotations

import argparse
import json
import re

from llm import chat
from links import MIN_CONFIDENCE, TYPES, print_distribution
from server import connect

BATCH = 8

PROMPT = """Ты строго проверяешь уже найденные связи между идеями из разных книг.

Пары:
{pairs}

Для каждой пары реши, верен ли заявленный тип:
- supports — кандидат подтверждает мысль источника;
- contradicts — авторы утверждают противоположное по ОДНОМУ и тому же вопросу;
- extends — кандидат добавляет новую мысль, которой у источника нет;
- example — кандидат служит примером к мысли источника;
- same — кандидат говорит то же самое другими словами.

Общая тема без общей мысли — это none: содержательной связи нет.
Если заявленный тип верен, поставь ok: true. Если нет — ok: false и настоящий
тип (или none). Если сомневаешься — ok: false, type: none.

Уверенность: 0.6 — слабая тематическая перекличка, 0.8 — явная связь,
0.95 — почти одна и та же мысль.

Ответь только JSON-массивом, без другого текста:
[{{"pair": "P1", "ok": false, "type": "extends", "why": "1–2 предложения", "confidence": 0.8}}]"""


def load_links(kind: str | None) -> list[dict]:
    with connect() as db:
        query = (
            "SELECT from_id, to_id, type, explanation, confidence FROM links"
        )
        args: tuple[str, ...] = ()
        if kind:
            query += " WHERE type = ?"
            args = (kind,)
        return [dict(row) for row in db.execute(query + " ORDER BY id", args).fetchall()]


def build_prompt(batch: list[dict], ideas: dict[int, str]) -> tuple[str, dict[str, tuple[int, int]]]:
    labels: dict[str, tuple[int, int]] = {}
    lines = []
    for number, link in enumerate(batch, 1):
        pair = (link["from_id"], link["to_id"])
        labels[f"P{number}"] = pair
        lines.append(
            f"P{number}. Заявленный тип: {TYPES.get(link['type'], link['type'])}\n"
            f"Источник: {ideas[link['from_id']]}\n"
            f"Кандидат: {ideas[link['to_id']]}\n"
            f"Объяснение модели: {link['explanation']}"
        )
    return PROMPT.format(pairs="\n\n".join(lines)), labels


def parse(answer: str, labels: dict[str, tuple[int, int]]) -> dict[tuple[int, int], dict]:
    start, end = answer.find("["), answer.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("В ответе модели нет JSON-массива:\n" + answer[:400])
    data = json.loads(answer[start:end + 1])
    verdicts: dict[tuple[int, int], dict] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        match = re.fullmatch(r"\s*P(\d+)\s*", str(item.get("pair") or ""))
        pair = labels.get(f"P{match.group(1)}") if match else None
        if pair is None:
            continue
        try:
            confidence = round(min(max(float(item.get("confidence")), 0.0), 1.0), 2)
        except (TypeError, ValueError):
            continue
        verdicts[pair] = {
            "ok": item.get("ok") is True,
            "type": str(item.get("type") or "").strip().lower(),
            "why": str(item.get("why") or "").strip(),
            "confidence": confidence,
        }
    return verdicts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=sorted(TYPES), help="проверять только этот тип")
    args = parser.parse_args()

    links = load_links(args.type)
    if not links:
        print("Связей для проверки нет.")
        return
    with connect() as db:
        ideas = {row["id"]: row["idea"] for row in db.execute("SELECT id, idea FROM ideas")}
    batches = [links[start:start + BATCH] for start in range(0, len(links), BATCH)]
    print(f"Проверяю связей: {len(links)}, запросов {len(batches)}", flush=True)

    verdicts: dict[tuple[int, int], dict] = {}
    for number, batch in enumerate(batches, 1):
        prompt, labels = build_prompt(batch, ideas)
        verdicts.update(parse(chat([{"role": "user", "content": prompt}]), labels))
        print(f"  проверка {number} из {len(batches)}", flush=True)

    kept = changed = deleted = 0
    updated: list[dict] = []
    dropped: list[tuple[int, int]] = []
    for link in links:
        pair = (link["from_id"], link["to_id"])
        verdict = verdicts.get(pair)
        if verdict is None:
            kept += 1
            continue
        if verdict["confidence"] < MIN_CONFIDENCE:
            dropped.append(pair)
            deleted += 1
            continue
        if verdict["ok"]:
            updated.append({**link, "confidence": verdict["confidence"]})
            kept += 1
            continue
        kind = verdict["type"]
        if kind not in TYPES or kind == "none" or not verdict["why"]:
            dropped.append(pair)
            deleted += 1
            continue
        updated.append({
            **link, "type": kind, "explanation": verdict["why"],
            "confidence": verdict["confidence"],
        })
        changed += 1

    with connect() as db:
        db.executemany("DELETE FROM links WHERE from_id = ? AND to_id = ?", dropped)
        db.executemany(
            "UPDATE links SET type = ?, explanation = ?, confidence = ?"
            " WHERE from_id = ? AND to_id = ?",
            [(link["type"], link["explanation"], link["confidence"],
              link["from_id"], link["to_id"]) for link in updated],
        )
    print(f"Осталось {kept}, сменили тип {changed}, удалено {deleted}", flush=True)
    print_distribution("После перепроверки")


if __name__ == "__main__":
    main()
