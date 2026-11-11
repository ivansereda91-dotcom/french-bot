"""Генерация и проверка заданий через Claude API."""
import json
import logging
import os
import re

MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")

SYSTEM = (
    "Ты — опытный преподаватель французского для русскоговорящих взрослых, живущих во Франции. "
    "Объяснения пиши по-русски, коротко и понятно. Французский — естественный, современный, "
    "как говорят во Франции сегодня. Строго соблюдай уровень CEFR ученика. "
    "Отвечай ТОЛЬКО одним JSON-объектом, без пояснений и без ```."
)

SCHEMAS = {
    "vocab": """{
  "fr": "слово или устойчивое выражение (с артиклем для существительных)",
  "ru": "перевод",
  "example_fr": "живой пример употребления",
  "example_ru": "перевод примера",
  "note": "1–2 предложения: когда и как это говорят, регистр, ловушки",
  "quiz": {"question": "вопрос на понимание или употребление этого выражения",
           "options": ["4 варианта"], "correct": 0, "explanation": "почему так"}
}""",
    "grammar": """{
  "topic": "название грамматической темы",
  "rule": "правило в 2–4 предложениях с мини-примером",
  "question": "французское предложение с пропуском ___",
  "options": ["4 варианта для пропуска"], "correct": 0,
  "explanation": "почему верен этот вариант и в чём ошибка остальных"
}""",
    "writing": """{
  "mode": "translate или write",
  "instruction": "что сделать, по-русски",
  "text": "для translate — русская фраза (1–2 предложения); для write — тема для 2–3 предложений по-французски",
  "hints": ["2–3 полезных французских слова или конструкции"],
  "topic": "короткое название темы"
}""",
}

CHECK_SCHEMA = """{
  "score": 0-10,
  "corrected": "исправленный вариант ответа ученика (максимально близко к его тексту)",
  "errors": [{"wrong": "фрагмент", "right": "как правильно", "why": "коротко по-русски"}],
  "comment": "1–2 предложения: что хорошо, над чем поработать",
  "model": "образцовый вариант носителя"
}"""

_client = None


def client():
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic
        _client = AsyncAnthropic()  # ключ из ANTHROPIC_API_KEY
    return _client


def parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("В ответе нет JSON")
    return json.loads(text[start:end + 1])


def validate(kind: str, d: dict) -> dict:
    if kind in ("vocab", "grammar"):
        q = d["quiz"] if kind == "vocab" else d
        opts = q["options"]
        if not (isinstance(opts, list) and 2 <= len(opts) <= 5):
            raise ValueError("Плохие варианты ответа")
        if not (0 <= int(q["correct"]) < len(opts)):
            raise ValueError("Неверный индекс ответа")
        q["correct"] = int(q["correct"])
        q["options"] = [str(o)[:60] for o in opts]  # ограничение кнопок Telegram
    if kind == "vocab":
        for k in ("fr", "ru", "example_fr"):
            assert d.get(k), k
    if kind == "writing":
        assert d.get("instruction") and d.get("text")
    return d


async def ask(prompt: str, max_tokens: int = 900) -> dict:
    msg = await client().messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return parse_json("".join(b.text for b in msg.content if b.type == "text"))


async def make_task(kind: str, level: str, topics: str, avoid: list[str]) -> dict:
    avoid_txt = "; ".join(avoid[:40]) or "—"
    prompt = (
        f"Составь задание типа «{kind}» для ученика уровня {level}.\n"
        f"Интересы ученика: {topics}.\n"
        f"Не повторяй уже пройденное: {avoid_txt}.\n"
        f"Формат ответа:\n{SCHEMAS[kind]}"
    )
    last_err = None
    for _ in range(2):  # одна повторная попытка, если JSON кривой
        try:
            return validate(kind, await ask(prompt))
        except Exception as e:  # noqa: BLE001
            last_err = e
            logging.warning("Плохое задание %s: %s", kind, e)
    raise RuntimeError(f"Не удалось получить задание: {last_err}")


async def check_writing(level: str, task: dict, answer: str) -> dict:
    prompt = (
        f"Ученик уровня {level} выполнял задание.\n"
        f"Инструкция: {task['instruction']}\nТекст задания: {task['text']}\n"
        f"Ответ ученика: «{answer}»\n"
        f"Проверь доброжелательно, оценивай с учётом уровня. Формат:\n{CHECK_SCHEMA}"
    )
    d = await ask(prompt, max_tokens=1200)
    d["score"] = max(0, min(10, int(d.get("score", 0))))
    d.setdefault("errors", [])
    return d
