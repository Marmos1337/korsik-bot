import asyncio

import pytest

from app import db, knowledge, llm, messaging, qa
from app.knowledge import Hit

PVTR = "Правила внутреннего трудового распорядка"
WORK_TIME = (
    "6.6.1. Работникам Организации устанавливается пятидневная рабочая неделя. С понедельника по четверг: "
    "начало работы - 9:00, окончание работы - 18:00. В пятницу окончание работы - 17:00. "
    "Перерыв для отдыха и питания: 13:00 - 13:48."
)
CONTACTS = (
    "Сообщить о нарушении можно на proverka@polyplastic.ru или по телефону +7 (999) 123-45-67. "
    "Подарки дороже 3 000 рублей не принимаются."
)


def run(coro):
    return asyncio.run(coro)


def hit(text, title=PVTR, section="п. 6.6.1", vector=0.64, rank=1, chunk_id=1):
    return Hit(chunk_id=chunk_id, title=title, section=section, text=text, score=0.03, keyword_rank=rank,
               vector_score=vector)


def make_employee(address="ty", tags="GrPP"):
    employee_id = db.execute(
        "INSERT INTO employees (full_name, first_name, start_date, timezone, tags, invite_code, max_user_id,"
        " address, created_at) VALUES ('Смирнова Анна', 'Аня', '2026-11-02', 'Europe/Moscow', ?, 'anna', 500, ?, ?)",
        (tags, address, db.now_iso()),
    )
    return db.query_one("SELECT * FROM employees WHERE id = ?", (employee_id,))


@pytest.fixture
def model(monkeypatch):
    """Ollama is up: chat answers from `replies` in order and keeps every prompt in `calls`."""
    state = {"replies": [], "calls": []}

    async def fake_chat(messages, json_mode=False):
        state["calls"].append(messages)
        return state["replies"].pop(0)

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(llm, "is_up", lambda: True)
    return state


@pytest.fixture
def found(monkeypatch):
    """knowledge.search returns `hits` and keeps the queries it was asked."""
    state = {"hits": [], "queries": []}

    async def fake_search(query, employee_tags, limit=5, use_vectors=True):
        state["queries"].append(query)
        return state["hits"]

    monkeypatch.setattr(knowledge, "search", fake_search)
    return state


@pytest.fixture
def ollama_off(monkeypatch):
    async def down(*args, **kwargs):
        raise llm.LLMUnavailable("off")

    monkeypatch.setattr(llm, "chat", down)
    monkeypatch.setattr(llm, "embed", down)
    monkeypatch.setattr(llm, "is_up", lambda: False)


def test_grounded_checks_numbers_phones_and_emails():
    fragments = [WORK_TIME, CONTACTS]

    assert qa.grounded("С понедельника по четверг с 9:00 до 18:00, в пятницу до 17.", fragments)
    assert qa.grounded("Пиши на proverka@polyplastic.ru или звони 8 999 123-45-67.", fragments)
    assert qa.grounded("Подарки дороже 3000 рублей принимать нельзя, см. п. 6.6.1.", fragments)
    assert qa.grounded("1. Предупреди руководителя.\n2. Напиши на почту.", fragments)  # list numbers are not facts

    assert not qa.grounded("Обед с 13:00 до 14:00.", fragments)
    assert not qa.grounded("Пиши на hr@polyplastic.ru.", fragments)
    assert not qa.grounded("Звони +7 (999) 123-45-68.", fragments)
    assert not qa.grounded("Отпуск длится 31 день.", fragments)


def test_small_talk_gets_a_short_reply_in_the_chosen_register():
    assert qa.small_talk_reply("Спасибо!", "ty") == "Пожалуйста! Если появятся вопросы, пиши."
    assert qa.small_talk_reply("спасибо большое", "vy") == "Пожалуйста! Если появятся вопросы, пишите."
    assert qa.small_talk_reply("Привет", None).startswith("Здравствуйте!")
    assert qa.small_talk_reply("ок", "ty") == "Хорошо 👍"
    assert qa.small_talk_reply("Спасибо, а где столовая?", "ty") is None
    assert qa.small_talk_reply("Во сколько обед?", "ty") is None


