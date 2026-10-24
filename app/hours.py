"""Working hours of a workplace or a recruiter: when the bot may bring them a message."""
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app import db

DAY_NAMES = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
TIME_RE = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")
DEFAULT_DAYS = "1,2,3,4,5"
# Russian time zones, west to east; offsets never change because Russia has no summer time
TIMEZONES = {
    "Europe/Kaliningrad": "Калининград (UTC+2)",
    "Europe/Moscow": "Москва, Санкт-Петербург (UTC+3)",
    "Europe/Samara": "Самара (UTC+4)",
    "Asia/Yekaterinburg": "Екатеринбург (UTC+5)",
    "Asia/Omsk": "Омск (UTC+6)",
    "Asia/Novosibirsk": "Новосибирск (UTC+7)",
    "Asia/Krasnoyarsk": "Красноярск (UTC+7)",
    "Asia/Irkutsk": "Иркутск (UTC+8)",
    "Asia/Yakutsk": "Якутск (UTC+9)",
    "Asia/Vladivostok": "Владивосток (UTC+10)",
    "Asia/Magadan": "Магадан (UTC+11)",
    "Asia/Kamchatka": "Камчатка (UTC+12)",
}


@dataclass
class Schedule:
    timezone: str
    days: set[int]  # ISO weekdays, 1 is Monday
    start: time
    end: time

    def is_open(self, at: datetime, holidays: set[date] = frozenset()) -> bool:
        local = at.astimezone(ZoneInfo(self.timezone))
        return (local.isoweekday() in self.days and local.date() not in holidays
                and self.start <= local.time() < self.end)

    def next_open(self, at: datetime, holidays: set[date] = frozenset()) -> datetime:
        """The moment itself if it is working time, otherwise the start of the next working day."""
        if self.is_open(at, holidays):
            return at
        zone = ZoneInfo(self.timezone)
        local = at.astimezone(zone)
        day = local.date()
        if local.time() >= self.start:
            day += timedelta(days=1)
        # a schedule with no days at all would loop forever, two weeks is more than any real week
        for _ in range(15):
            if day.isoweekday() in self.days and day not in holidays:
                return datetime.combine(day, self.start, tzinfo=zone).astimezone(timezone.utc)
            day += timedelta(days=1)
        return at

    def describe(self) -> str:
        return f"{describe_days(self.days)} {self.start:%H:%M}-{self.end:%H:%M}"


def parse_days(text: str | None) -> set[int]:
    return {int(part) for part in (text or "").split(",") if part.strip().isdigit() and 1 <= int(part) <= 7}


def parse_time(text: str | None) -> time | None:
    match = TIME_RE.fullmatch((text or "").strip())
    return time(int(match[1]), int(match[2])) if match else None


def describe_days(days: set[int]) -> str:
    ordered = sorted(days)
    if not ordered:
        return "нет рабочих дней"
    if ordered == list(range(ordered[0], ordered[-1] + 1)) and len(ordered) > 2:
        return f"{DAY_NAMES[ordered[0] - 1]}-{DAY_NAMES[ordered[-1] - 1]}"
    return ", ".join(DAY_NAMES[d - 1] for d in ordered)


def from_row(row) -> Schedule:
    return Schedule(row["timezone"], parse_days(row["work_days"]) or parse_days(DEFAULT_DAYS),
                    parse_time(row["work_start"]) or time(9), parse_time(row["work_end"]) or time(18))


def of_employee(employee) -> Schedule | None:
    """The schedule of the newcomer's workplace; None when HR has not picked one, then time is not limited."""
    if not employee["workplace_id"]:
        return None
    workplace = db.query_one("SELECT * FROM workplaces WHERE id = ?", (employee["workplace_id"],))
    if workplace is None:
        return None
    # the newcomer's own zone wins: HR may have set it by hand for a remote worker
    schedule = from_row(workplace)
    schedule.timezone = employee["timezone"] or schedule.timezone
    return schedule


def of_recruiter(recruiter) -> Schedule:
    return from_row(recruiter)


def when_text(at: datetime, tz: str) -> str:
    local = at.astimezone(ZoneInfo(tz))
    return f"{DAY_NAMES[local.weekday()]} {local:%d.%m} в {local:%H:%M}"


def waited_text(since: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    minutes = max(int((now - datetime.fromisoformat(since)).total_seconds() // 60), 0)
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч {minutes} мин"
    days, hours = divmod(hours, 24)
    return f"{days} дн {hours} ч"


def read_form(tz: str, days: list[str], start: str, end: str) -> tuple[tuple | None, str]:
    """(zone, days, start, end) from a form, or None and what is wrong."""
    picked = sorted(parse_days(",".join(days)))
    begin, finish = parse_time(start), parse_time(end)
    if tz not in TIMEZONES:
        return None, "Выберите часовой пояс из списка."
    if not picked:
        return None, "Отметьте хотя бы один рабочий день."
    if begin is None or finish is None:
        return None, "Время работы нужно в виде 09:00 и 18:00."
    if begin >= finish:
        return None, "Конец рабочего дня должен быть позже начала."
    return (tz, ",".join(map(str, picked)), f"{begin:%H:%M}", f"{finish:%H:%M}"), ""
