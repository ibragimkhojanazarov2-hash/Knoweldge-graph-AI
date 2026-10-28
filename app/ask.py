"""«Спросить библиотеку»: поиск идей по вопросу и ответ модели со сносками.

Поиск — на стандартной библиотеке Python: вопрос и идеи режутся на слова,
у слов отсекаются окончания, редкие слова весят больше частых (IDF).
Модель получает 10–15 лучших фрагментов и отвечает только по ним,
со сносками [1], [2] на номера фрагментов.

Запуск для проверки: python3 app/ask.py "в чём смысл свободы?"
"""
from __future__ import annotations

import json
import math
import re
import sys

from llm import chat

MAX_SOURCES = 15      # сколько фрагментов даём модели
MIN_SCORE_SHARE = 0.18  # слабее этой доли от лучшего результата не берём
MAX_QUESTION = 500

WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
SPACES = re.compile(r"\s+")

# Служебные слова: на вопрос они не отвечают, только шумят в поиске.
STOP = set("""
и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по
только ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если
уже или ни быть был него до вас нибудь опять уж вам ведь там потом себя ничего ей
может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз
тоже себе под будет тогда кто этот того потому этого какой совсем ним здесь этом
один почти мой тем чтобы нее сейчас были куда зачем всех никогда можно при наконец
два об другой хоть после над больше тот через эти нас про всего них какая много
разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой
им более всегда конечно всю между это
the a an and or of to in on for with without about into over after before is are was
were be been being do does did how what why when where who whom which that this these
those it its as at by from not no but if then than so such can could should would may
might will
""".split())

# Русские окончания — от длинных к коротким, чтобы «иями» ушло раньше «и».
ENDINGS = tuple(sorted({
    "иями", "ями", "ами", "ыми", "ими", "ов", "ев", "ий", "ый", "ая", "ое",
    "ые", "ей", "ой", "ам", "ям", "ах", "ях", "ом", "ем", "а", "я", "ы", "и",
    "у", "ю", "е", "о", "ь",
}, key=len, reverse=True))

ENGLISH = tuple(sorted({"ing", "ed", "es", "s"}, key=len, reverse=True))

FALLBACK_SUGGESTIONS = [
    "О чём говорят книги этой библиотеки?",
    "Как авторы понимают счастье?",
    "Что общего у разных книг?",
]

PROMPT = """Ты помогаешь читателю библиотеки Marginalia. Отвечай на вопрос только по фрагментам ниже.

Вопрос: {question}

Фрагменты из книг:
{fragments}

Правила:
- Используй только эти фрагменты. Не добавляй знаний от себя и не придумывай цитат.
- Каждое утверждение подкрепляй сноской [1], [2] — это номер фрагмента.
- Если фрагменты не отвечают на вопрос, честно скажи: «В книгах библиотеки об этом прямо не говорится» — и коротко объясни, о чём они.
- Пиши по-русски, 3–6 предложений, без заголовков и списков.
- Предложи ровно 3 коротких вопроса, на которые эта библиотека может ответить дальше.

Ответь только JSON-объектом, без другого текста:
{{"answer": "...", "suggestions": ["...", "...", "..."]}}"""


def stem(word: str) -> str:
    """Нижний регистр, ё → е и отсечение частого окончания."""
    word = word.lower().replace("ё", "е")
    if word.isascii():
        for end in ENGLISH:
            if word.endswith(end) and len(word) - len(end) >= 3:
                return word[:-len(end)]
        return word
    for end in ENDINGS:
        if word.endswith(end) and len(word) - len(end) >= 4:
            return word[:-len(end)]
    return word


def words(text: str) -> list[str]:
    """Значимые слова текста, уже без окончаний."""
    return [stem(w) for w in WORD.findall(text or "") if len(w) > 2 and w.lower() not in STOP]


def similar(a: str, b: str) -> bool:
    """Одно и то же слово в разных формах: «свобод» и «свобода»."""
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 4 and long.startswith(short)


def squeeze(text: str) -> str:
    """Текст для поиска фразы: нижний регистр, ё → е, одиночные пробелы."""
    return SPACES.sub(" ", (text or "").lower().replace("ё", "е")).strip()


def load_ideas() -> list[dict]:
    from server import connect  # server импортирует этот модуль, поэтому импорт здесь

    with connect() as db:
        rows = db.execute(
            "SELECT i.id, i.book_id, i.chapter, i.idea, i.quote, i.why,"
            " b.title AS book_title, b.author, c.title AS chapter_title"
            " FROM ideas i"
            " JOIN books b ON b.id = i.book_id"
            " LEFT JOIN chapters c ON c.book_id = i.book_id AND c.idx = i.chapter"
            " ORDER BY i.id"
        ).fetchall()
    return [dict(row) for row in rows]


