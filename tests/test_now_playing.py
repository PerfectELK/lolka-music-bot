"""Тесты now_playing.py: проверка прав, кнопки, _can_control."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from now_playing import NowPlayingView

GUILD = 42
VC_CHANNEL = 123


def make_interaction(user_id, voice_channel_id=None):
    interaction = MagicMock()
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    if voice_channel_id is not None:
        interaction.user.voice = MagicMock()
        interaction.user.voice.channel = MagicMock()
        interaction.user.voice.channel.id = voice_channel_id
    else:
        interaction.user.voice = None
    return interaction


def make_engine(guild_id, vc_channel_id=None, has_perm=True):
    engine = MagicMock()
    engine.bot = MagicMock()
    guild = MagicMock()
    guild.id = guild_id
    vc = MagicMock() if vc_channel_id is not None else None
    if vc:
        vc.channel = MagicMock()
        vc.channel.id = vc_channel_id
        vc.is_playing = MagicMock(return_value=True)
    guild.voice_client = vc
    engine.bot.get_guild.return_value = guild
    engine.has_perm = MagicMock(return_value=has_perm)
    engine.skip_current = MagicMock(return_value="ok")
    engine.prev_current = MagicMock(return_value="ok")
    engine.pause_toggle = MagicMock(return_value="ok")
    engine.loop_toggle = MagicMock(return_value=True)
    engine.stop_and_leave = AsyncMock()
    engine.play_state = {}
    return engine


@pytest.mark.asyncio
async def test_can_control_same_channel():
    engine = make_engine(GUILD, VC_CHANNEL)
    view = NowPlayingView(engine, GUILD)
    interaction = make_interaction(1, VC_CHANNEL)
    result = await view._can_control(interaction)
    assert result is True


@pytest.mark.asyncio
async def test_can_control_different_channel():
    engine = make_engine(GUILD, VC_CHANNEL)
    view = NowPlayingView(engine, GUILD)
    interaction = make_interaction(1, 999)
    result = await view._can_control(interaction)
    assert result is False


@pytest.mark.asyncio
async def test_can_control_no_voice():
    engine = make_engine(GUILD, VC_CHANNEL)
    view = NowPlayingView(engine, GUILD)
    interaction = make_interaction(1)  # voice = None
    result = await view._can_control(interaction)
    assert result is False


@pytest.mark.asyncio
async def test_can_control_bot_disconnected():
    engine = make_engine(GUILD, vc_channel_id=None)
    view = NowPlayingView(engine, GUILD)
    interaction = make_interaction(1, VC_CHANNEL)
    result = await view._can_control(interaction)
    assert result is False


@pytest.mark.asyncio
async def test_can_control_no_perm():
    engine = make_engine(GUILD, VC_CHANNEL, has_perm=False)
    view = NowPlayingView(engine, GUILD)
    interaction = make_interaction(1, VC_CHANNEL)
    result = await view._can_control(interaction)
    assert result is False


@pytest.mark.asyncio
async def test_skip_button_ok():
    engine = make_engine(GUILD, VC_CHANNEL)
    view = NowPlayingView(engine, GUILD)
    interaction = make_interaction(1, VC_CHANNEL)

    await view.next_button.callback(interaction)

    engine.skip_current.assert_called_once_with(GUILD)
    interaction.response.defer.assert_called_once()


@pytest.mark.asyncio
async def test_skip_button_nothing_playing():
    engine = make_engine(GUILD, VC_CHANNEL)
    engine.skip_current.return_value = "nothing"
    view = NowPlayingView(engine, GUILD)
    interaction = make_interaction(1, VC_CHANNEL)

    await view.next_button.callback(interaction)

    interaction.response.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_prev_button_ok():
    engine = make_engine(GUILD, VC_CHANNEL)
    engine.prev_current.return_value = "ok"
    view = NowPlayingView(engine, GUILD)
    interaction = make_interaction(1, VC_CHANNEL)

    await view.prev_button.callback(interaction)

    engine.prev_current.assert_called_once_with(GUILD)
    interaction.response.defer.assert_called_once()


@pytest.mark.asyncio
async def test_prev_button_no_prev():
    engine = make_engine(GUILD, VC_CHANNEL)
    engine.prev_current.return_value = "no_prev"
    view = NowPlayingView(engine, GUILD)
    interaction = make_interaction(1, VC_CHANNEL)

    await view.prev_button.callback(interaction)

    interaction.response.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_pause_button():
    engine = make_engine(GUILD, VC_CHANNEL)
    view = NowPlayingView(engine, GUILD)
    interaction = make_interaction(1, VC_CHANNEL)

    await view.pause_button.callback(interaction)

    engine.pause_toggle.assert_called_once_with(GUILD)
    interaction.response.defer.assert_called_once()


@pytest.mark.asyncio
async def test_loop_button():
    engine = make_engine(GUILD, VC_CHANNEL)
    view = NowPlayingView(engine, GUILD)
    interaction = make_interaction(1, VC_CHANNEL)
    interaction.message = MagicMock()
    interaction.message.content = "текущий контент"

    await view.loop_button.callback(interaction)

    engine.loop_toggle.assert_called_once_with(GUILD)
    interaction.response.edit_message.assert_called_once()


@pytest.mark.asyncio
async def test_stop_button():
    engine = make_engine(GUILD, VC_CHANNEL)
    view = NowPlayingView(engine, GUILD)
    interaction = make_interaction(1, VC_CHANNEL)

    await view.stop_button.callback(interaction)

    engine.stop_and_leave.assert_called_once_with(GUILD)
    interaction.response.defer.assert_called_once()
