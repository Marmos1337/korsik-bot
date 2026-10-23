"""Build scenario/scenario.xlsx from the case workbook and our rewritten texts.

    python -I tools/build_scenario.py SOURCE.xlsx korsik_texts.py [OUT.xlsx]

SOURCE.xlsx is the case file "Финал. ТЗ. Чат-бот Корсик- 15.09.xlsx" (first sheet),
korsik_texts.py keeps the rewritten "ты"/"вы" texts under the same row numbers.
Step ids are source row numbers, so every step can be traced back to the case file.
The script is for the first build: after that HR edits the xlsx by hand.
"""
import argparse
import importlib.util
import re
import sys
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.scenario import validate_scenario  # noqa: E402

HEADERS = ["Шаг", "Когда", "После", "Теги", "Тип", "Текст на «ты»", "Текст на «вы»", "Кнопки", "Плохой ответ",
           "Картинка", "Комментарий"]
WIDTHS = [7, 14, 26, 15, 11, 60, 60, 32, 14, 13, 55]

# Attachments the bot can't send by itself stay in the text as notes for HR.
# Every other service note in brackets is a button, card or link that moves to its own column.
HR_ATTACHMENTS = ("приложить PDF", "приложить корпоративную презентацию", "приложить адаптационный план")
# korsik_texts writes buttons and survey options as list lines that start with an em dash
ITEM = "\u2014 "
YES_NO = "\u2014 Да / Нет"

NO_FOLLOW_UP = ("Что прислать после «да», в исходнике нет, поэтому шаг без кнопок: ответ новичка разберёт ИИ "
                "по базе знаний или передаст рекрутеру. Когда HR даст материалы, сделать кнопки и ветку.")
FROM_57 = "Текст из строки 57 исходника: ответ стоит сразу после кнопки, а не после всего опроса."
OWN_TEXT = "Текст наш: в korsik_texts его нет."
WRITE_IN = ("Напиши, пожалуйста, своими словами.", "Напишите, пожалуйста, своими словами.")

# korsik_texts uses em dashes, the bot texts do not: a dash becomes a colon or a comma where the phrase asks
# for one (the words before the dash are listed), and a hyphen everywhere else
DASH = "\u2014"
COLON_AFTER = ("»", "общаться", "задать мне вопрос", "колонке J", "на связи", "для оформления", "спрашивать",
               "своими словами", "учётной записью", "Наша миссия", "оставили дословно", "открытый вопрос",
               "Поздравляю", "по подбору", "на выходные", "мне не решить", "подборку", "пролетели", "И напоследок",
               "как в исходнике")
COMMA_AFTER = ("🐾", "на выбор", "напиши мне", "напишите мне", "до первого дня", "пока всё", "остались вопросы",
               "третий день", "напрямую", "горячий период", "нужна помощь", "в консалтинговой", "расскажи",
               "расскажите", "Давно не писал", "где их брать", "Напиши здесь", "Напишите здесь", "Пора прощаться",
               "Гадать не хочу")

HOW_TO_READ = """Сценарий Корсика: как читать

Одна строка листа «Сценарий» = одно сообщение бота. Бот находит колонки по названиям.

Шаг: номер, не повторяется. Номер = строка исходного файла кейса, 25.1, 25.2 и дальше: части и ветки строки 25.

Когда: время шага в часовом поясе новичка.
старт: сразу после того, как новичок открыл свою ссылку.
-2д 10:00, +10д 10:00: за 2 дня до выхода и через 10 дней после выхода, в 10:00.
р1 10:00, р3 17:30: первый рабочий день (день выхода) и третий рабочий день.
м1 09:00, м1+14д 10:00: дата выхода плюс месяц; плюс месяц и 14 дней.
конец 09:00: последний день испытательного срока (выход + 3 месяца - 1 день).
напоминание: этим текстом бот один раз напоминает про вопрос без ответа.
эскалация: этим текстом бот отвечает, когда передал вопрос HR.
Выходной или праздник из scenario/holidays.txt сдвигает шаг: до выхода на рабочий день раньше, после выхода на рабочий день позже.

После: «25: Да!» значит, что шаг уходит сразу после нажатия кнопки «Да!» в шаге 25. Заполняется что-то одно: «Когда» или «После».

Теги: пусто: всем. «GrPP, Luzhniki»: только новичкам, у которых есть хотя бы один из этих тегов. «-GTP»: всем, кроме GTP. Большие и маленькие буквы не важны.

Тип:
сообщение: просто текст;
обращение: кнопки «ты» и «вы»;
вопрос: кнопки ведут в ветки;
опрос: кнопки, выбранный вариант сохраняется;
шкала: кнопки от 1 до 10, ставятся сами;
текст: бот ждёт ответ текстом.
Шаги одного времени уходят по порядку строк. Пока новичок не ответил на вопрос, опрос, шкалу или текст, следующие шаги ждут.

Текст на «ты» и на «вы»: {имя} бот заменит на имя новичка. Пустой «вы» = как «ты». Строки [HR: ...] новичку не уходят: это пометки, что дописать. Не длиннее 4000 символов.

Кнопки: через |. «Справочник = https://...» это кнопка-ссылка.

Плохой ответ: для шкалы «<8», для опроса варианты через |. На такой ответ бот спросит, что не так, и подождёт ответ текстом.

Картинка: имя файла из папки data/images. Если файла нет, сообщение уйдёт без картинки.

Комментарий: для людей, бот его не читает.

Строка 3 исходника не шаг: это описание бота, его ставят в профиль бота в МАКС (до 200 символов):"""


