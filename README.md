# Корсик 2.0

Бот в мессенджере МАКС: ведёт нового сотрудника Группы ПОЛИПЛАСТИК по первым трём месяцам, отвечает на вопросы по документам компании и передаёт рекрутеру то, чего в документах нет. Хакатон «Открой#Моспром», команда УДАВ.

## Запуск

Нужны Python 3.12 и токен бота из кабинета МАКС. Для сканов PDF ещё tesseract с русским языком, для умных ответов Ollama с моделями `qwen3:8b` (или `qwen3:14b`) и `bge-m3`. Без Ollama бот всё равно стартует и отвечает цитатами из документов.

```
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m app.main
```

В `.env` вписать `MAX_BOT_TOKEN`, `ADMIN_PASSWORD` и `SECRET_KEY` (любая длинная случайная строка). Если модель стоит на другом компьютере, там же `OLLAMA_URL` и `LLM_MODEL`.

Кабинет HR открывается на http://127.0.0.1:8080. Сначала добавьте рекрутера и дайте ему открыть свою ссылку в МАКС, потом заведите новичка и отправьте ему ссылку или QR.

Тесты: `.venv/bin/python -m pytest -q`. Они не ходят в сеть.

На сервере бот запускается в Docker: `docker compose up -d --build`. Порт кабинета открыт только на 127.0.0.1, снаружи его отдаёт прокси. Подробности в [docs/deploy.md](docs/deploy.md).

## Из чего сделано

Python 3.12, один процесс: FastAPI и uvicorn для кабинета, рядом два цикла asyncio (приём событий МАКС и планировщик сценария). SQLite с FTS5 для поиска по словам, векторы считает numpy. Модели работают локально через Ollama, наружу ничего не уходит. Сценарий лежит в Excel (`scenario/scenario.xlsx`), документы разбирают pymupdf, python-docx, python-pptx и tesseract.

## Дальше читать

- [docs/how-it-works.md](docs/how-it-works.md) - как всё устроено, простым языком
- [docs/deploy.md](docs/deploy.md) - сервер, домен korsik.udav.team, модель на Windows
- [docs/plan.md](docs/plan.md) - что готово и что осталось до защиты
- [deploy/windows/README.md](deploy/windows/README.md) - компьютер с моделью
