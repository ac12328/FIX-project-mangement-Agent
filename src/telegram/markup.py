"""Telegram markup helpers shared across runtime surfaces."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.telegram.cards import InteractionCard


def card_to_markup(card: InteractionCard) -> InlineKeyboardMarkup | None:
    """Convert an interaction card into Telegram inline keyboard markup."""

    rows = []
    for row in card.buttons:
        rows.append(
            [
                InlineKeyboardButton(text=button.label, callback_data=button.callback_data)
                for button in row
            ]
        )
    from src.telegram.operator_messages import dashboard_url, en
    url = dashboard_url(card)
    if url:
        rows.append([InlineKeyboardButton("View related items" if en() else "查看相關項目", url=url)])
    return InlineKeyboardMarkup(rows) if rows else None
