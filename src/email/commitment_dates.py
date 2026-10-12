"""Conservative calendar-date resolution from source text, never machine today."""
from datetime import datetime,timedelta,timezone
import re



def authored_datetime(value):
    if not isinstance(value, (str, datetime)):
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def source_relative_date(text: str, authored_at: datetime | None):
    if authored_at is None or authored_at.tzinfo is None:
        return None
    if re.search(r"\bor\b|business days|\u5de5\u4f5c\u65e5|\u6216", text, re.I):
        return None
    offsets=set()
    if re.search(r"\bHKT\b|Asia/Hong_Kong|UTC\s*\+\s*0?8(?::?00)?|\u9999\u6e2f\u6642\u9593|\u9999\u6e2f\u65f6\u95f4",text,re.I): offsets.add(8)
    if re.search(r"\bUTC\b(?!\s*[+-])",text,re.I): offsets.add(0)
    if len(offsets)!=1:
        return None
    days=set()
    if re.search(r"\bday after tomorrow\b|\u5f8c\u5929|\u540e\u5929",text,re.I):
        days.add(2)
        text=re.sub(r"\bday after tomorrow\b|\u5f8c\u5929|\u540e\u5929","",text,flags=re.I)
    if re.search(r"\btomorrow\b|\u660e\u5929",text,re.I):days.add(1)
    if re.search(r"\btoday\b|\u4eca\u5929",text,re.I):days.add(0)
    for value in re.findall(r"\bin (\d{1,4}) calendar days\b",text,re.I):days.add(int(value))
    if len(days)!=1 or max(days, default=0)>3650:
        return None
    try:
        return authored_at.astimezone(timezone(timedelta(hours=next(iter(offsets))))).date()+timedelta(days=next(iter(days)))
    except (OverflowError, ValueError):
        return None
