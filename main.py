import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, Update

import aiosqlite

# === Конфиг из переменных окружения ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "MM_studio_spb")  # без @
ADMIN_USERNAMES = os.getenv("ADMIN_USERNAMES", "M_M_nails,N_a_i_t")
DB_PATH = os.getenv("DB_PATH", "participants.db")
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в переменных окружения")

# ВАЖНО: теперь путь вебхука фиксированный, без токена
WEBHOOK_PATH = "/webhook"

# === Глобальные объекты бота / диспетчера / БД ===
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

db = None  # соединение с SQLite


# === Работа с БД ===
async def init_db():
    global db
    db = await aiosqlite.connect(DB_PATH)
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            username TEXT,
            first_name TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.commit()
    logging.info("База participants готова")


async def get_or_create_participant(user_id, username, first_name):
    """Вернёт существующий номер или создаст новый."""
    global db
    cur = await db.execute(
        "SELECT id FROM participants WHERE user_id = ?", (user_id,)
    )
    row = await cur.fetchone()
    await cur.close()

    if row:
        return row[0]

    cur = await db.execute(
        "INSERT INTO participants (user_id, username, first_name) VALUES (?, ?, ?)",
        (user_id, username, first_name),
    )
    await db.commit()
    return cur.lastrowid


async def get_all_participants():
    global db
    cur = await db.execute(
        "SELECT id, user_id, username, first_name FROM participants ORDER BY id"
    )
    rows = await cur.fetchall()
    await cur.close()
    return rows


# === Вспомогательные функции ===
def is_admin(message: Message) -> bool:
    if not message.from_user:
        return False
    username = (message.from_user.username or "").lower()
    admins = [u.strip().lower() for u in ADMIN_USERNAMES.split(",") if u.strip()]
    return username in admins


async def check_subscription(bot: Bot, user_id: int) -> bool:
    """Проверяем, подписан ли пользователь на канал."""
    chat_id = f"@{CHANNEL_USERNAME.lstrip('@')}"
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        status = member.status  # member / administrator / creator / left / kicked / restricted
        return status in ("member", "administrator", "creator")
    except Exception as e:
        logging.exception("Не удалось проверить подписку: %s", e)
        # Если что-то пошло не так, считаем, что не подписан
        return False


async def handle_registration(message: Message, bot: Bot):
    """Общий код регистрации, вызывается из /start и 'участвую'."""
    if not message.from_user:
        return

    user = message.from_user
    subscribed = await check_subscription(bot, user.id)

    if not subscribed:
        await message.answer(
            "Похоже, ты ещё не подписан(а) на канал 🥲\n\n"
            "Подпишись, пожалуйста, на канал:\n"
            "👉 https://t.me/MM_studio_spb\n\n"
            "После этого снова нажми /start или напиши «участвую»."
        )
        return

    number = await get_or_create_participant(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
    )

    mention = f"@{user.username}" if user.username else (user.first_name or "участник")

    await message.answer(
        f"{mention}, ты участвуешь в розыгрыше! 🎉\n"
        f"Твой номер: {number}"
    )


# === Хэндлеры бота ===
@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot):
    await handle_registration(message, bot)


@router.message(F.text.regexp("(?i)участвую"))
async def on_participate(message: Message, bot: Bot):
    await handle_registration(message, bot)


@router.message(Command("list"))
async def cmd_list(message: Message):
    """Список участников — только для админов."""
    if not is_admin(message):
        return

    rows = await get_all_participants()
    if not rows:
        await message.answer("Пока никто не зарегистрировался.")
        return

    lines = []
    for pid, user_id, username, first_name in rows:
        nick = f"@{username}" if username else (first_name or str(user_id))
        lines.append(f"{pid}. {nick} (id {user_id})")

    # Чтобы не упереться в лимит 4096 символов — порежем при необходимости
    chunk = "Список участников:\n"
    for line in lines:
        if len(chunk) + len(line) + 1 > 4000:
            await message.answer(chunk.rstrip())
            chunk = "Список участников (продолжение):\n"
        chunk += line + "\n"

    if chunk.strip():
        await message.answer(chunk.rstrip())


# === Обработчик webhook для aiohttp ===
async def handle_webhook(request: web.Request) -> web.Response:
    """Сюда Телеграм шлёт апдейты."""
    try:
        data = await request.json()
    except Exception as e:
        logging.exception("Не удалось распарсить JSON от Telegram: %s", e)
        # При кривом JSON реально возвращаем 400
        return web.Response(status=400, text="Bad Request")

    try:
        update = Update.model_validate(data, context={"bot": bot})
        await dp.feed_update(update)
    except Exception as e:
        logging.exception("Ошибка при обработке апдейта: %s", e)
        # В любом случае отвечаем 200, чтобы Telegram не отключал webhook
        return web.Response(text="ok")

    return web.Response(text="ok")


def create_app() -> web.Application:
    app = web.Application()

    # маршрут для Telegram webhook: ЧЁТКО /webhook
    app.router.add_post(WEBHOOK_PATH, handle_webhook)

    # healthcheck на /
    async def healthcheck(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    app.router.add_get("/", healthcheck)

    async def on_startup(app: web.Application):
        logging.info("Запуск приложения, инициализируем БД...")
        await init_db()

    async def on_cleanup(app: web.Application):
        logging.info("Останавливаемся, закрываем БД и сессию бота...")
        global db
        if db is not None:
            await db.close()
        await bot.session.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=PORT)