def test_question_without_a_close_fragment_is_escalated_without_the_model(model, found):
    employee = make_employee()
    found["hits"] = [hit("6.7.4. Отпуск 28 календарных дней.", section="п. 6.7.4", vector=0.31)]

    result = run(qa.answer_question(employee, "Сколько весит Земля?"))

    assert (result.escalate, result.reason, result.text) == (True, "no_match", None)
    assert model["calls"] == []


def test_answer_comes_from_fragments_with_source_and_register(model, found):
    employee = make_employee(address="vy")
    found["hits"] = [
        hit(WORK_TIME),
        hit("6.7.3. Работник вправе использовать перерыв по своему усмотрению.", section="п. 6.7.3", vector=0.55,
            chunk_id=2),
    ]
    model["replies"] = ["Рабочий день начинается в 9:00.\nИсточник: 1", "ДА"]

    result = run(qa.answer_question(employee, "Во сколько начинается рабочий день?"))

    source = "Правила внутреннего трудового распорядка, п. 6.6.1"
    assert (result.escalate, result.simple_mode) == (False, False)
    # the source is kept for HR, but under the answer only when the newcomer asks for it
    assert result.text == "Рабочий день начинается в 9:00."
    assert result.sources == [source]
    rules, question = model["calls"][0][0]["content"], model["calls"][0][-1]["content"]
    assert "«вы»" in rules and "NO_ANSWER" in rules
    assert WORK_TIME in question and "Во сколько начинается рабочий день?" in question


def test_no_answer_and_unchecked_numbers_are_escalated(model, found):
    employee = make_employee()
    found["hits"] = [hit(WORK_TIME)]

    model["replies"] = ["NO_ANSWER"]
    result = run(qa.answer_question(employee, "Есть ли в офисе парковка?"))
    assert (result.escalate, result.reason, result.text) == (True, "no_answer", None)

    model["replies"] = ["Обед с 13:00 до 14:00.\nИсточник: 1"]
    result = run(qa.answer_question(employee, "Когда обед?"))
    assert (result.escalate, result.reason, result.text) == (True, "unchecked", None)


def test_answer_with_numbers_is_checked_once_more(model, found):
    # live qwen3 gave the lunch break time as the canteen hours: the number is in the fragments, the meaning is not
    employee = make_employee()
    found["hits"] = [hit(WORK_TIME), hit("На площадке GrPP есть столовая.", title="ДЕМО: питание", section="",
                                         chunk_id=2)]
    model["replies"] = ["Столовая работает до 13:48.\nИсточник: 1, 2", "НЕТ"]

    result = run(qa.answer_question(employee, "До скольки работает столовая?"))

    assert (result.escalate, result.reason) == (True, "unchecked")
    check = model["calls"][1][-1]["content"]
    assert "До скольки работает столовая?" in check and "Столовая работает до 13:48." in check
    assert WORK_TIME in check


def test_follow_up_is_rewritten_with_history_before_search(model, found):
    employee = make_employee()
    messaging.log_message(employee["id"], "employee", "Где поесть на площадке?")
    messaging.log_message(employee["id"], "bot", "На площадке GrPP есть столовая.\n\nИсточник: ДЕМО: питание")
    messaging.log_message(employee["id"], "employee", "А до скольки она работает?")
    found["hits"] = [hit("Столовая GrPP работает с 8:00 до 16:00.", title="ДЕМО: питание", section="")]
    model["replies"] = ["До скольки работает столовая на площадке GrPP?", "Столовая работает до 16:00.\nИсточник: 1",
                        "ДА"]

    result = run(qa.answer_question(employee, "А до скольки она работает?"))

    assert found["queries"] == ["До скольки работает столовая на площадке GrPP?"]
    rewrite = model["calls"][0][-1]["content"]
    assert "Где поесть на площадке?" in rewrite and "А до скольки она работает?" in rewrite
    # the bot's own replies stay out of the rewrite, they made the model invent topics
    assert "есть столовая" not in rewrite
    assert "Где поесть на площадке?" in model["calls"][1][-1]["content"]
    assert result.text == "Столовая работает до 16:00."
    check = model["calls"][2][-1]["content"]
    assert "Сотрудник написал: А до скольки она работает?" in check


