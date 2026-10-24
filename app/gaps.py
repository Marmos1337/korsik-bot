"""What the knowledge base is missing: questions the bot passed to HR, grouped by meaning."""
import logging
import re
from datetime import datetime, timedelta, timezone

import numpy as np

from app import db, llm

log = logging.getLogger(__name__)

WINDOW = timedelta(days=60)
SAME_MEANING = 0.78  # bge-m3 cosine; questions about the same thing in other words land around 0.8-0.9
SAME_WORDS = 0.5  # without the model: share of common word stems
TOP = 8
WORD_RE = re.compile(r"[а-яёa-z0-9]{4,}")


async def find(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    since = (now - WINDOW).isoformat(timespec="seconds")
    # stop topics (salary, dismissal) go to people on purpose, they are not a hole in the documents
    rows = db.query("SELECT id, question, status, created_at FROM escalations WHERE created_at >= ?"
                    " AND reason != 'stop_topic' ORDER BY id", (since,))
    if not rows:
        return []
    vectors = await _vectors([r["question"] for r in rows]) if llm.is_up() else None
    groups: list[dict] = []
    for i, row in enumerate(rows):
        home = None
        for group in groups:
            if vectors is not None:
                center = group["sum"] / np.linalg.norm(group["sum"])
                if float(vectors[i] @ center) >= SAME_MEANING:
                    home = group
                    break
            elif _overlap(_stems(row["question"]), group["stems"]) >= SAME_WORDS:
                home = group
                break
        if home is None:
            home = {"rows": [], "sum": np.zeros_like(vectors[i]) if vectors is not None else None, "stems": set()}
            groups.append(home)
        home["rows"].append(row)
        if vectors is not None:
            home["sum"] = home["sum"] + vectors[i]
        else:
            home["stems"] |= _stems(row["question"])
    result = []
    for group in groups:
        questions = [r["question"] for r in group["rows"]]
        in_base = any(db.kv_get(f"hr_kb:{r['id']}") for r in group["rows"])
        result.append({"label": min(questions, key=len), "count": len(questions), "questions": questions[-5:],
                       "open": sum(1 for r in group["rows"] if r["status"] == "open"), "in_base": in_base,
                       "last": group["rows"][-1]["created_at"]})
    # the biggest holes first, the newest among equals; those HR already covered sink down
    result.sort(key=lambda g: g["last"], reverse=True)
    result.sort(key=lambda g: (g["in_base"], -g["count"]))
    return result[:TOP]


async def _vectors(questions: list[str]) -> np.ndarray | None:
    known = {}
    for q in set(questions):
        row = db.query_one("SELECT embedding FROM question_vectors WHERE question = ?", (q,))
        if row:
            known[q] = np.frombuffer(row["embedding"], dtype=np.float32)
    missing = [q for q in dict.fromkeys(questions) if q not in known]
    if missing:
        try:
            fresh = await llm.embed(missing, mark_down=False)
        except Exception as e:  # the page still shows the groups by words
            log.warning("question vectors failed: %s", e)
            return None
        for q, vector in zip(missing, fresh):
            vector = np.asarray(vector, dtype=np.float32)
            known[q] = vector
            db.execute("INSERT OR IGNORE INTO question_vectors (question, embedding) VALUES (?, ?)",
                       (q, vector.tobytes()))
    matrix = np.vstack([known[q] for q in questions])
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(norms == 0, 1, norms)


def _stems(text: str) -> set[str]:
    return {w[:5] for w in WORD_RE.findall(text.lower())}


def _overlap(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0
