import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import openpyxl
import pytest

from app.scenario import (
    Button, ScenarioError, Step, is_bad_answer, load_holidays, load_scenario, render_text, scale_buttons,
    step_due_at, tags_match, validate_scenario,
)

UTC = timezone.utc
NO_HOLIDAYS: set[date] = set()


def make_step(**kw) -> Step:
    fields = dict(id="1", when="", after=None, tags=[], kind="сообщение", text_ty="Привет",
                  text_vy="", buttons=[], bad_answer="", image="", row=2)
    fields.update(kw)
    return Step(**fields)


def due(when, start, tz="Europe/Moscow", holidays=NO_HOLIDAYS, **kw):
    return step_due_at(make_step(when=when), start, tz, holidays, None, **kw)


# --- timing


def test_working_day_skips_weekend_and_holidays_in_employee_timezone():
    friday = date(2026, 11, 6)
    holidays = {date(2026, 11, 9)}  # Monday made a holiday on purpose
    # р1 = Fri 6, р2 = Tue 10 (weekend and Mon 9 skipped), р3 = Wed 11
    assert due("р3 17:30", friday, holidays=holidays) == datetime(2026, 11, 11, 14, 30, tzinfo=UTC)
    assert due("р3 17:30", friday, "Asia/Novosibirsk", holidays) == datetime(2026, 11, 11, 10, 30, tzinfo=UTC)


def test_first_working_day_is_start_day():
    assert due("р1 10:00", date(2026, 11, 2)) == datetime(2026, 11, 2, 7, 0, tzinfo=UTC)


def test_day_after_start_on_weekend_moves_forward():
    friday = date(2026, 11, 6)
    assert due("+1д 10:00", friday) == datetime(2026, 11, 9, 7, 0, tzinfo=UTC)


def test_day_before_start_on_weekend_moves_back():
    monday = date(2026, 11, 9)
    assert due("-1д 10:00", monday) == datetime(2026, 11, 6, 7, 0, tzinfo=UTC)


def test_holiday_before_start_moves_back():
    # 2026-11-04 is a Wednesday holiday, the step two days before a Friday start lands on it
    friday = date(2026, 11, 6)
    assert due("-2д 12:00", friday, holidays={date(2026, 11, 4)}) == datetime(2026, 11, 3, 9, 0, tzinfo=UTC)


def test_month_plus_days():
    # 2026-01-15 + 1 month = Sun 2026-02-15, + 14 days = Sun 2026-03-01, moved to Mon 03-02
    assert due("м1+14д 10:00", date(2026, 1, 15)) == datetime(2026, 3, 2, 7, 0, tzinfo=UTC)


def test_month_clamps_to_month_end():
    # 2026-01-31 + 1 month = 2026-02-28, a Saturday, so Monday 2026-03-02
    assert due("м1 09:00", date(2026, 1, 31)) == datetime(2026, 3, 2, 6, 0, tzinfo=UTC)


def test_end_of_probation():
    # 2026-11-02 + 3 months - 1 day = Mon 2027-02-01
    assert due("конец 09:00", date(2026, 11, 2)) == datetime(2027, 2, 1, 6, 0, tzinfo=UTC)
    # 2026-11-02 + 6 months - 1 day = Sat 2027-05-01: the probation is over by Monday, so back to Fri 04-30
    assert due("конец 09:00", date(2026, 11, 2), probation_months=6) == datetime(2027, 4, 30, 6, 0, tzinfo=UTC)
    # Thu 2026-10-01 + 3 months - 1 day = Thu 2026-12-31, and the holidays push it back to Wed 12-30
    assert due("конец 09:00", date(2026, 10, 1), holidays={date(2026, 12, 31)}) == datetime(2026, 12, 30, 6, 0,
                                                                                              tzinfo=UTC)


def test_start_is_link_time():
    linked = datetime(2026, 11, 3, 21, 15, tzinfo=ZoneInfo("Asia/Novosibirsk"))
    got = step_due_at(make_step(when="старт"), date(2026, 11, 6), "Asia/Novosibirsk", NO_HOLIDAYS, linked)
    assert got == datetime(2026, 11, 3, 14, 15, tzinfo=UTC)
    assert got.utcoffset() == timedelta(0)


def test_start_before_linking_has_no_time():
    assert due("старт", date(2026, 11, 6)) is None


def test_button_step_has_no_time():
    step = make_step(when="", after=("25", "Да!"))
    assert step_due_at(step, date(2026, 11, 6), "Europe/Moscow", NO_HOLIDAYS, datetime.now(UTC)) is None


# --- tags


