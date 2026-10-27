"""Извлечение идей из главы книги через класс-API чата.

Идея — короткая мысль из главы, у которой есть настоящая цитата из текста.
После ответа модели каждая цитата ищется в тексте главы: пробелы и неразрывные
пробелы не в счёт, остальное должно совпасть почти посимвольно. Цитата
заменяется на настоящий кусок текста книги, а идея без цитаты выбрасывается.

Запуск: python3 app/extract.py <book_id> <chapter>
"""
from __future__ import annotations

import difflib
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

from server import connect, get_chapter, init_db

API = "http://127.0.0.1:19040/v1/chat/completions"
MODEL = "DeepSeek-V4.1-Flash"
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


def chat(messages: list[dict], max_tokens: int = 2000) -> str:
    body = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": messages,
    }
    request = urllib.request.Request(
        API, json.dumps(body).encode(), {"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(exc.read().decode("utf-8", "replace") or str(exc)) from exc
    content = data["choices"][0]["message"]["content"]
    if not content or not content.strip():
        raise RuntimeError("Модель вернула пустой ответ.")
    return content


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


def main() -> None:
    if len(sys.argv) != 3:
        print("Запуск: python3 app/extract.py <book_id> <chapter>", file=sys.stderr)
        raise SystemExit(2)
    book_id, chapter_idx = sys.argv[1], int(sys.argv[2])

    init_db()
    chapter = get_chapter(book_id, chapter_idx)
    if chapter is None:
        print(f"Глава {chapter_idx} книги {book_id} не найдена.", file=sys.stderr)
        raise SystemExit(1)

    chunks = split_text(chapter["text"])
    print(f"Глава «{chapter['title']}»: {len(chapter['text'])} знаков, кусков: {len(chunks)}")

    ideas: list[dict] = []
    dropped = 0
    for number, chunk in enumerate(chunks, 1):
        prompt = PROMPT.format(title=chapter["title"], text=chunk)
        answer = chat([{"role": "user", "content": prompt}])
        found = parse_ideas(answer)
        kept, lost = check_ideas(found, chapter["text"])
        dropped += lost
        print(f"  кусок {number}: идей {len(found)}, с цитатой из книги {len(kept)}")
        ideas.extend(kept)

    save_ideas(book_id, chapter_idx, ideas)
    print(f"Сохранено идей: {len(ideas)}" + (f", без цитаты отброшено: {dropped}" if dropped else ""))
    for idea in ideas[:3]:
        print(f"- {idea['idea']}\n  «{idea['quote'][:120]}»")


if __name__ == "__main__":
    main()
