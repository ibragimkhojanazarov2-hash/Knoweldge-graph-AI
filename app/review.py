"""Повторение: 5–10 карточек в день из ключевых идей и связей.

Три вида карточек:
- idea — цитата, вопрос «Какую мысль автор ею доказывает?»;
- link — две идеи, вопрос «Как связаны эти идеи?»;
- author — идея и вопрос «Кто из авторов не согласился бы с этим?»
  (берём только противоречия, отмеченные читателем как верные, — они впереди).

Ответ «Помню» откладывает карточку на 1, 3, 7, 21 и 60 дней, «Трудно» —
на шаг назад (не меньше чем на день), «Не помню» — на завтра и с начала.

Карточки лежат в таблице reviews: source — "idea:<id>", "link:<id>" или
"author:<id связи>", stage — сколько раз подряд ответили «Помню» (0–5),
due — дата показа.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone

DAILY = 7
STEPS = (1, 3, 7, 21, 60)
PATTERN = ("idea", "link", "author", "idea", "link", "idea", "author")
RATINGS = ("remember", "hard", "forget")


def _db():
    # debate.py делает так же: соединение берём из server.py, чтобы не
    # заводить второй путь к базе.
    from server import connect

    return connect()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _order_key(source: str, day: str) -> str:
    """Стабильный псевдослучайный порядок: одни и те же карточки за день."""
    return hashlib.sha1(f"{source}|{day}".encode("utf-8")).hexdigest()


def _mix(pools: dict[str, list[str]], need: int) -> list[tuple[str, str]]:
    """Берём вперемешку идеи, связи и споры, пока не наберём need."""
    chosen: list[tuple[str, str]] = []
    queues = {kind: list(items) for kind, items in pools.items()}
    while len(chosen) < need and any(queues.values()):
        moved = False
        for kind in PATTERN:
            if len(chosen) >= need:
                break
            if queues.get(kind):
                chosen.append((queues[kind].pop(0), kind))
                moved = True
        if not moved:            # остались только виды вне шаблона
            for kind in sorted(queues):
                while queues[kind] and len(chosen) < need:
                    chosen.append((queues[kind].pop(0), kind))
    return chosen


def _seed(day: date, need: int) -> None:
    """Добавляем в оборот новые ключевые идеи и связи — не больше DAILY в день."""
    with _db() as db:
        idea_rows = db.execute(
            "SELECT 'idea:' || k.id AS source FROM key_ideas k"
            " WHERE NOT EXISTS (SELECT 1 FROM reviews r WHERE r.source = 'idea:' || k.id)"
        ).fetchall()
        link_rows = db.execute(
            "SELECT 'link:' || l.id AS source,"
            "       COALESCE(lm.verdict, '') AS verdict,"
            "       COALESCE(NULLIF(lm.type, ''), l.type) AS type"
            " FROM links l"
            " LEFT JOIN link_marks lm ON lm.from_id = l.from_id AND lm.to_id = l.to_id"
            " WHERE COALESCE(lm.verdict, '') <> 'wrong'"
            "   AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.source = 'link:' || l.id)"
            "   AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.source = 'author:' || l.id)"
        ).fetchall()

    stamp = day.isoformat()
    ideas = sorted((row["source"] for row in idea_rows), key=lambda s: _order_key(s, stamp))
    links, authors = [], []
    for row in link_rows:
        key = _order_key(row["source"], stamp)
        if row["type"] == "contradicts":
            authors.append((0 if row["verdict"] == "right" else 1, key, row["source"]))
        else:
            links.append((0 if row["verdict"] == "right" else 1, key, row["source"]))
    # Подтверждённые читателем связи — первыми, дальше по стабильному порядку.
    links = [item[2] for item in sorted(links)]
    authors = [item[2] for item in sorted(authors)]
    chosen = _mix({"idea": ideas, "link": links, "author": authors}, need)
    if not chosen:
        return

    now = _now()
    with _db() as db:
        db.executemany(
            "INSERT OR IGNORE INTO reviews"
            " (source, kind, stage, due, seen, forgot, answered, created, updated)"
            " VALUES (?, ?, 0, ?, 0, 0, '', ?, ?)",
            [(source, kind, stamp, now, now) for source, kind in chosen],
        )


def _link_row(ref: str) -> dict | None:
    """Связь по id: только не отклонённая читателем, с настоящим типом."""
    with _db() as db:
        row = db.execute(
            "SELECT l.id AS link_id, l.from_id, l.to_id,"
            "       COALESCE(NULLIF(lm.type, ''), l.type) AS link_type,"
            "       l.explanation, l.confidence,"
            "       fi.idea AS from_idea, fi.quote AS from_quote,"
            "       fi.chapter AS from_chapter, fb.title AS from_book_title,"
            "       ti.idea AS to_idea, ti.quote AS to_quote,"
            "       ti.chapter AS to_chapter, tb.title AS to_book_title"
            "  FROM links l"
            "  JOIN ideas fi ON fi.id = l.from_id"
            "  JOIN ideas ti ON ti.id = l.to_id"
            "  JOIN books fb ON fb.id = fi.book_id"
            "  JOIN books tb ON tb.id = ti.book_id"
            "  LEFT JOIN link_marks lm ON lm.from_id = l.from_id AND lm.to_id = l.to_id"
            " WHERE l.id = ? AND COALESCE(lm.verdict, '') <> 'wrong'",
            (int(ref),),
        ).fetchone()
    return dict(row) if row else None


def _payload(source: str) -> dict | None:
    """Всё, что нужно карточке. None — если идея или связь исчезла."""
    kind, _, ref = source.partition(":")
    if not ref.isdigit():
        return None

    if kind == "idea":
        with _db() as db:
            row = db.execute(
                "SELECT k.id AS idea_id, k.idea, k.quote, k.chapter AS chapter_idx,"
                "       b.title AS book_title, c.title AS chapter_title"
                "  FROM key_ideas k"
                "  JOIN books b ON b.id = k.book_id"
                "  LEFT JOIN chapters c ON c.book_id = k.book_id AND c.idx = k.chapter"
                " WHERE k.id = ?",
                (int(ref),),
            ).fetchone()
        if row is None:
            return None
        return {
            "kind": "idea",
            "idea_id": row["idea_id"],
            "idea": row["idea"],
            "quote": row["quote"],
            "chapter": row["chapter_idx"],
            "book_title": row["book_title"],
            "chapter_title": row["chapter_title"] or "",
        }

    if kind in ("link", "author"):
        row = _link_row(ref)
        if row is None:
            return None
        # Для карточки спора идея А — это утверждение, идея Б — тот, кто спорит.
        return {"kind": kind, **row}

    return None


def _collect(day: date, limit: int) -> list[dict]:
    if limit <= 0:
        return []
    with _db() as db:
        rows = db.execute(
            "SELECT id, source, kind, stage FROM reviews WHERE due <= ?"
            " ORDER BY due, id LIMIT ?",
            (day.isoformat(), limit),
        ).fetchall()
    cards, dead = [], []
    for row in rows:
        payload = _payload(row["source"])
        if payload is None:
            dead.append(row["id"])
            continue
        # id карточки и вид берём из reviews: source «link:<id>» бывает и у спора.
        cards.append({**payload, "id": row["id"], "kind": row["kind"],
                      "stage": row["stage"]})
    if dead:
        with _db() as db:
            db.executemany("DELETE FROM reviews WHERE id = ?", [(card_id,) for card_id in dead])
    return cards


def today_cards(day: date | None = None) -> dict:
    """Карточки на сегодня. Уже отвеченные сегодня не повторяем."""
    day = day or date.today()
    stamp = day.isoformat()
    with _db() as db:
        answered = db.execute(
            "SELECT COUNT(*) FROM reviews WHERE answered = ?", (stamp,)
        ).fetchone()[0]

    remaining = max(0, DAILY - answered)
    cards = _collect(day, remaining)
    if len(cards) < remaining:
        _seed(day, remaining - len(cards))
        cards = _collect(day, remaining)

    with _db() as db:
        row = db.execute(
            "SELECT MIN(due) FROM reviews WHERE due > ?", (stamp,)
        ).fetchone()
        next_due = row[0] if row and row[0] else None

    return {
        "date": stamp,
        "cards": cards,
        "total": DAILY,
        "answered_today": answered,
        "next_due": next_due,
    }


def answer_card(card_id: int, rating: str, day: date | None = None) -> dict | None:
    """«Помню» — шаг вперёд, «Трудно» — шаг назад, «Не помню» — завтра, с начала."""
    if rating not in RATINGS:
        rating = "forget"
    day = day or date.today()
    with _db() as db:
        row = db.execute("SELECT stage FROM reviews WHERE id = ?", (card_id,)).fetchone()
        if row is None:
            return None
        stage = int(row["stage"])
        if rating == "remember":
            interval = STEPS[min(stage, len(STEPS) - 1)]
            stage = min(stage + 1, len(STEPS))
        elif rating == "hard":
            stage = max(stage - 1, 0)
            interval = STEPS[stage]
        else:
            interval = STEPS[0]
            stage = 0
        due = day + timedelta(days=interval)
        db.execute(
            "UPDATE reviews SET stage = ?, due = ?, seen = seen + 1,"
            " forgot = forgot + ?, answered = ?, updated = ? WHERE id = ?",
            (stage, due.isoformat(), 1 if rating == "forget" else 0,
             day.isoformat(), _now(), card_id),
        )
        answered = db.execute(
            "SELECT COUNT(*) FROM reviews WHERE answered = ?", (day.isoformat(),)
        ).fetchone()[0]

    return {
        "ok": True,
        "rating": rating,
        "remembered": rating == "remember",
        "interval": interval,
        "stage": stage,
        "due": due.isoformat(),
        "answered_today": answered,
        "total": DAILY,
    }


def main() -> None:
    data = today_cards()
    print(f"{data['date']}: карточек {len(data['cards'])},"
          f" отвечено {data['answered_today']} из {data['total']}")
    for card in data["cards"]:
        if card["kind"] == "idea":
            print(f" - #{card['id']} идея: {card['quote'][:70]}")
        elif card["kind"] == "author":
            print(f" - #{card['id']} спор: {card['from_idea'][:45]} ↔ {card['to_idea'][:45]}")
        else:
            print(f" - #{card['id']} связь: {card['from_idea'][:45]} ↔ {card['to_idea'][:45]}")


if __name__ == "__main__":
    main()
