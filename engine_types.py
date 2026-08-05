"""TypedDict-классы для ключевых структур данных движка (аннотации времени разработки)."""

from typing import TypedDict

import lolka as discord


class QueueEntry(TypedDict, total=False):
    """Элемент очереди треков."""

    title: str
    page_url: str | None
    url: str | None
    duration: int | None
    resolved: bool
    error: str | None
    future: "asyncio.Task | None"  # type: ignore[name-defined]
    save_to_default: bool


class PlayState(TypedDict, total=False):
    """Состояние воспроизведения на глиду («Сейчас играет»)."""

    channel: discord.TextChannel | None
    now_message: discord.Message | None
    title: str | None
    page_url: str | None
    duration: int | None
    view: "lolka.ui.View | None"  # type: ignore[name-defined]
    new_session: bool


class PlNav(TypedDict):
    """Курсор плейлиста (инвариант: очередь == items[index:])."""

    name: str
    items: list[QueueEntry]
    index: int


class PendingSearch(TypedDict, total=False):
    """Активный поиск по YouTube с выбором результата реакцией."""

    message: discord.Message | None
    user_id: int
    results: list[dict]
    expire_task: "asyncio.Task | None"  # type: ignore[name-defined]
