"""
HK business-day calendar utilities.
Shared by inactivity_alert, morning_brief, and any future scheduler jobs.
"""

from datetime import datetime, timedelta, timezone


INACTIVITY_THRESHOLD_DAYS = 3  # business days, per Section 34.1

# Gazetted 2026 general holidays. Weekends are handled below.
# Source: https://www.gov.hk/en/about/abouthk/holiday/2026.htm
_HK_HOLIDAYS_2026 = {
    "01-01",  # The first day of January
    "02-17",  # Lunar New Year's Day
    "02-18",  # The second day of Lunar New Year
    "02-19",  # The third day of Lunar New Year
    "04-03",  # Good Friday
    "04-04",  # The day following Good Friday
    "04-06",  # The day following Ching Ming Festival
    "04-07",  # The day following Easter Monday
    "05-01",  # Labour Day
    "05-25",  # The day following the Birthday of the Buddha
    "06-19",  # Tuen Ng Festival
    "07-01",  # Hong Kong SAR Establishment Day
    "09-26",  # The day following the Chinese Mid-Autumn Festival
    "10-01",  # National Day
    "10-19",  # The day following Chung Yeung Festival
    "12-25",  # Christmas Day
    "12-26",  # The first weekday after Christmas Day
}


def is_business_day(dt: datetime) -> bool:
    """Return True if dt is a business day (Mon-Fri, not a HK public holiday)."""
    if dt.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    date_str = dt.strftime("%m-%d")
    if date_str in _HK_HOLIDAYS_2026:
        return False
    return True


def business_days_between(start: datetime, end: datetime) -> int:
    """Count business days from start to end (inclusive of start, exclusive of end)."""
    if start >= end:
        return 0
    count = 0
    current = start
    while current < end:
        if is_business_day(current):
            count += 1
        current += timedelta(days=1)
    return count


def next_business_day(dt: datetime) -> datetime:
    """Return the next business day at midnight UTC."""
    result = dt + timedelta(days=1)
    while not is_business_day(result):
        result += timedelta(days=1)
    return result.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