def test_tags_match():
    assert tags_match([], [])
    assert tags_match([], ["GTP"])
    assert tags_match(["GrPP", "Luzhniki"], ["luzhniki"])
    assert not tags_match(["GrPP", "Luzhniki"], ["GTP"])
    assert not tags_match(["GrPP"], [])
    assert tags_match(["-GTP"], ["GrPP"])
    assert tags_match(["-GTP"], [])
    assert not tags_match(["-GTP"], ["gtp", "офис"])
    assert not tags_match(["GrPP", "-офис"], ["GrPP", "Офис"])


# --- texts


def test_render_text_picks_register_and_fills_name():
    step = make_step(text_ty="Привет, {имя}! Как ты?", text_vy="Здравствуйте, {имя}! Как вы?")
    assert render_text(step, "ty", "Аня") == "Привет, Аня! Как ты?"
    assert render_text(step, "vy", "Аня") == "Здравствуйте, Аня! Как вы?"


def test_render_text_without_vy_version_uses_ty():
    step = make_step(text_ty="Спасибо за ответы!", text_vy="")
    assert render_text(step, "vy", "Аня") == "Спасибо за ответы!"


def test_render_text_before_choice_is_vy():
    # spec 4.4: until the employee picks, the bot talks on "вы"
    step = make_step(text_ty="Как ты?", text_vy="Как вы?")
    assert render_text(step, None, "Аня") == "Как вы?"


def test_render_text_drops_hr_notes():
    step = make_step(text_ty="О компании.\n\n[HR: добавить факты]\n\nЗдесь ценят инициативу.\n[HR: число площадок]")
    assert render_text(step, "ty", "Аня") == "О компании.\n\nЗдесь ценят инициативу."


# --- answers


def test_bad_answer_by_score():
    step = make_step(kind="шкала", bad_answer="<8")
    assert is_bad_answer(step, "7", 7)
    assert not is_bad_answer(step, "8", 8)
    assert is_bad_answer(step, "1", None)


def test_bad_answer_by_option():
    step = make_step(kind="опрос", buttons=[Button("Всё по плану"), Button("Сильно отстаю"), Button("Нет")],
                     bad_answer="Сильно отстаю | Нет")
    assert is_bad_answer(step, "Сильно отстаю", None)
    assert is_bad_answer(step, "нет", None)
    assert not is_bad_answer(step, "Всё по плану", None)


def test_no_rule_means_never_bad():
    assert not is_bad_answer(make_step(kind="шкала"), "1", 1)


def test_scale_buttons():
    rows = scale_buttons()
    assert [[b.text for b in row] for row in rows] == [["1", "2", "3", "4", "5"], ["6", "7", "8", "9", "10"]]


# --- holidays


def test_load_holidays(tmp_path):
    path = tmp_path / "holidays.txt"
    path.write_text("# сверить с производственным календарём\n2026-01-01\n\n2026-05-09  # День Победы\n",
                    encoding="utf-8")
    assert load_holidays(path) == {date(2026, 1, 1), date(2026, 5, 9)}


def test_missing_holidays_file_is_empty(tmp_path):
    assert load_holidays(tmp_path / "nope.txt") == set()


# --- scenario file

HEADERS = ["Шаг", "Когда", "После", "Теги", "Тип", "Текст на «ты»", "Текст на «вы»", "Кнопки", "Плохой ответ",
           "Картинка", "Комментарий"]
REAL_SCENARIO = Path(__file__).resolve().parent.parent / "scenario" / "scenario.xlsx"