def test_follow_up_that_lost_its_number_is_searched_as_written(model, found):
    employee = make_employee()
    messaging.log_message(employee["id"], "employee", "Где найти рабочее расписание?")
    messaging.log_message(employee["id"], "bot", "С 9:00 до 18:00. Перерыв на обед с 13:00 до 13:48.")
    found["hits"] = [hit(WORK_TIME)]
    model["replies"] = ["Положен ли перерыв на обед?", "NO_ANSWER"]

    run(qa.answer_question(employee, "А если я работаю 20 часов?"))

    assert found["queries"] == ["А если я работаю 20 часов?"]


def test_follow_up_answer_that_misses_the_point_goes_to_hr(model, found):
    employee = make_employee()
    messaging.log_message(employee["id"], "employee", "Где найти рабочее расписание?")
    found["hits"] = [hit(WORK_TIME)]
    model["replies"] = ["Если я работаю 20 часов, какое у меня расписание?",
                        "Если работаешь меньше четырёх часов, перерыв не положен.\nИсточник: 1", "НЕТ"]

    result = run(qa.answer_question(employee, "А если я работаю 20 часов?"))

    assert (result.escalate, result.reason) == (True, "unchecked")


def test_source_goes_under_the_answer_when_asked_and_on_its_own_request(model, found):
    employee = make_employee()
    found["hits"] = [hit(WORK_TIME)]
    model["replies"] = ["Рабочий день начинается в 9:00.\nИсточник: 1", "ДА"]
    result = run(qa.answer_question(employee, "Где написано, во сколько начинается рабочий день?"))
    assert result.text.endswith("Источник: Правила внутреннего трудового распорядка, п. 6.6.1")

    db.execute("INSERT INTO bot_answers (employee_id, question, answer, sources, created_at) VALUES"
               " (?, 'q', 'a', 'ПВТР, п. 6.6.1', ?)", (employee["id"], db.now_iso()))
    for asked in ("А где это написано?", "Источник?", "Дай ссылку на документ"):
        result = run(qa.answer_question(employee, asked))
        assert (result.reason, result.text) == ("source", "Это из документа: ПВТР, п. 6.6.1"), asked
    assert not qa.source_only("Где написано про отпуск?")


def test_pronouns_after_a_preposition_mark_a_follow_up():
    for question in ("Сколько у него выходных?", "А у неё?", "Можно с ним связаться?", "Что в нём написано?"):
        assert qa.is_follow_up(question), question
    assert not qa.is_follow_up("Сколько дней отпуска?")


def test_question_that_stands_on_its_own_is_searched_as_is(model, found):
    # live qwen3 turned «Где поесть?» into «Где можно поесть в городе?», so only follow-ups are rewritten
    employee = make_employee()
    messaging.log_message(employee["id"], "bot", "Привет! Я Корсик, помогу освоиться в первые месяцы.")
    found["hits"] = [hit("На площадке GrPP есть столовая.", title="ДЕМО: питание", section="")]
    model["replies"] = ["На площадке есть столовая. Источник: 1"]

    result = run(qa.answer_question(employee, "Где поесть?"))

    assert found["queries"] == ["Где поесть?"]
    assert len(model["calls"]) == 1
    assert result.text == "На площадке есть столовая."


