"""Aggregate English UI strings for Telegram i18n."""

from src.telegram.i18n_en_cards import STRINGS_EN as _cards
from src.telegram.i18n_en_callbacks import STRINGS_EN as _callbacks
from src.telegram.i18n_en_commands import STRINGS_EN as _commands
from src.telegram.i18n_en_portfolio import STRINGS_EN as _portfolio
from src.telegram.i18n_en_scheduler import STRINGS_EN as _scheduler

STRINGS_EN: dict[str, str] = {}
STRINGS_EN.update(_cards)
STRINGS_EN.update(_commands)
STRINGS_EN.update(_callbacks)
STRINGS_EN.update(_scheduler)
STRINGS_EN.update(_portfolio)
