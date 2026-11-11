# Français chaque jour — семейный Telegram-бот

Два задания в день для каждого, у каждого свой уровень (A1–C1) и своё время:
- **1-е задание** — «Слово дня» (выражение, пример, заметка, мини-тест) и повторение старых слов по интервалам (1 → 3 → 7 → 16 → 35 дней);
- **2-е задание** — по очереди грамматика (тест с объяснением) или перевод / письмо (ИИ проверяет, исправляет и объясняет ошибки).

Ответы видят оба: когда один отвечает, второму приходит задание, ответ и разбор.

Задания составляет и проверяет Claude (по умолчанию модель `claude-haiku-5-5`).

## Команды
`/task` задание сейчас · `/today` задания и ответы обоих за сегодня · `/stats` прогресс обоих · `/words` мои слова · `/level` уровень · `/time` время · `/topics` интересы · `/pause` пауза

## Настройки (.env)
| Переменная | Что это |
|---|---|
| `BOT_TOKEN` | токен от @BotFather |
| `ANTHROPIC_API_KEY` | ключ Claude API (console.anthropic.com) |
| `ALLOWED_IDS` | Telegram ID через запятую |
| `DB_PATH` | путь к базе SQLite |
| `SHARE_WITH_PARTNER` | `1` — показывать ответы друг другу |
| `CLAUDE_MODEL` | модель (необязательно) |

## Установка на сервер
```bash
git clone https://github.com/ivansereda91-dotcom/french-bot /opt/french-bot && bash /opt/french-bot/setup.sh
bash /opt/french-bot/allow.sh ID1 ID2
```
Обновление: `cd /opt/french-bot && git pull && systemctl restart french-bot`