def test_stop_topic_goes_to_the_recruiter_at_once(model, found):
    employee = make_employee()

    result = run(qa.answer_question(employee, "Какая у меня будет зарплата?"))

    assert (result.escalate, result.reason) == (True, "stop_topic")
    assert model["calls"] == [] and found["queries"] == []


def test_simple_mode_quotes_only_a_confident_keyword_match(ollama_off, tmp_path):
    path = tmp_path / "pvtr.txt"
    path.write_text("6.6. Рабочее время\n\n" + WORK_TIME + "\n\n6.7.4. Работникам предоставляются ежегодные "
                    "отпуска продолжительностью 28 календарных дней.", encoding="utf-8")
    run(knowledge.add_document(path, PVTR, []))
    employee = make_employee()

    result = run(qa.answer_question(employee, "Какой перерыв для отдыха и питания?"))
    assert (result.escalate, result.simple_mode) == (False, True)
    assert WORK_TIME in result.text
    assert "Источник" not in result.text and result.sources == ["Правила внутреннего трудового распорядка, п. 6.6.1"]
    assert result.text.endswith("Если это не то, напиши «нужен человек».")

    # «начинается» is not in the clause, so a quote could be about something else
    result = run(qa.answer_question(employee, "Во сколько начинается рабочий день?"))
    assert (result.escalate, result.reason, result.simple_mode) == (True, "no_match", True)


def test_model_failing_in_the_middle_falls_back_to_a_quote(monkeypatch, found):
    async def broken_chat(messages, json_mode=False):
        raise llm.LLMUnavailable("connection refused")

    monkeypatch.setattr(llm, "chat", broken_chat)
    monkeypatch.setattr(llm, "is_up", lambda: True)
    found["hits"] = [hit(WORK_TIME)]

    result = run(qa.answer_question(make_employee(), "Какой перерыв для отдыха и питания?"))

    assert (result.escalate, result.simple_mode) == (False, True)
    assert WORK_TIME in result.text


def test_unclear_text_and_request_for_a_human(model, found):
    employee = make_employee()

    result = run(qa.answer_question(employee, "да"))
    assert (result.escalate, result.reason) == (False, "unclear")
    assert result.text.startswith("Не совсем понял вопрос.")

    result = run(qa.answer_question(employee, "Нужен человек"))
    assert (result.escalate, result.reason) == (True, "asked_human")
    assert model["calls"] == [] and found["queries"] == []


def test_refusal_in_words_is_escalated_like_no_answer(model, found):
    employee = make_employee()
    found["hits"] = [hit(WORK_TIME)]

    for reply in ["Во фрагментах нет информации о парковке.\nИсточник: 1",
                  "К сожалению, в документах об этом ничего не сказано.\nИсточник: 1"]:
        model["replies"] = [reply]
        result = run(qa.answer_question(employee, "Есть ли в офисе парковка?"))
        assert (result.escalate, result.reason, result.text) == (True, "no_answer", None)


def test_answer_must_name_the_fragment_it_comes_from(model, found):
    employee = make_employee()
    found["hits"] = [hit(WORK_TIME), hit("6.7.4. Отпуск 28 календарных дней.", section="п. 6.7.4", chunk_id=2)]

    # no number, or a source in words: there is nothing to show and nothing to check against
    for reply in ["Рабочая неделя пятидневная.", "Рабочая неделя пятидневная.\nИсточник: Правила распорядка"]:
        model["replies"] = [reply]
        result = run(qa.answer_question(employee, "Сколько дней в рабочей неделе?"))
        assert (result.escalate, result.reason) == (True, "no_source")

    # 9:00 is in fragment 1, but the answer names fragment 2
    model["replies"] = ["Работа начинается в 9:00.\nИсточник: 2"]
    result = run(qa.answer_question(employee, "Во сколько начинается работа?"))
    assert (result.escalate, result.reason) == (True, "unchecked")
