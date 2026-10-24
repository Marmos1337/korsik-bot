import logging
import re
from dataclasses import dataclass

from app import db, knowledge, llm, messaging
from app.config import cfg
from app.engine import PHRASES

log = logging.getLogger(__name__)

# Measured with bge-m3 on the real base (ПВТР, three policies, demo food files): questions the documents do not
# cover at all (the weight of the Earth, the capital of France, the CEO's name, wi-fi) scored 0.26-0.44, real
# questions 0.49 and up. Between them sit company questions without an answer (parking, dress code, 0.47-0.52):
# they reach the model, and the model says NO_ANSWER.
MIN_SIMILARITY = 0.45
HISTORY_SIZE = 6
NO_ANSWER = "NO_ANSWER"

NUMBER = re.compile(r"\d+(?:[ \u00a0]\d{3}(?!\d))*(?:[.,:]\d+)*")  # 3 000, 9:00, 6.6.1
PHONE = re.compile(r"\+?\d[\d\s()\-]{8,}\d")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
LIST_NUMBER = re.compile(r"(?m)^\s*\d+[.)]\s")
SOURCE_TAIL = re.compile(r"[(\[]?\s*источник\w*\s*:?\s*(?:фрагмент\w*)?[\d\s,;.и\[\]]*\)?\s*$", re.IGNORECASE)

# asked to rewrite every question, qwen3:8b adds things («Где поесть?» -> «Где можно поесть в городе?»),
# asked to be careful, it rewrites nothing; so the code decides which question needs the history
FOLLOW_UP_WORDS = {"он", "она", "оно", "они", "его", "ее", "их", "ему", "ей", "им", "него", "нее", "нему", "ней",
                   "ним", "нем", "них", "ними",
                   "это", "этот", "эта", "эти", "этого", "этой", "этом", "там", "туда", "тогда"}
# instead of NO_ANSWER the model sometimes says it in words: «во фрагментах нет информации о ...»
REFUSALS = ("нет информации", "нет данных", "нет сведений", "нет ответа", "информации нет", "информация отсутствует",
            "не содержит", "не содержится", "не указан", "не упомина", "не сказано", "не говорится", "не приводится",
            "не нашел", "не найден", "не могу ответить", "не могу сказать", "к сожалению")
# question words that say nothing about the topic; the rest of the list lives in knowledge
EXTRA_STOP_WORDS = {"такое", "такой", "такая", "такие", "будет", "мой", "моя", "мое", "мои", "могу", "может"}

SMALL_TALK = {
    "small_thanks": {"спасибо", "спс", "благодарю", "пасиб", "спасибки"},
    "small_hello": {"привет", "здравствуй", "здравствуйте", "добрый", "доброе", "приветик", "хай"},
    "small_how": {"дела"},
    "small_bye": {"пока", "свидания"},
    "small_ok": {"ок", "окей", "ok", "хорошо", "понятно", "ясно", "понял", "поняла", "отлично", "супер", "класс",
                 "круто", "ага", "угу", "договорились"},
}
# words that may come along without making the text a question
SMALL_TALK_FILLER = {"большое", "огромное", "тебе", "вам", "день", "утро", "вечер", "как", "до", "все", "очень"}

ANSWER_RULES = """Ответь новому сотруднику компании на вопрос о работе. Используй только фрагменты документов, которые придут вместе с вопросом.

Правила:
- Бери факты только из фрагментов. Ничего не додумывай и не добавляй из общих знаний.
- Отвечай ровно на заданный вопрос с его условиями (часы, дни, суммы). Не отвечай на соседний вопрос, даже если о нём есть во фрагментах.
- Если во фрагментах нет ответа именно на этот вопрос, напиши только NO_ANSWER.
- Обращайся к сотруднику на «{address}».
- Пиши коротко и просто, обычным текстом без разметки.
- Последней строкой напиши «Источник:» и номера фрагментов, из которых взят ответ, например: Источник: 2"""

CHECK_PROMPT = """Проверь ответ бота по фрагментам документов.

Фрагменты:
{fragments}

Сотрудник написал: {asked}
Вопрос целиком: {question}
Ответ бота: {answer}

Ответ верный, только если каждое его утверждение прямо написано во фрагментах и отвечает именно на то, что написал сотрудник, с его условиями. Если бот что-то додумал, перенёс сведения об одном на другое, ответил не на тот вопрос или пропустил условие из вопроса, ответ неверный. Напиши только ДА, если ответ верный, или НЕТ."""