def index_idea(row: dict) -> dict:
    """Слова идеи по полям: разные поля весят по-разному при поиске."""
    return {
        "idea": words(row["idea"]),
        "quote": words(row["quote"]),
        "why": words(row["why"]),
        "meta": words(" ".join(str(row.get(k) or "") for k in
                               ("book_title", "author", "chapter_title"))),
    }


def search(question: str, limit: int = MAX_SOURCES) -> list[dict]:
    """10–15 идей и цитат, лучше всего подходящих к вопросу."""
    asked = list(dict.fromkeys(words(question)))
    if not asked:
        return []

    rows = load_ideas()
    if not rows:
        return []
    docs = [index_idea(row) for row in rows]

    # Сколько идей содержат слово: чем реже слово, тем оно важнее.
    seen: dict[str, int] = {}
    for doc in docs:
        for field in doc.values():
            for token in set(field):
                seen[token] = seen.get(token, 0) + 1
    total = len(docs)

    def idf(token: str) -> float:
        return math.log((total + 1) / (seen.get(token, 0) + 1)) + 1

    phrase = squeeze(question)
    field_weight = {"idea": 3.0, "quote": 1.0, "why": 1.0, "meta": 0.4}
    scored: list[dict] = []
    for row, doc in zip(rows, docs):
        score = 0.0
        for token in asked:
            weight = 0.0
            for field, tokens in doc.items():
                if any(similar(token, t) for t in tokens):
                    weight = max(weight, field_weight[field])
            if weight:
                score += weight * idf(token)
        if len(phrase) >= 10 and phrase in squeeze(row["idea"] + " " + row["quote"]):
            score += 5.0
        if score <= 0:
            continue
        scored.append({**row, "score": round(score, 2)})

    if not scored:
        return []
    scored.sort(key=lambda item: (-item["score"], item["id"]))
    best = scored[0]["score"]
    return [item for item in scored if item["score"] >= best * MIN_SCORE_SHARE][:limit]


def fragments(sources: list[dict]) -> str:
    parts = []
    for n, source in enumerate(sources, 1):
        parts.append(
            f"[{n}] Книга «{source['book_title']}», глава «{source['chapter_title'] or '—'}»\n"
            f"Идея: {source['idea']}\n"
            f"Цитата: «{source['quote']}»"
        )
    return "\n\n".join(parts)


def clean_answer(answer: str, count: int) -> str:
    """Убирает сноски на фрагменты, которых нет: модели иногда придумывают [9]."""
    def fix(match: re.Match) -> str:
        number = int(match.group(1))
        return match.group(0) if 1 <= number <= count else ""

    text = re.sub(r"\[(\d+)\]", fix, answer)
    return SPACES.sub(" ", text).strip()


def parse_model(answer: str, count: int) -> tuple[str, list[str]]:
    """Достаёт answer и подсказки из JSON модели; иначе берёт ответ как текст."""
    start, end = answer.find("{"), answer.rfind("}")
    if start != -1 and end > start:
        try:
            data = json.loads(answer[start:end + 1])
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            text = str(data.get("answer") or "").strip()
            hints = [re.sub(r"^[\s\-•]+", "", str(h)).strip()
                     for h in data.get("suggestions") or []]
            hints = [h for h in dict.fromkeys(hints) if 3 <= len(h) <= 160][:3]
            if text:
                return clean_answer(text, count), hints
    return clean_answer(answer.strip(), count), []


def answer_question(question: str) -> dict:
    """Ответ на вопрос читателя: найденные фрагменты + текст модели со сносками."""
    question = SPACES.sub(" ", question or "").strip()[:MAX_QUESTION]
    sources = search(question)

    if not sources:
        return {
            "answer": "В книгах библиотеки об этом прямо не говорится: я не нашёл "
                      "подходящих идей. Попробуйте спросить иначе или добавить книгу.",
            "sources": [],
            "suggestions": FALLBACK_SUGGESTIONS,
        }

    prompt = PROMPT.format(question=question, fragments=fragments(sources))
    raw = chat([{"role": "user", "content": prompt}], max_tokens=900)
    text, hints = parse_model(raw, len(sources))
    if len(hints) < 3:
        hints = (hints + [h for h in FALLBACK_SUGGESTIONS if h not in hints])[:3]

    return {
        "answer": text,
        "sources": [{**source, "n": n} for n, source in enumerate(sources, 1)],
        "suggestions": hints,
    }


def main() -> None:
    if len(sys.argv) < 2:
        print('Запуск: python3 app/ask.py "вопрос"', file=sys.stderr)
        raise SystemExit(2)
    question = " ".join(sys.argv[1:])
    found = search(question)
    print(f"Вопрос: {question}\nНайдено фрагментов: {len(found)}", flush=True)
    for n, source in enumerate(found, 1):
        print(f"[{n}] {source['score']} · «{source['book_title']}» · {source['idea'][:90]}")

    result = answer_question(question)
    print("\nОтвет:\n" + result["answer"])
    print("\nДальше:")
    for hint in result["suggestions"]:
        print("- " + hint)


if __name__ == "__main__":
    main()
