"""Единственное место, где проект разговаривает с моделью.

Все скрипты зовут только `chat(messages)`. Чтобы поменять DeepSeek на другую
модель (например, Claude API), правьте этот файл: адрес API, имя модели,
заголовки и разбор ответа. Остальной код трогать не нужно.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

API = "http://127.0.0.1:19040/v1/chat/completions"  # класс-API, без ключа
MODEL = "DeepSeek-V4.1-Flash"
TIMEOUT = 180  # секунд на один ответ


def chat(messages: list[dict], max_tokens: int = 2000) -> str:
    """Один запрос к модели: список сообщений → текст ответа."""
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
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(exc.read().decode("utf-8", "replace") or str(exc)) from exc
    content = data["choices"][0]["message"]["content"]
    if not content or not content.strip():
        raise RuntimeError("Модель вернула пустой ответ.")
    return content