REWRITE_PROMPT = """Перепиши последний вопрос сотрудника так, чтобы он был понятен без его прошлых вопросов: подставь только тему, о которой он спрашивал раньше. Сохрани все условия и числа последнего вопроса. Не добавляй ничего, о чём сотрудник не спрашивал. Если вопрос и так понятен, верни его без изменений. Ответь только вопросом.

Прошлые вопросы сотрудника:
{dialog}

Последний вопрос: {question}"""


@dataclass
class Answer:
    text: str | None
    sources: list[str]
    escalate: bool
    # small_talk | unclear | answered | stop_topic | no_match | no_answer | no_source | unchecked | asked_human
    reason: str
    simple_mode: bool


async def answer_question(employee, question: str) -> Answer:
    question = question.strip()
    address = employee["address"]
    reply = small_talk_reply(question, address)
    if reply:
        return Answer(reply, [], False, "small_talk", False)
    if wants_human(question):
        return _escalation("asked_human")
    if source_only(question):
        last = db.query_one("SELECT sources FROM bot_answers WHERE employee_id = ? AND sources != ''"
                            " ORDER BY id DESC LIMIT 1", (employee["id"],))
        if last:
            return Answer(_phrase(address, "source_is") + " " + last["sources"], [], False, "source", False)
    if is_stop_topic(question):
        return _escalation("stop_topic")
    if not _terms(question):
        return Answer(_phrase(address, "unclear"), [], False, "unclear", False)

    tags = db.split_tags(employee["tags"])
    if llm.is_up():
        try:
            return await _answer_by_model(employee, question, tags)
        except llm.LLMUnavailable:
            log.info("employee %s: the model went away while answering, quoting instead", employee["id"])
    return await _answer_by_quote(question, tags, address)


async def _answer_by_model(employee, question: str, tags: list[str]) -> Answer:
    asked = question
    dialog = _dialog(employee["id"], question)
    follow_up = bool(dialog) and is_follow_up(question)
    if follow_up:
        question = await _rewrite(question, dialog)
        if is_stop_topic(question):
            return _escalation("stop_topic")

    hits = await knowledge.search(question, tags)
    if not llm.is_up():
        # search swallows a failed embedding call and goes on by keywords; answer the way the simple mode does
        raise llm.LLMUnavailable("vector search failed")
    best = max((hit.vector_score or 0 for hit in hits), default=0)
    if best < MIN_SIMILARITY:
        log.info("employee %s: best fragment %.2f is below %.2f", employee["id"], best, MIN_SIMILARITY)
        return _escalation("no_match")

    fragments = [f"{_source(hit)}\n{hit.text}" for hit in hits]
    address = "ты" if employee["address"] == "ty" else "вы"
    numbered = "\n\n".join(f"[{i}] {fragment}" for i, fragment in enumerate(fragments, 1))
    request = f"Фрагменты документов:\n\n{numbered}\n\n"
    if dialog:
        request += f"Переписка до вопроса:\n{_dialog_text(dialog)}\n\n"
    request += f"Вопрос: {question}"
    reply = await llm.chat([
        {"role": "system", "content": ANSWER_RULES.format(address=address)},
        {"role": "user", "content": request},
    ])

    if not reply or NO_ANSWER in reply.upper():
        return _escalation("no_answer")
    body, cited = _split_source(reply, len(hits))
    if not body or _refuses(body):
        return _escalation("no_answer")
    if not cited:
        # without a fragment number there is no honest source to show and nothing to check the answer against
        log.info("employee %s: the answer names no fragment", employee["id"])
        return _escalation("no_source")
    if not grounded(body, [fragments[i] for i in cited]):
        log.info("employee %s: the answer has facts that are not in its fragments", employee["id"])
        return _escalation("unchecked")
    # a number can be in the fragments and still be about something else (the lunch break is not the canteen
    # hours), so an answer with numbers or contacts gets a second look; on answers without them the check
    # turned down correct lists too often to be worth it
    # a follow-up is always checked: the rewritten question may have drifted from what was asked
    if (_has_facts(body) or follow_up) and not await _confirmed(asked, question, body, numbered):
        log.info("employee %s: the check did not confirm the answer", employee["id"])
        return _escalation("unchecked")
    sources = list(dict.fromkeys(_source(hits[i]) for i in cited))
    return Answer(with_sources(body, sources, asked), sources, False, "answered", False)


