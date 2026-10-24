import asyncio
import logging

import uvicorn

from app import bot, db, engine, hr, knowledge, llm, max_api
from app.config import cfg
from app.web import app

log = logging.getLogger(__name__)

BOT_NAME_RETRY = 60  # seconds between GET /me attempts while the bot name is unknown


async def check_bot() -> bool:
    try:
        me = await max_api.get_me()
    except Exception as e:
        log.error("GET /me failed: %s", e)
        return False
    if not cfg.BOT_USERNAME:
        cfg.BOT_USERNAME = me.get("username") or ""
    log.info("bot %s (@%s) is ready", me.get("name"), me.get("username"))
    return bool(cfg.BOT_USERNAME)


async def wait_for_bot_name() -> None:
    # invite links are built from the bot name, so keep asking MAX until it answers
    while not await check_bot():
        await asyncio.sleep(BOT_NAME_RETRY)


async def main() -> None:
    db.init_db()
    if not cfg.MAX_BOT_TOKEN:
        log.error("MAX_BOT_TOKEN is empty, the bot cannot talk to MAX")
    for problem in engine.reload_scenario():
        log.error("scenario: %s", problem)

    # loops run next to uvicorn, not in the FastAPI lifespan, so web tests can use the app without starting the bot
    tasks = [
        asyncio.create_task(wait_for_bot_name()),
        asyncio.create_task(bot.polling_loop()),
        asyncio.create_task(engine.scheduler_loop()),
        asyncio.create_task(hr.followup_loop()),
        asyncio.create_task(llm.watch_loop(hr.on_llm_mode_change, knowledge.embed_missing)),
    ]
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
