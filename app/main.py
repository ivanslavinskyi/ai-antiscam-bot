import asyncio
import logging

from .bot import create_bot, create_dispatcher
from .handlers import messages, commands, admin
from .config import settings
from .db.session import engine
from .db.models import Base

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    # Создаём таблицы, если их ещё нет
    logger.info("Creating database tables (if not exist)...")
    Base.metadata.create_all(bind=engine)

    bot = create_bot()
    dp = create_dispatcher()

    # Регистрируем роутеры
    dp.include_router(commands.router)
    dp.include_router(admin.router)
    dp.include_router(messages.router)

    logger.info("Starting anti-scam bot polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