async def _answer_by_quote(question: str, tags: list[str], address: str | None) -> Answer:
    hits = await knowledge.search(question, tags, use_vectors=False)
    hit = _confident_hit(question, hits)
    if hit is None:
        return _escalation("no_match", simple_mode=True)
    text = "\n\n".join([_phrase(address, "found"), with_sources(hit.text, [_source(hit)], question),
                          _phrase(address, "simple_quote")])
    return Answer(text, [_source(hit)], False, "answered", True)


def _confident_hit(question: str, hits: list[knowledge.Hit]) -> knowledge.Hit | None:
    """The first keyword hit that has every word of the question. Without the model a partial match is a guess."""
    terms = _terms(question)
    if len(terms) < 2:
        return None
    for hit in hits:
        words = _words(f"{hit.title} {hit.text}")
        if all(any(word.startswith(term) for word in words) for term in terms):
            return hit
    return None


async def _confirmed(asked: str, question: str, answer: str, fragments: str) -> bool:
    prompt = CHECK_PROMPT.format(fragments=fragments, asked=asked, question=question, answer=answer)
    verdict = await llm.chat([{"role": "user", "content": prompt}])
    return verdict.strip().upper().startswith("ДА")


async def _rewrite(question: str, dialog: list[dict]) -> str:
    # only the newcomer's own words: given the bot's replies, the model picked a topic from them that nobody asked
    # about («а если я работаю 20 часов?» became a question about the lunch break)
    own = [m for m in dialog if m["role"] == "employee"]
    if not own:
        return question
    prompt = REWRITE_PROMPT.format(dialog=_dialog_text(own), question=question)
    rewritten = (await llm.chat([{"role": "user", "content": prompt}])).strip().strip("«»\"")
    # a runaway answer instead of a question is worse than the short original
    if not rewritten or len(rewritten) > 3 * len(question) + 200:
        return question
    # a number the newcomer gave (20 hours, 3 days) must survive the rewrite, or it is another question
    if not _numbers(question) <= _numbers(rewritten):
        return question
    return rewritten


SOURCE_WORDS = ("источник", "где написано", "где это написано", "откуда", "ссылк", "какой документ",
                "каком документ", "какой пункт", "каком пункте", "пункт", "подтверд", "документ")


def wants_source(text: str) -> bool:
    lowered = text.lower().replace("ё", "е")
    return any(word in lowered for word in SOURCE_WORDS)


def with_sources(body: str, sources: list[str], asked: str) -> str:
    """The document goes under the answer only when the newcomer asks where it is written."""
    if not sources or not wants_source(asked):
        return body
    label = "Источник" if len(sources) == 1 else "Источники"
    return f"{body}\n\n{label}: {'; '.join(sources)}"


SOURCE_STEMS = ("источ", "напис", "откуд", "ссылк", "докум", "пункт", "подтв", "каком", "какой", "дай", "покаж",
                "скин", "можн", "это", "где", "взял", "инфо")


def source_only(text: str) -> bool:
    """«А где это написано?», «источник?»: about the last answer, not a new question."""
    if not wants_source(text) or len(_words(text)) > 6:
        return False
    return all(any(term.startswith(stem) or stem.startswith(term) for stem in SOURCE_STEMS) for term in _terms(text))


def is_follow_up(question: str) -> bool:
    """«А в пятницу?», «до скольки она работает?»: the question leans on what was said before."""
    words = _words(question)
    return bool(words) and (words[0] in ("а", "и", "но") or bool(FOLLOW_UP_WORDS & set(words)))


def _dialog(employee_id: int, question: str) -> list[dict]:
    """Last replies before the question. The bot logs the question before answering it, so it is cut off here."""
    dialog = messaging.history(employee_id, HISTORY_SIZE + 1)
    if dialog and dialog[-1]["role"] == "employee" and dialog[-1]["text"].strip() == question:
        dialog = dialog[:-1]
    return dialog[-HISTORY_SIZE:]


def _dialog_text(dialog: list[dict]) -> str:
    lines = []
    for message in dialog:
        who = "Сотрудник" if message["role"] == "employee" else "Бот"
        # old source lines and mode notes only distract the model
        text = "\n".join(line for line in message["text"].splitlines()
                         if not line.startswith(("Источник", "⚙️")))
        lines.append(f"{who}: {text.strip()[:600]}")
    return "\n".join(lines)


