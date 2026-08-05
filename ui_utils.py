"""Общие UI-утилиты: форматирование, экранирование, пагинация.

Не импортирует engine (engine и UI-модули импортируют этот модуль).
"""

from typing import Optional

import lolka as discord


def esc(text) -> str:
    """Экранировать пользовательский/YouTube текст перед вставкой в сообщение.

    Упоминания (@everyone, @user) и markdown (включая ссылочный синтаксис
    [text](url) — фишинговые ссылки) в названиях роликов или запросах
    иначе превращаются в пинги и кликабельные ссылки: в lolka.py упоминания
    в сообщениях бота разрешены по умолчанию.
    """
    s = str(text)
    s = discord.utils.escape_mentions(s)
    return discord.utils.escape_markdown(s, ignore_links=False)


def fmt_duration(seconds) -> str:
    """Длительность в формате м:сс (или ч:мм:сс для длинных треков).

    None/нечисловое значение → пустая строка (показывать скобки не нужно).
    """
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return ""
    if total < 0:
        return ""
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def paginate(items: list, page: int, page_size: int = 10, *, zero_based: bool = False):
    """Разбить список на страницы.

    Возвращает (chunk | None, total_pages, has_prev, has_next, first_item_number).
    При page вне диапазона chunk = None.

    zero_based=False — страницы нумеруются с 1 (для пользовательских команд).
    zero_based=True  — страницы нумеруются с 0 (для внутренней пагинации view).
    """
    pages = max(1, -(-len(items) // page_size))
    if zero_based:
        if page < 0 or page >= pages:
            return None, pages, False, False, 0
        chunk = items[page * page_size:(page + 1) * page_size]
        return chunk, pages, page > 0, page + 1 < pages, page * page_size + 1
    else:
        if page < 1 or page > pages:
            return None, pages, False, False, 0
        chunk = items[(page - 1) * page_size: page * page_size]
        return chunk, pages, page > 1, page < pages, (page - 1) * page_size + 1
