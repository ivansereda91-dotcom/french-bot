"""Семейный бот для французского: 2 задания в день, у каждого свой уровень и время."""
import asyncio
import html
import logging
import os
import random

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ai
import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

TOKEN = os.environ["BOT_TOKEN"]
ALLOWED = {int(x) for x in os.getenv("ALLOWED_IDS", "").replace(" ", "").split(",") if x}
SHARE = os.getenv("SHARE_WITH_PARTNER", "1") == "1"

router = Router()
e = html.escape


class Settings(StatesGroup):
    times = State()
    topics = State()


# ---------- доступ ----------

class Access(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        u = data.get("event_from_user")
        if u is None:
            return
        if u.id not in ALLOWED:
            if isinstance(event, Message):
                await event.answer(f"Это частный бот. Твой Telegram ID: <code>{u.id}</code>")
            elif isinstance(event, CallbackQuery):
                await event.answer("Нет доступа", show_alert=True)
            return
        db.ensure_user(u.id, u.first_name or str(u.id))
        return await handler(event, data)


# ---------- оформление заданий ----------

def options_kb(task_id: int, options: list[str]):
    kb = InlineKeyboardBuilder()
    for i, o in enumerate(options):
        kb.button(text=o, callback_data=f"ans:{task_id}:{i}")
    kb.adjust(1)
    return kb.as_markup()


def render(t: dict) -> str:
    p, k = t["payload"], t["kind"]
    if k == "vocab":
        q = p["quiz"]
        return (f"📘 <b>Слово дня</b>\n\n<b>{e(p['fr'])}</b> — {e(p['ru'])}\n\n"
                f"<i>{e(p['example_fr'])}</i>\n{e(p.get('example_ru', ''))}\n\n"
                f"💡 {e(p.get('note', ''))}\n\n❓ {e(q['question'])}")
    if k == "grammar":
        return (f"📐 <b>Грамматика: {e(p['topic'])}</b>\n\n{e(p['rule'])}\n\n"
                f"❓ {e(p['question'])}")
    if k == "review":
        return f"🔁 <b>Повторение</b>\n\nЧто значит «<b>{e(p['fr'])}</b>»?"
    if k == "writing":
        title = "Перевод" if p.get("mode") == "translate" else "Напиши по-французски"
        hints = ", ".join(e(h) for h in p.get("hints", []))
        return (f"✍️ <b>{title}</b>\n\n{e(p['instruction'])}\n\n«{e(p['text'])}»\n\n"
                f"Подсказки: {hints}\n\n<i>Ответь сообщением — я проверю.</i>")
    return "?"


def quiz_of(t: dict) -> dict:
    return t["payload"]["quiz"] if t["kind"] == "vocab" else t["payload"]


def quiz_result(t: dict, idx: int) -> str:
    q = quiz_of(t)
    ok = idx == q["correct"]
    head = "✅ Верно!" if ok else f"❌ Правильно: <b>{e(q['options'][q['correct']])}</b>"
    expl = q.get("explanation", "")
    return f"{head}\n{e(expl)}" if expl else head


def writing_result(fb: dict) -> str:
    lines = [f"<b>Оценка: {fb['score']}/10</b>", "", f"✏️ {e(fb.get('corrected', ''))}"]
    for err in fb.get("errors", [])[:6]:
        lines.append(f"• <s>{e(err.get('wrong', ''))}</s> → <b>{e(err.get('right', ''))}</b> — {e(err.get('why', ''))}")
    if fb.get("comment"):
        lines += ["", f"💬 {e(fb['comment'])}"]
    if fb.get("model"):
        lines += ["", f"🇫🇷 Как сказал бы француз: <i>{e(fb['model'])}</i>"]
    return "\n".join(lines)


# ---------- выдача заданий ----------

async def send_task(bot: Bot, uid: int, kind: str) -> None:
    u = db.user(uid)
    if kind == "review":
        w = db.due_word(uid)
        if not w:
            return
        wrong = db.distractors(uid, w["id"])
        if len(wrong) < 3:
            return
        options = wrong + [w["ru"]]
        random.shuffle(options)
        payload = {"word_id": w["id"], "fr": w["fr"], "options": options,
                   "correct": options.index(w["ru"]), "explanation": w["example"] or ""}
    else:
        try:
            payload = await ai.make_task(kind, u["level"], u["topics"], db.recent_items(uid))
        except Exception as ex:  # noqa: BLE001
            logging.exception("ИИ не ответил")
            await bot.send_message(uid, f"😕 Не получилось подготовить задание ({e(str(ex))[:200]}). "
                                        "Попробуй позже командой /task.")
            return
    tid = db.add_task(uid, kind, payload)
    t = db.task(tid)
    markup = options_kb(tid, quiz_of(t)["options"]) if kind != "writing" else None
    await bot.send_message(uid, render(t), reply_markup=markup)


def slot_kinds(slot: int) -> list[str]:
    if slot == 1:
        return ["vocab", "review"]
    return ["grammar"] if db.today().toordinal() % 2 == 0 else ["writing"]


async def scheduler(bot: Bot):
    while True:
        try:
            for u, slot in db.due_slots():
                if u["tg_id"] in ALLOWED and db.mark_sent(u["tg_id"], db.today().isoformat(), slot):
                    for kind in slot_kinds(slot):
                        await send_task(bot, u["tg_id"], kind)
        except Exception:  # noqa: BLE001
            logging.exception("Ошибка планировщика")
        await asyncio.sleep(30)


# ---------- партнёр видит ответы ----------

async def share(bot: Bot, who_id: int, t: dict, result: str):
    if not SHARE:
        return
    who = e(db.user(who_id)["name"])
    answer = t["answer"] if t["kind"] == "writing" else quiz_of(t)["options"][int(t["answer"])]
    text = (f"👀 <b>{who}</b> выполнил(а) задание\n\n{render(t).split('<i>Ответь')[0].strip()}\n\n"
            f"Ответ: <b>{e(answer)}</b>\n{result}")
    for uid in ALLOWED - {who_id}:
        try:
            await bot.send_message(uid, text[:4000])
        except Exception as ex:  # noqa: BLE001
            logging.warning("Не отправлено %s: %s", uid, ex)


# ---------- команды ----------

def level_kb():
    kb = InlineKeyboardBuilder()
    for lv in db.LEVELS:
        kb.button(text=lv, callback_data=f"lvl:{lv}")
    kb.adjust(4)
    return kb.as_markup()


@router.message(CommandStart())
async def start(msg: Message, state: FSMContext):
    await state.clear()
    u = db.user(msg.from_user.id)
    await msg.answer(
        f"Bonjour, {e(u['name'])} ! 🇫🇷\n\n"
        f"Каждый день я пришлю два задания:\n"
        f"• <b>{u['time1']}</b> — новое слово и повторение старых\n"
        f"• <b>{u['time2']}</b> — грамматика или перевод (по очереди)\n\n"
        f"Ответы друг друга вы тоже видите 👀\n\n"
        f"Выбери свой уровень:",
        reply_markup=level_kb(),
    )


@router.message(Command("help"))
async def help_(msg: Message):
    await msg.answer(
        "/task — задание прямо сейчас\n/today — задания и ответы обоих за сегодня\n"
        "/stats — прогресс\n/words — мои слова\n/level — уровень\n"
        "/time — время заданий\n/topics — интересы для заданий\n/pause — пауза / продолжить"
    )


@router.message(Command("level"))
async def level(msg: Message):
    await msg.answer(f"Сейчас: <b>{db.user(msg.from_user.id)['level']}</b>. Новый уровень:", reply_markup=level_kb())


@router.callback_query(F.data.startswith("lvl:"))
async def set_level(cb: CallbackQuery):
    lv = cb.data.split(":", 1)[1]
    db.update_user(cb.from_user.id, level=lv)
    u = db.user(cb.from_user.id)
    await cb.message.edit_text(
        f"Уровень: <b>{lv}</b> ✅\nЗадания в {u['time1']} и {u['time2']} (изменить — /time).\n"
        f"Интересы: {e(u['topics'])} (изменить — /topics).\n\nПервое задание — /task"
    )
    await cb.answer()


@router.message(Command("time"))
async def ask_time(msg: Message, state: FSMContext):
    await state.set_state(Settings.times)
    await msg.answer("Во сколько присылать задания? Напиши два времени, например <code>8:30 20:00</code>")


@router.message(Settings.times, F.text)
async def set_time(msg: Message, state: FSMContext):
    t = db.parse_times(msg.text)
    if not t:
        return await msg.answer("Не понял. Пример: <code>9:00 19:30</code>")
    db.update_user(msg.from_user.id, time1=t[0], time2=t[1])
    await state.clear()
    await msg.answer(f"Готово: задания в <b>{t[0]}</b> и <b>{t[1]}</b> ⏰")


@router.message(Command("topics"))
async def ask_topics(msg: Message, state: FSMContext):
    await state.set_state(Settings.topics)
    await msg.answer(f"Сейчас: <i>{e(db.user(msg.from_user.id)['topics'])}</i>\n\n"
                     "Напиши, о чём тебе интересно — задания будут на эти темы. "
                     "Например: <code>работа в HR, искусство, кино, путешествия</code>")


@router.message(Settings.topics, F.text)
async def set_topics(msg: Message, state: FSMContext):
    db.update_user(msg.from_user.id, topics=msg.text.strip()[:300])
    await state.clear()
    await msg.answer("Учту в заданиях 👍")


@router.message(Command("pause"))
async def pause(msg: Message):
    u = db.user(msg.from_user.id)
    db.update_user(msg.from_user.id, paused=0 if u["paused"] else 1)
    await msg.answer("▶️ Задания снова приходят" if u["paused"] else "⏸ Пауза. Вернуть — /pause")


@router.message(Command("task"))
async def task_menu(msg: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    for kind, label in (("vocab", "📘 Слово"), ("grammar", "📐 Грамматика"),
                        ("writing", "✍️ Перевод/письмо"), ("review", "🔁 Повторить слово")):
        kb.button(text=label, callback_data=f"new:{kind}")
    kb.adjust(2)
    await msg.answer("Какое задание?", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("new:"))
async def task_new(cb: CallbackQuery, bot: Bot):
    kind = cb.data.split(":")[1]
    await cb.answer("Готовлю…")
    await cb.message.edit_text("⏳ Готовлю задание…")
    if kind == "review" and not db.due_word(cb.from_user.id):
        await cb.message.edit_text("Сегодня повторять нечего — все слова свежие 👌")
        return
    await send_task(bot, cb.from_user.id, kind)
    await cb.message.delete()


# ---------- ответы ----------

@router.callback_query(F.data.startswith("ans:"))
async def answer_quiz(cb: CallbackQuery, bot: Bot):
    _, tid, idx = cb.data.split(":")
    t = db.task(int(tid))
    if not t or t["user_id"] != cb.from_user.id:
        return await cb.answer("Это не твоё задание 🙂", show_alert=True)
    idx = int(idx)
    q = quiz_of(t)
    ok = idx == q["correct"]
    if not db.save_answer(t["id"], str(idx), int(ok)):
        return await cb.answer("Уже отвечено")
    if t["kind"] == "vocab":
        p = t["payload"]
        db.add_word(cb.from_user.id, p["fr"], p["ru"], p.get("example_fr", ""))
    if t["kind"] == "review":
        db.review_word(t["payload"]["word_id"], ok)
    result = quiz_result(t, idx)
    await cb.message.edit_text(f"{render(t)}\n\nТвой ответ: <b>{e(q['options'][idx])}</b>\n{result}")
    await cb.answer("✅" if ok else "❌")
    t["answer"] = str(idx)
    await share(bot, cb.from_user.id, t, result)


@router.message(StateFilter(None), F.text & ~F.text.startswith("/"))
async def answer_writing(msg: Message, bot: Bot):
    t = db.open_writing_task(msg.from_user.id)
    if not t:
        return await msg.answer("Открытых заданий нет. Новое — /task, все команды — /help")
    wait = await msg.answer("⏳ Проверяю…")
    try:
        fb = await ai.check_writing(db.user(msg.from_user.id)["level"], t["payload"], msg.text[:1500])
    except Exception:  # noqa: BLE001
        logging.exception("Проверка не удалась")
        return await wait.edit_text("😕 Не получилось проверить. Пришли ответ ещё раз чуть позже.")
    if not db.save_answer(t["id"], msg.text[:1500], fb["score"], fb):
        return await wait.edit_text("На это задание уже есть ответ.")
    result = writing_result(fb)
    await wait.edit_text(result)
    t["answer"] = msg.text[:1500]
    await share(bot, msg.from_user.id, t, result)


# ---------- прогресс ----------

def stats_text(uid: int) -> str:
    u, s = db.user(uid), db.stats(uid)
    acc = f"{s['quiz_ok']}/{s['quiz_total']} ({s['quiz_ok'] / s['quiz_total']:.0%})" if s["quiz_total"] else "—"
    wr = f"{s['writing_avg']}/10" if s["writing_avg"] is not None else "—"
    return (f"<b>{e(u['name'])}</b> · {u['level']}\n"
            f"🔥 Серия: {s['streak']} дн. · ✅ Тесты: {acc} · ✍️ Письмо: {wr}\n"
            f"📘 Слов: {s['words']} (выучено: {s['learned']})")


@router.message(Command("stats"))
async def stats(msg: Message):
    me = msg.from_user.id
    others = [uid for uid in ALLOWED if uid != me and db.user(uid)]
    parts = [stats_text(me)] + [stats_text(uid) for uid in others]
    await msg.answer("📊 <b>Прогресс</b>\n\n" + "\n\n".join(parts))


@router.message(Command("words"))
async def my_words(msg: Message):
    ws = db.words(msg.from_user.id)
    if not ws:
        return await msg.answer("Слов пока нет — они появятся после заданий «Слово дня».")
    stars = {1: "▫️", 2: "▫️", 3: "🔸", 4: "🔹", 5: "⭐️"}
    lines = [f"{stars[w['box']]} <b>{e(w['fr'])}</b> — {e(w['ru'])}" for w in ws[:60]]
    await msg.answer("📘 <b>Мои слова</b> (⭐️ — выучено)\n\n" + "\n".join(lines))


@router.message(Command("today"))
async def today(msg: Message):
    tasks = db.tasks_on(db.today().isoformat())
    if not tasks:
        return await msg.answer("Сегодня заданий ещё не было.")
    names = {u["tg_id"]: u["name"] for u in db.all_users()}
    icons = {"vocab": "📘", "grammar": "📐", "writing": "✍️", "review": "🔁"}
    lines = []
    for t in tasks:
        p = t["payload"]
        title = p.get("fr") or p.get("topic") or p.get("text", "")[:50]
        if t["answer"] is None:
            res = "⏳ ждёт ответа"
        elif t["kind"] == "writing":
            res = f"{t['correct']}/10"
        else:
            res = "✅" if t["correct"] else "❌"
        lines.append(f"{icons[t['kind']]} {e(names.get(t['user_id'], '?'))}: {e(title)} — {res}")
    await msg.answer("📅 <b>Сегодня</b>\n\n" + "\n".join(lines))


async def main():
    db.init()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(Access())
    dp.callback_query.outer_middleware(Access())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(scheduler(bot))
    logging.info("Французский бот запущен, доступ: %s", ALLOWED or "никому")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
