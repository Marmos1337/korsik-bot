"""Short lessons on the work rules and safety, each with one question to check it was read."""
import json
import logging
from pathlib import Path

from app import db, tasks
from app.config import cfg
from app.scenario import tags_match

log = logging.getLogger(__name__)

SEED = Path(cfg.SCENARIO_PATH).parent / "learning.json"
# the scenario task that counts as done when every lesson is passed
TASK_MARK = "обучение"


def seed() -> None:
    """The first start fills the lessons from scenario/learning.json; after that HR edits them in the cabinet."""
    if db.query_one("SELECT 1 FROM lessons") or not SEED.is_file():
        return
    for position, item in enumerate(json.loads(SEED.read_text(encoding="utf-8"))):
        save(None, item["title"], item["body"], item.get("source", ""), item.get("tags", ""), position,
             item.get("question", ""), item.get("options", []), item.get("correct", 0))
    log.info("lessons seeded from %s", SEED.name)


def save(lesson_id: int | None, title: str, body: str, source: str, tags: str, position: int, question: str,
         options: list[str], correct: int) -> int:
    title, question = " ".join(title.split()), " ".join(question.split())
    options = [" ".join(o.split()) for o in options if o.strip()]
    if not title or not body.strip():
        raise ValueError("У урока нужны название и текст.")
    if question and len(options) < 2:
        raise ValueError("У вопроса нужно хотя бы два варианта ответа.")
    if question and not 0 <= correct < len(options):
        raise ValueError("Отметьте правильный вариант.")
    values = (title, body.strip(), source.strip(), ", ".join(db.split_tags(tags)), position, question,
              "\n".join(options), correct)
    if lesson_id is None:
        return db.execute("INSERT INTO lessons (title, body, source, tags, position, question, options, correct,"
                          " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (*values, db.now_iso()))
    db.execute("UPDATE lessons SET title = ?, body = ?, source = ?, tags = ?, position = ?, question = ?, options = ?,"
               " correct = ? WHERE id = ?", (*values, lesson_id))
    return lesson_id


def all_lessons() -> list:
    return db.query("SELECT * FROM lessons ORDER BY position, id")


def for_employee(employee) -> list[dict]:
    done = {r["lesson_id"]: r for r in db.query("SELECT * FROM lesson_progress WHERE employee_id = ?",
                                                 (employee["id"],))}
    tags = db.split_tags(employee["tags"])
    return [{"lesson": lesson, "options": lesson["options"].split("\n") if lesson["options"] else [],
             "done": lesson["id"] in done}
            for lesson in all_lessons() if tags_match(db.split_tags(lesson["tags"]), tags)]


def answer(employee, lesson_id: int, choice: int | None) -> bool:
    """True when the lesson counts as passed: the right option, or the lesson has no question."""
    lesson = next((item for item in for_employee(employee) if item["lesson"]["id"] == lesson_id), None)
    if lesson is None:
        raise ValueError("Такого урока нет.")
    right = not lesson["options"] or choice == lesson["lesson"]["correct"]
    if not right:
        return False
    db.execute("INSERT OR IGNORE INTO lesson_progress (employee_id, lesson_id, done_at, correct) VALUES (?, ?, ?, 1)",
               (employee["id"], lesson_id, db.now_iso()))
    if all(item["done"] or item["lesson"]["id"] == lesson_id for item in for_employee(employee)):
        for task in db.query("SELECT id, title FROM tasks WHERE employee_id = ? AND step_id IS NOT NULL"
                             " AND done_at IS NULL", (employee["id"],)):
            if TASK_MARK in task["title"].lower():
                tasks.set_done(task["id"], employee["id"], True, "обучение пройдено")
    return True


def progress() -> list:
    """Who passed how many lessons, for HR."""
    total = len(all_lessons())
    rows = db.query("SELECT e.id, e.full_name, COUNT(p.lesson_id) AS done FROM employees e"
                    " LEFT JOIN lesson_progress p ON p.employee_id = e.id WHERE e.max_user_id IS NOT NULL"
                    " GROUP BY e.id ORDER BY e.full_name")
    return [{"id": r["id"], "name": r["full_name"], "done": r["done"], "total": total} for r in rows]
