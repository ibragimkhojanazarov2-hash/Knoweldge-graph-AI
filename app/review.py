"""Повторение: пять карточек в день из ключевых идей и связей.

Карточка либо показывает цитату и спрашивает, какую мысль автор ею
доказывает, либо показывает две идеи и спрашивает, как они связаны.
Ответ «Помню» откладывает карточку на 3, 7 и 21 день, «Не помню» —
на завтра и возвращает её в начало.

Карточки лежат в таблице reviews: source — "idea:<id>" или "link:<id>",
stage — сколько раз подряд ответили «Помню» (0–3), due — дата показа.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone

DAILY = 5
STEPS = (3, 7, 21)


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


def _mix(ideas: list[str], links: list[str], need: int) -> list[tuple[str, str]]:
    """Берём и идеи, и связи: на пять карточек примерно три идеи и две связи."""
    chosen: list[tuple[str, str]] = []
    pattern = ("idea", "idea", "link", "idea", "link")
    while len(chosen) < need and (ideas or links):
        for kind in pattern:
            if len(chosen) >= need:
                break
            pool = ideas if kind == "idea" else links
            if pool:
                chosen.append((pool.pop(0), kind))
    return chosen


def _seed(day: date, need: int) -> None:
    """Добавляем в оборот новые ключевые идеи и связи — по пять в день."""
    with _db() as db:
        idea_rows = db.execute(
            "SELECT 'idea:' || k.id AS source FROM key_ideas k"
            " WHERE NOT EXISTS (SELECT 1 FROM reviews r WHERE r.source = 'idea:' || k.id)"
        ).fetchall()
        link_rows = db.execute(
            "SELECT 'link:' || l.id AS source FROM links l"
            " LEFT JOIN link_marks lm ON lm.from_id = l.from_id AND lm.to_id = l.to_id"
            " WHERE COALESCE(lm.verdict, '') <> 'wrong'"
            "   AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.source = 'link:' || l.id)"
        ).fetchall()

    stamp = day.isoformat()
    ideas = sorted((row["source"] for row in idea_rows), key=lambda s: _order_key(s, stamp))
    links = sorted((row["source"] for row in link_rows), key=lambda s: _order_key(s, stamp))
    chosen = _mix(ideas, links, need)
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

    if kind == "link":
        with _db() as db:
            row = db.execute(
                "SELECT l.id, l.from_id, l.to_id, l.type AS link_type,"
                "       l.explanation, l.confidence,"
                "       fi.idea AS from_idea, fb.title AS from_book_title,"
                "       ti.idea AS to_idea, tb.title AS to_book_title"
                "  FROM links l"
                "  JOIN ideas fi ON fi.id = l.from_id"
                "  JOIN ideas ti ON ti.id = l.to_id"
                "  JOIN books fb ON fb.id = fi.book_id"
                "  JOIN books tb ON tb.id = ti.book_id"
                "  LEFT JOIN link_marks lm ON lm.from_id = l.from_id AND lm.to_id = l.to_id"
                " WHERE l.id = ? AND COALESCE(lm.verdict, '') <> 'wrong'",
                (int(ref),),
            ).fetchone()
        if row is None:
            return None
        return {
            "kind": "link",
            "link_id": row["id"],
            "from_id": row["from_id"],
            "to_id": row["to_id"],
            "link_type": row["link_type"],
            "explanation": row["explanation"] or "",
            "confidence": row["confidence"],
            "from_idea": row["from_idea"],
            "from_book_title": row["from_book_title"],
            "to_idea": row["to_idea"],
            "to_book_title": row["to_book_title"],
        }

    return None


def _collect(day: date, limit: int) -> list[dict]:
    if limit <= 0:
        return []
    with _db() as db:
        rows = db.execute(
            "SELECT id, source, stage FROM reviews WHERE due <= ?"
            " ORDER BY due, id LIMIT ?",
            (day.isoformat(), limit),
        ).fetchall()
    cards, dead = [], []
    for row in rows:
        payload = _payload(row["source"])
        if payload is None:
            dead.append(row["id"])
            continue
        cards.append({"id": row["id"], "stage": row["stage"], **payload})
    if dead:
        with _db() as db:
            db.executemany("DELETE FROM reviews WHERE id = ?", [(card_id,) for card_id in dead])
    return cards


def today_cards(day: date | None = None) -> dict:
    """Пять карточек на сегодня. Уже отвеченные сегодня не повторяем."""
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


def answer_card(card_id: int, remembered: bool, day: date | None = None) -> dict | None:
    """«Помню» — через 3, 7 и 21 день; «Не помню» — завтра, с начала."""
    day = day or date.today()
    with _db() as db:
        row = db.execute("SELECT stage FROM reviews WHERE id = ?", (card_id,)).fetchone()
        if row is None:
            return None
        stage = int(row["stage"])
        if remembered:
            interval = STEPS[min(stage, len(STEPS) - 1)]
            stage = min(stage + 1, len(STEPS))
        else:
            interval = 1
            stage = 0
        due = day + timedelta(days=interval)
        db.execute(
            "UPDATE reviews SET stage = ?, due = ?, seen = seen + 1,"
            " forgot = forgot + ?, answered = ?, updated = ? WHERE id = ?",
            (stage, due.isoformat(), 0 if remembered else 1, day.isoformat(), _now(), card_id),
        )
        answered = db.execute(
            "SELECT COUNT(*) FROM reviews WHERE answered = ?", (day.isoformat(),)
        ).fetchone()[0]

    return {
        "ok": True,
        "remembered": remembered,
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
        else:
            print(f" - #{card['id']} связь: {card['from_idea'][:45]} ↔ {card['to_idea'][:45]}")


if __name__ == "__main__":
    main()
