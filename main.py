import os
import logging
from typing import Optional

import asyncpg
from aiohttp import web

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from aiogram.enums import ChatMemberStatus
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# ----------------------------
# ЛОГИ
# ----------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ----------------------------
# НАСТРОЙКИ ЧЕРЕЗ ENV
# ----------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Переменная окружения BOT_TOKEN не задана")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Переменная окружения DATABASE_URL не задана")

# Канал по умолчанию твой
CHANNEL_ID = os.getenv("CHANNEL_ID", "@MM_studio_spb")

# Список админов (через запятую): "1234567,9876543"
_admin_ids_env = os.getenv("ADMIN_IDS", "").replace(" ", "")
ADMIN_IDS = {int(x) for x in _admin_ids_env.split(",") if x}

if not ADMIN_IDS:
    logger.warning(
        "Переменная ADMIN_IDS не задана или пустая. Команда /list будет недоступна."
    )

# Путь, на который Telegram будет слать вебхуки
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"

# ----------------------------
# ИНИЦИАЛИЗАЦИЯ БОТА И ДИСПЕТЧЕРА
# ----------------------------

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Пул соединений с БД
db_pool: Optional[asyncpg.Pool] = None


# ----------------------------
# РАБОТА С БАЗОЙ
# ----------------------------

async def init_db() -> None:
    """
    Создаём пул соединений и таблицу participants, если её ещё нет.
    """
    global db_pool
    logger.info("Подключаемся к БД...")
    db_pool = await asyncpg.create_pool(DATABASE_URL)

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS participants (
                id SERIAL PRIMARY KEY,
                user_id BIGINT UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                created_at TIMESTAMPTZ DEFAULT now()
            );
            """
        )
    logger.info("БД инициализирована")


async def get_participant(user_id: int) -> Optional[asyncpg.Record]:
    """
    Ищем участника по Telegram user_id.
    """
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, user_id, username, first_name, last_name
            FROM participants
            WHERE user_id = $1
            """,
            user_id,
        )
        return row


async def add_participant(user) -> int:
    """
    Добавляем участника (или обновляем его данные, если он уже есть).
    Возвращаем его номер (id).
    """
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO participants (user_id, username, first_name, last_name)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id) DO UPDATE
                SET username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name
            RETURNING id;
            """,
            user.id,
            user.username,
            user.first_name,
            user.last_name,
        )
        return row["id"]


async def list_participants() -> list[asyncpg.Record]:
    """
    Возвращает список всех участников, отсортированный по номеру.
    """
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, user_id, username, first_name, last_name
            FROM participants
            ORDER BY id;
            """
        )
        return list(rows)


# ----------------------------
# ПРОВЕРКА ПОДПИСКИ НА КАНАЛ
# ----------------------------

async def check_subscription(user_id: int) -> bool:
    """
    Возвращает True, если пользователь подписан на канал.
    Используем getChatMember. :contentReference[oaicite:3]{index=3}
    """
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
    except Exception as e:
        logger.warning("Не удалось получить статус участника канала: %s", e)
        return False

    status = member.status
    return status in (
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.RESTRICTED,  # в канале, но с ограничениями
    )


# ----------------------------
# ХЕНДЛЕРЫ
# ----------------------------

@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """
    /start:
    1. Если уже участвует — просто показываем его номер.
    2. Если не участвует — проверяем подписку.
    3. Если подписан — регистрируем и выдаём номер.
    4. Если нет — просим подписаться.
    """
    user = message.from_user
    if not user:
        return

    # 1) Уже зарегистрирован?
    existing = await get_participant(user.id)
    if existing:
        num = existing["id"]
        await message.answer(
            f"Ты уже участвуешь в розыгрыше 🎉\n"
            f"Твой номер: <b>{num}</b>",
            parse_mode="HTML",
        )
        return

    # 2) Проверяем подписку
    subscribed = await check_subscription(user.id)
    if not subscribed:
        link = "https://t.me/MM_studio_spb"
        await message.answer(
            "Пока я не вижу у тебя подписки на канал 😔\n\n"
            f"1. Подпишись на канал: {link}\n"
            "2. Потом снова нажми /start у бота.",
        )
        return

    # 3) Регистрируем нового участника
    num = await add_participant(user)
    mention = f"@{user.username}" if user.username else (user.full_name or "участник")

    await message.answer(
        f"{mention}, ты участвуешь в розыгрыше! 🎁\n"
        f"Твой номер: <b>{num}</b>",
        parse_mode="HTML",
    )


@dp.message(Command("list"))
async def cmd_list(message: Message) -> None:
    """
    /list — только для админа.
    Показывает список участников: номер -> ник/имя.
    """
    user = message.from_user
    if not user or user.id not in ADMIN_IDS:
        # игнорируем, чтобы никто лишний не видел
        return

    rows = await list_participants()

    if not rows:
        await message.answer("Участников пока нет.")
        return

    lines: list[str] = []
    for row in rows:
        num = row["id"]
        user_id = row["user_id"]
        username = row["username"]
        first_name = row["first_name"] or ""
        last_name = row["last_name"] or ""

        if username:
            name = f"@{username}"
        else:
            name = (first_name + " " + last_name).strip() or "(без имени)"

        lines.append(f"{num}. {name} (id: {user_id})")

    text = "Список участников:\n\n" + "\n".join(lines)
    await message.answer(text)


# ----------------------------
# AIOHTTP + WEBHOOK
# ----------------------------

async def on_startup(app: web.Application) -> None:
    """
    Запускается при старте веб-приложения.
    Инициализируем БД.
    """
    await init_db()
    logger.info("Приложение запущено.")


async def on_shutdown(app: web.Application) -> None:
    """
    Корректно закрываем ресурсы.
    """
    global db_pool
    if db_pool is not None:
        await db_pool.close()
    await bot.session.close()
    logger.info("Приложение остановлено.")


def create_app() -> web.Application:
    """
    Создаём aiohttp-приложение и вешаем на него webhook-обработчик aiogram.
    """
    app = web.Application()

    # Регистрируем обработчик вебхука на путь /webhook/<BOT_TOKEN>
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(
        app, path=WEBHOOK_PATH
    )

    # Эта функция настраивает работу диспетчера внутри aiohttp-приложения. :contentReference[oaicite:4]{index=4}
    setup_application(app, dp, bot=bot)

    # Хуки старта/остановки
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    # Простой healthcheck на /
    async def healthcheck(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    app.router.add_get("/", healthcheck)

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    # Render ожидает, что сервис слушает порт из переменной PORT. :contentReference[oaicite:5]{index=5}
    web.run_app(app, host="0.0.0.0", port=port)
