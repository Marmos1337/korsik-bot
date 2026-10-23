import asyncio
import logging

import uvicorn

from app import bot, db, max_api
from app.config import cfg
from app.web import app

log = logging.getLogger(__name__)


async def check_bot() -> None:
    try:
        me = await max_api.get_me()
    except Exception as e:
        log.error("GET /me failed: %s", e)
        return
    if not cfg.BOT_USERNAME:
        cfg.BOT_USERNAME = me.get("username") or ""
    log.info("bot %s (@%s) is ready", me.get("name"), me.get("username"))


async def main() -> None:
    db.init_db()
    if not cfg.MAX_BOT_TOKEN:
        log.error("MAX_BOT_TOKEN is empty, the bot cannot talk to MAX")
    await check_bot()

    # loops run next to uvicorn, not in the FastAPI lifespan, so web tests can use the app without starting the bot
    tasks = [asyncio.create_task(bot.polling_loop())]
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.WEB_HOST, port=cfg.WEB_PORT))
    try:
        await server.serve()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO, and MAX upload URLs carry an access key
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