def no_dashes(text: str) -> str:
    for words in COLON_AFTER:
        text = text.replace(f"{words} {DASH} ", f"{words}: ")
    for words in COMMA_AFTER:
        text = text.replace(f"{words} {DASH} ", f"{words}, ")
    # in «Напиши, чего именно нет, ... передам» the comma is already there, the dash just goes
    text = text.replace(f", {DASH} ", ", ")
    return text.replace(f" {DASH} ", " - ").replace(DASH, "-")


def load_texts(path):
    spec = importlib.util.spec_from_file_location("korsik_texts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_source(path) -> dict[int, dict]:
    ws = openpyxl.load_workbook(path).worksheets[0]
    return {row: {"tags": (ws[f"D{row}"].value or "").strip(), "image": image_name(ws[f"I{row}"].value)}
            for row in range(3, 71)}


def image_name(value) -> str:
    if not value:
        return ""
    if value.strip() == "ФОТО":
        return "photo_1.png"
    number = int(re.search(r"\d+", value)[0])
    return f"card_{number}.png" if "Открытка" in value else f"img_{number:02d}.png"


def clean(text: str) -> str:
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("[") and not stripped.startswith("[HR:"):
            note = stripped[1:stripped.index("]")]
            if note.startswith(HR_ATTACHMENTS):
                lines.append(f"[HR: {note}]")
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def main_part(text: str) -> str:
    return text.split("\n\nКнопки:")[0]


def paragraph_after(text: str, header: str) -> str:
    for paragraph in text.split("\n\n"):
        first, _, rest = paragraph.partition("\n")
        if first == header:
            return rest
    raise KeyError(f"no paragraph {header!r} in {text[:40]!r}")


def options(text: str) -> list[str]:
    return [line.removeprefix(ITEM) for line in paragraph_after(text, "Кнопки:").split("\n")]


def numbered(text: str) -> list[str]:
    return re.findall(r"^\d+\. (.+)$", text, re.MULTILINE)


def survey(text: str) -> list[tuple[str, list[str]]]:
    """Paragraphs of a numbered question followed by its options -> [(question, options)]."""
    result = []
    for paragraph in text.split("\n\n"):
        first, *rest = paragraph.split("\n")
        result.append((re.sub(r"^\d+\. ", "", first), [line.removeprefix(ITEM) for line in rest]))
    return result


def plan(kt, source) -> list[list[str]]:
    texts = {row: (ty, vy or ty) for row, (ty, vy, _) in kt.TEXTS.items()}
    comments = {row: note for row, (_, _, note) in kt.TEXTS.items()}
    steps = []

    def add(step_id, when="", after="", kind="сообщение", text=None, buttons="", bad="", tags=None, image=None,
            note=""):
        row = int(step_id.split(".")[0])
        is_main = "." not in step_id
        ty, vy = (clean(t) for t in (text or texts[row]))
        if tags is None:
            tags = source[row]["tags"]
        if image is None:
            image = source[row]["image"] if is_main else ""
        comment = " ".join(c for c in (comments[row] if is_main else "", note) if c)
        steps.append([step_id, when, after, tags, kind, ty, "" if vy == ty else vy, buttons, bad, image, comment])

    def part(row, fn, *args):
        return tuple(fn(t, *args) for t in texts[row])

    def with_list(row):
        return tuple(f"{t}\n\n{kt.J_TEXTS[row]}" for t in texts[row])

    def ratings(row, when):
        # one message with nine questions becomes nine steps: yes/no is a poll, the rest are 1-10 scales
        for n, (ty, vy) in enumerate(zip(*part(row, numbered)), start=1):
            yes_no = ty.endswith(YES_NO)
            add(f"{row}.{n}", when, kind="опрос" if yes_no else "шкала", text=(f"{n} из 9. {ty}", f"{n} из 9. {vy}"),
                buttons="Да | Нет" if yes_no else "", bad="Нет" if yes_no else "<8")

    def two_months_survey(row, when, note):
        (q1, q2, q3), (v1, v2, v3) = part(row, survey)
        add(f"{row}", when, kind="опрос", text=(f"1 из 3. {q1[0]}", f"1 из 3. {v1[0]}"), buttons=" | ".join(q1[1]),
            note=note)
        add(f"{row}.1", after=f"{row}: Ровно: отработать и домой", kind="текст", note=FROM_57,
            text=part(57, paragraph_after, "Если в вопросе 1 «Ровно: отработать и домой»:"))
        add(f"{row}.2", after=f"{row}: Устаю, задачи наваливаются", kind="текст", note=FROM_57,
            text=part(57, paragraph_after, "Если в вопросе 1 «Устаю, задачи наваливаются»:"))
        # MAX has no multiple choice, so the second question takes one answer
        q2_text = [f"2 из 3. {q[0].replace(' (можно выбрать несколько)', '')}" for q in (q2, v2)]
        add(f"{row}.3", when, kind="опрос", text=q2_text, buttons=" | ".join(q2[1]))
        add(f"{row}.4", after=f"{row}.3: Никто", note=FROM_57,
            text=part(57, paragraph_after, "Если в вопросе 2 «Никто»:"))
        q3_buttons = [b.replace("Свой вариант (напишу сам)", "Свой вариант (напишу)") for b in q3[1]]
        add(f"{row}.5", when, kind="опрос", text=(f"3 из 3. {q3[0]}", f"3 из 3. {v3[0]}"),
            buttons=" | ".join(q3_buttons))
        add(f"{row}.6", after=f"{row}.5: Свой вариант (напишу)", kind="текст", text=WRITE_IN, note=OWN_TEXT)

    # first block: sent as soon as the employee opens the link (in the case: 2-3 days before the start)
    add("4", "старт")
    add("5", "старт", kind="обращение", buttons=" | ".join(options(texts[6][0])),
        note="Строки 5 и 6 исходника: вопрос и кнопки в одном шаге.")
    add("7", "старт", note="Стоит после шага 5 и ждёт выбора, поэтому уходит уже в нужном регистре.")
    add("8", "старт")
    add("9", "старт", text=with_list(9), note="Список документов из колонки J добавлен в конец текста.")
    add("10", "старт", text=with_list(10), note="Список документов из колонки J добавлен в конец текста.")
    add("11", "старт", note=NO_FOLLOW_UP)
    add("12", "старт", note=NO_FOLLOW_UP)
    add("13", "старт", note=NO_FOLLOW_UP)
    add("14", "старт")
    add("15", "старт")

    # the day before the start
    add("16", "-1д 10:00", kind="опрос", text=part(16, main_part), buttons=" | ".join(options(texts[16][0])),
        note="Время в исходнике не указано, взяли 10:00. Ответ сохраняется как опрос.")
    add("16.1", after="16: Жду не дождусь", text=part(16, paragraph_after, "Если «Жду не дождусь» или «Всё готово»:"))
    add("16.2", after="16: Всё готово", text=part(16, paragraph_after, "Если «Жду не дождусь» или «Всё готово»:"))
    add("16.3", after="16: Немного волнуюсь", text=part(16, paragraph_after, "Если «Немного волнуюсь»:"))
    add("16.4", "-1д 10:00", text=part(16, paragraph_after, "Дальше всем:"),
        note="Строка 16, «дальше всем»: уходит после ответа на шаг 16.")
    # column D of row 17 holds the reason, not a tag
    add("17", "-1д 10:00", text=with_list(17), tags="", note="Список документов из колонки J добавлен в конец текста.")

    # first working day
    add("18", "р1 10:00")
    add("20", "р1 11:00", note=NO_FOLLOW_UP)
    add("21", "р1 11:00", note=NO_FOLLOW_UP)
    for row in (22, 23):
        add(f"{row}", "р1 12:00", kind="опрос", text=part(row, main_part), buttons=" | ".join(options(texts[row][0])))
        add(f"{row}.1", after=f"{row}: Да, всё есть", text=part(row, paragraph_after, "Если «Да, всё есть»:"))
        add(f"{row}.2", after=f"{row}: Чего-то не хватает", kind="текст",
            text=part(row, paragraph_after, "Если «Чего-то не хватает»:"))
    add("24", "р1 12:01")
    add("25", "р1 12:30", kind="вопрос", text=part(25, main_part), buttons="Да! | Нет, спасибо",
        note="Кнопка «Да, подскажи» стала «Да!»: колонка кнопок общая для «ты» и «вы». Команды /food в боте пока нет, "
             "работает вопрос своими словами.")
    add("25.1", after="25: Нет, спасибо", text=part(25, paragraph_after, "Если «Нет, спасибо»:"))
    for row in (26, 27, 28):
        add(f"{row}", after="25: Да!", note="PDF бот не отправляет, только картинки: список лучше вписать текстом.")
    add("29", "р1 13:00")
    add("30", "р1 13:00", buttons="Справочник = https://my.polyplastic.ru/employees/Pages/address-book.aspx",
        note="Ссылка продублирована кнопкой.")
    add("31", "р1 13:00", note="Без кнопки: адрес http://ats/ внутренний.")
    add("32", "р1 15:00")
    add("33", "р1 18:00", note="В исходнике «для сотрудников офиса», тега не было: если нужно, поставить тег.")

    add("34", "р2 10:00", kind="текст",
        note="В исходнике «по отмашке рекрутера», сейчас уходит всем. Бот сохраняет ответ, сам знакомство не публикует.")

    # third working day and the first survey
    add("35", "р3 09:00")
    add("36", "р3 09:00", note="План бот сам не прикладывает, см. пометку [HR: ...].")
    add("37", "р3 11:00", note=NO_FOLLOW_UP)
    add("38", "р3 17:30")
    add("39", "р3 17:30", kind="опрос", text=part(39, main_part), buttons=" | ".join(options(texts[39][0])),
        note="Кнопок «Написать рекрутеру» пока нет: у бота нет ссылки на личный диалог с рекрутером.")
    add("39.1", after="39: Остались вопросы", text=part(39, paragraph_after, "Если «Остались вопросы»:"))
    add("39.2", after="39: Были трудности с документами", kind="текст",
        text=part(39, paragraph_after, "Если «Были трудности с документами»:"))
    add("40", "р3 17:30", kind="опрос", text=part(40, main_part), buttons=" | ".join(options(texts[40][0])))
    add("40.1", after="40: Никак, все заняты", text=part(40, paragraph_after, "Если «Никак, все заняты»:"))
    add("41", "р3 17:30", kind="опрос", text=part(41, main_part), buttons=" | ".join(options(texts[41][0])))
    add("41.1", after="41: Да, всё понятно", text=part(41, paragraph_after, "Если «Да, всё понятно»:"))
    add("41.2", after="41: Жду разъяснений от руководителя",
        text=part(41, paragraph_after, "Если «Жду разъяснений от руководителя»:"))
    add("41.3", after="41: Пока ничего не понятно", text=part(41, paragraph_after, "Если «Пока ничего не понятно»:"))
    add("42", "р3 17:30")

    add("43", "+10д 10:00", note="Время в исходнике не указано, взяли 10:00.")
    add("44", "+10д 10:00")
    add("45", "+10д 10:00", note="В исходнике картинка «ФОТО», назвали photo_1.png.")
    add("46", "+10д 10:00")

    add("47", "+14д 10:00", text=part(47, lambda t: t.split("\n\n")[0]),
        note="Девять вопросов разбиты на шаги 47.1-47.9: «да/нет» это опрос, оценки это шкала. При «Нет» или оценке "
             "ниже 8 бот спрашивает, что не так.")
    ratings(47, "+14д 10:00")

    add("48", "м1 09:00")
    add("49", "м1 09:00", kind="опрос", text=part(49, main_part), buttons=" | ".join(options(texts[49][0])),
        note="Команды /hr в боте пока нет.")
    add("49.1", after="49: Всё по плану", text=part(49, paragraph_after, "Если «Всё по плану»:"))
    add("49.2", after="49: Немного отстаю", text=part(49, paragraph_after, "Если «Немного отстаю»:"))
    add("49.3", after="49: Сильно отстаю", kind="текст", text=part(49, paragraph_after, "Если «Сильно отстаю»:"))
    add("50", "м1+14д 10:00", note="Время в исходнике не указано, взяли 10:00.")
    add("51", "м1+14д 16:00", note="Развели с шагом 50 по времени, чтобы не было двух «Привет» подряд.")
    add("52", "м1+14д 16:00")

    add("53", "м1+21д 09:00")
    add("54", "м1+21д 09:00")
    two_months_survey(55, "м1+21д 09:01", "Три вопроса разбиты на шаги, ответы из строки 57 стали ветками. Выбрать "
                                          "несколько вариантов в МАКС нельзя: во втором вопросе один вариант.")
    two_months_survey(56, "м1+21д 09:01", "Как шаг 55, без варианта про удалённый формат.")
    add("57", "м1+21д 09:01", text=part(57, paragraph_after, "Всем после ответов:"),
        note="Здесь только благодарность, ответы на варианты стоят ветками у шагов 55 и 56.")

    add("58", "м2 09:00")
    add("59", "м2+14д 10:00", note="В исходнике «для офисных сотрудников», тега не было.")
    add("60", "м2+14д 10:00", text=("Это тебе открытка 🙂", "Это вам открытка 🙂"),
        note="Текст наш: в исходнике была только открытка.")

    # last day of probation and the final survey
    add("61", "конец 09:00")
    add("62", "конец 09:01", kind="опрос", text=part(62, main_part), buttons=" | ".join(options(texts[62][0])))
    add("62.1", after="62: Другое (напишу)", kind="текст", text=WRITE_IN, note=OWN_TEXT)
    add("63", "конец 09:01", kind="опрос", text=part(63, main_part), buttons=" | ".join(options(texts[63][0])))
    add("63.1", after="63: Да, однозначно", text=part(63, paragraph_after, "Если «Да, однозначно»:"))
    add("63.2", after="63: Пока сомневаюсь", kind="текст", text=part(63, paragraph_after, "Если «Пока сомневаюсь»:"))
    add("63.3", after="63: Нет, думаю об уходе", kind="текст",
        text=part(63, paragraph_after, "Если «Нет, думаю об уходе»:"))
    add("64", "конец 09:01", text=part(64, lambda t: t.split("\n\n")[0]), note="Девять оценок разбиты на шаги 64.1-64.9.")
    ratings(64, "конец 09:01")
    add("65", "конец 09:01", kind="текст")
    add("66", "конец 09:01")
    add("67", "конец 09:01", buttons="Плейлист = https://music.yandex.ru/artist/5631933",
        note="Ссылка стала кнопкой: в МАКС нельзя сделать ссылкой одно слово.")
    add("68", "конец 09:01", kind="вопрос", text=part(68, main_part), buttons=" | ".join(options(texts[68][0])))

    # not scheduled: the engine takes these texts when it needs them
    add("69", "эскалация", note="Не шаг по времени: так бот отвечает, когда передал вопрос HR.")
    add("70", "напоминание", note="Не шаг по времени: текст напоминания про вопрос без ответа. Кнопки «Продолжить опрос» нет.")
    return steps


def write(steps, description: str, out: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Сценарий"
    ws.append(HEADERS)
    for step in steps:
        ws.append([no_dashes(value) for value in step])
    wrap = Alignment(wrap_text=True, vertical="top")
    for row in ws.iter_rows():
        for cell in row:
            cell.alignment = wrap
            cell.number_format = "@"
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for i, width in enumerate(WIDTHS, start=1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = width
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions

    notes = wb.create_sheet("Как читать")
    for line in HOW_TO_READ.split("\n") + [no_dashes(description)]:
        notes.append([line])
    notes["A1"].font = Font(bold=True, size=13)
    notes.column_dimensions["A"].width = 120
    for row in notes.iter_rows():
        row[0].alignment = Alignment(wrap_text=True, vertical="top")

    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build scenario.xlsx from the case workbook and korsik_texts.py")
    parser.add_argument("source", help="case workbook, Финал. ТЗ. Чат-бот Корсик- 15.09.xlsx")
    parser.add_argument("texts", help="korsik_texts.py with the rewritten texts")
    parser.add_argument("out", nargs="?", default=str(ROOT / "scenario" / "scenario.xlsx"))
    args = parser.parse_args()

    kt = load_texts(args.texts)
    steps = plan(kt, read_source(args.source))
    out = Path(args.out)
    write(steps, kt.TEXTS[3][0], out)

    problems = validate_scenario(out)
    for problem in problems:
        print(problem)
    print(f"{len(steps)} steps -> {out}")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