def write_xlsx(path, rows, sheet="Сценарий"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(HEADERS)
    for row in rows:
        ws.append(list(row) + [""] * (len(HEADERS) - len(row)))
    wb.save(path)
    return path


GOOD_ROWS = [
    ("1", "старт", "", "", "обращение", "Как общаться?", "", "На «ты» | На «вы»"),
    ("2", "р1 12:30", "", "GrPP, Luzhniki", "вопрос", "Подсказать, где пообедать?", "", "Да! | Нет, спасибо",
     "", "img_05.png"),
    ("2.1", "", "2: Да!", "GTP", "сообщение", "Вот кафе рядом"),
    (25, "+14д 10:00", "", "-GTP", "шкала", "Оцени, {имя}", "Оцените, {имя}", "", "<8"),
    ("26", "р1 13:00", "", "", "сообщение", "Справочник коллег", "", "Справочник = https://example.ru/book"),
    ("27", "напоминание", "", "", "сообщение", "Кажется, опрос остался незаконченным"),
]


def test_load_scenario_reads_steps(tmp_path):
    steps = load_scenario(write_xlsx(tmp_path / "s.xlsx", GOOD_ROWS))
    assert [s.id for s in steps] == ["1", "2", "2.1", "25", "26", "27"]
    assert [s.row for s in steps] == [2, 3, 4, 5, 6, 7]
    first, lunch, cafe, scale, book, _ = steps
    assert [b.text for b in first.buttons] == ["На «ты»", "На «вы»"]
    assert lunch.tags == ["GrPP", "Luzhniki"] and lunch.image == "img_05.png"
    assert cafe.after == ("2", "Да!") and cafe.when == ""
    assert scale.kind == "шкала" and scale.bad_answer == "<8" and scale.text_vy == "Оцените, {имя}"
    assert book.buttons == [Button("Справочник", "https://example.ru/book")]


def test_validate_good_file_has_no_problems(tmp_path):
    assert validate_scenario(write_xlsx(tmp_path / "s.xlsx", GOOD_ROWS)) == []


BROKEN_ROWS = [
    ("1", "р1 10:00", "", "", "вопрос", "Всё на месте?", "", "Да | Нет"),       # row 2, fine
    ("2", "р1 11:00", "", "", "картинка", "Неизвестный тип"),                    # row 3
    ("3", "послезавтра", "", "", "сообщение", "Нечитаемое «Когда»"),            # row 4
    ("4", "", "99: Да", "", "сообщение", "После несуществующего шага"),          # row 5
    ("5", "", "1: Может быть", "", "сообщение", "После несуществующей кнопки"), # row 6
    ("6", "р2 10:00", "", "", "сообщение", "я" * 4001),                         # row 7
    ("1", "р2 11:00", "", "", "сообщение", "Повтор id"),                        # row 8
    ("7", "", "", "", "сообщение", "Ни времени, ни кнопки"),                     # row 9
]


def test_validate_finds_each_broken_row(tmp_path):
    problems = validate_scenario(write_xlsx(tmp_path / "s.xlsx", BROKEN_ROWS))
    rows_with_problems = {int(n) for p in problems for n in re.findall(r"строка (\d+)", p)}
    assert rows_with_problems == {3, 4, 5, 6, 7, 8, 9}
    text = "\n".join(problems)
    for word in ("картинка", "послезавтра", "99", "Может быть", "4000", "повтор"):
        assert word in text


def test_load_scenario_raises_with_all_problems(tmp_path):
    with pytest.raises(ScenarioError) as err:
        load_scenario(write_xlsx(tmp_path / "s.xlsx", BROKEN_ROWS))
    assert len(err.value.problems) >= 7


def test_validate_checks_buttons_and_bad_answers(tmp_path):
    rows = [
        ("1", "старт", "", "", "обращение", "Как общаться?", "", "Только на «ты»"),          # row 2
        ("2", "р1 10:00", "", "", "вопрос", "Вопрос без кнопок"),                            # row 3
        ("3", "р1 11:00", "", "", "опрос", "Как дела?", "", "Хорошо | Плохо", "Ужасно"),    # row 4
        ("4", "р1 12:00", "", "", "шкала", "Оцени", "", "", "меньше восьми"),               # row 5
        ("5", "р1 13:00", "", "", "сообщение", "Ссылка", "", "Сайт = example.ru"),         # row 6
        ("6", "р1 14:00", "", "", "сообщение", "[HR: только заметка]"),                     # row 7
    ]
    problems = validate_scenario(write_xlsx(tmp_path / "s.xlsx", rows))
    rows_with_problems = {int(n) for p in problems for n in re.findall(r"строка (\d+)", p)}
    assert rows_with_problems == {2, 3, 4, 5, 6, 7}


def test_validate_reports_unreadable_file(tmp_path):
    bad = tmp_path / "s.xlsx"
    bad.write_text("not a workbook", encoding="utf-8")
    assert validate_scenario(bad)
    assert validate_scenario(write_xlsx(tmp_path / "other.xlsx", GOOD_ROWS, sheet="Лист1"))


def test_real_scenario_is_valid_and_complete():
    assert validate_scenario(REAL_SCENARIO) == []
    steps = load_scenario(REAL_SCENARIO)
    source_rows = [int(s.id.split(".")[0]) for s in steps]
    # source rows 3 (bot description), 6 (buttons of row 5) and 19 (HR letter) are not separate steps
    assert set(source_rows) == set(range(4, 71)) - {6, 19}
    assert source_rows == sorted(source_rows)
    assert [s.when for s in steps if s.when in ("напоминание", "эскалация")] == ["эскалация", "напоминание"]


def test_real_scenario_promises_only_what_the_bot_does():
    # the bot has no /food or /hr commands, does not show drafts, cannot take photos and sends no files
    texts = [text for s in load_scenario(REAL_SCENARIO) for text in (s.text_ty, s.text_vy)]
    for promise in ("/food", "/hr", "перед публикацией", "Вот он.", "пришли фото", "пришлите фото"):
        assert not [t for t in texts if promise in t], promise


def test_real_holidays_file():
    holidays = load_holidays(REAL_SCENARIO.parent / "holidays.txt")
    assert {date(2026, 1, 1), date(2026, 11, 4), date(2027, 1, 8), date(2027, 6, 12)} <= holidays
    assert {d.year for d in holidays} == {2026, 2027}