def _split_source(reply: str, count: int) -> tuple[str, list[int]]:
    """Cuts off «Источник: 1, 3» (on its own line or at the end of the last one) and returns the fragment indexes."""
    # the dialog in the prompt is labelled «Бот:», and the model sometimes signs its answer the same way
    text = re.sub(r"^(Бот|Корсик)\s*:\s*", "", reply.replace("**", "").strip())
    match = SOURCE_TAIL.search(text)
    if match:
        # «6.6.1» is a clause number, not a fragment number
        numbers = re.findall(r"(?<![\d.])\d+(?![\d.])", match.group())
        return text[: match.start()].strip(), sorted({int(n) - 1 for n in numbers if 1 <= int(n) <= count})
    lines = text.splitlines()
    if lines and re.match(r"\W*источник", lines[-1], re.IGNORECASE):
        return "\n".join(lines[:-1]).strip(), []  # a source named in words: nothing to map it to
    return text, []


def _source(hit: knowledge.Hit) -> str:
    return f"{hit.title}, {hit.section}" if hit.section else hit.title


def _escalation(reason: str, simple_mode: bool = False) -> Answer:
    return Answer(None, [], True, reason, simple_mode or not llm.is_up())


def grounded(answer: str, fragments: list[str]) -> bool:
    """Numbers, phones and emails of the answer must all be in the fragments, otherwise the model made them up."""
    found = "\n".join(fragments)
    emails = {email.lower() for email in EMAIL.findall(found)}
    if any(email.lower() not in emails for email in EMAIL.findall(answer)):
        return False
    phones = {_phone(phone) for phone in PHONE.findall(found)}
    if any(_phone(phone) not in phones for phone in PHONE.findall(answer)):
        return False
    rest = PHONE.sub(" ", EMAIL.sub(" ", LIST_NUMBER.sub("", answer)))
    numbers = _numbers(found)
    return all(number in numbers for number in _numbers(rest))


def _refuses(text: str) -> bool:
    lowered = text.lower().replace("ё", "е")
    return any(phrase in lowered for phrase in REFUSALS)


def _has_facts(text: str) -> bool:
    return bool(NUMBER.search(LIST_NUMBER.sub("", text)) or EMAIL.search(text))


def _phone(text: str) -> str:
    # +7 and 8 at the start are the same number
    return re.sub(r"\D", "", text)[-10:]


def _numbers(text: str) -> set[str]:
    numbers = set()
    for raw in NUMBER.findall(text):
        parts = re.split(r"[.,:]", re.sub(r"[ \u00a0]", "", raw))
        parts[0] = parts[0].lstrip("0") or "0"
        # «до 18» and «до 18:00» say the same
        if len(parts) == 2 and parts[1] == "00":
            parts = parts[:1]
        numbers.add(".".join(parts))
    return numbers


def is_stop_topic(text: str) -> bool:
    lowered = text.lower()
    return any(topic in lowered for topic in cfg.STOP_TOPICS)


def wants_human(text: str) -> bool:
    words = set(_words(text))
    return any(w.startswith("человек") for w in words) and bool(
        words & {"нужен", "нужна", "позови", "позовите", "живой", "хочу", "дайте", "соедини", "соедините"}
    )


def small_talk_reply(text: str, address: str | None) -> str | None:
    words = _words(text)
    if not words or len(words) > 5:
        return None
    known = set().union(*SMALL_TALK.values())
    if any(word not in known and word not in SMALL_TALK_FILLER for word in words):
        return None
    for key, group in SMALL_TALK.items():
        if group & set(words):
            return _phrase(address, key)
    return None


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text.lower().replace("ё", "е"))


def _terms(text: str) -> list[str]:
    """Word stems the way the keyword search cuts them: «отпуска» -> «отпус»."""
    stop = knowledge.STOP_WORDS | EXTRA_STOP_WORDS
    terms = [w[: max(4, len(w) - 2)] for w in _words(text) if len(w) >= 3 and not w.isdigit() and w not in stop]
    return list(dict.fromkeys(terms))


def _phrase(address: str | None, key: str) -> str:
    ty, vy = PHRASES[key]
    return ty if address == "ty" else vy
