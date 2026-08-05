"""Тесты cogs/listener.py: on_message обработка ссылок, вложений, поиска."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.listener import ListenerCog

GUILD = 42


def make_message(content, attachments=None, author_bot=False, channel_name="music"):
    msg = MagicMock()
    msg.content = content
    msg.attachments = attachments or []
    msg.author = MagicMock()
    msg.author.bot = author_bot
    msg.channel = MagicMock()
    msg.channel.name = channel_name
    msg.channel.send = AsyncMock()
    msg.guild = MagicMock()
    msg.guild.id = GUILD
    return msg


def make_engine():
    engine = MagicMock()
    engine.enqueue_single = AsyncMock()
    engine.enqueue_local = AsyncMock()
    engine.start_search = AsyncMock()
    engine.cleanup_uploads = MagicMock()
    return engine


@pytest.fixture
def bot():
    b = MagicMock()
    b.command_prefix = "!"
    return b


@pytest.fixture
def cog(bot):
    engine = make_engine()
    return ListenerCog(bot, engine)


@pytest.mark.asyncio
async def test_ignores_bot_messages(cog):
    msg = make_message("https://youtu.be/abcdefghijk", author_bot=True)
    await cog.on_message(msg)
    cog.engine.enqueue_single.assert_not_called()


@pytest.mark.asyncio
async def test_ignores_commands(cog):
    msg = make_message("!play test")
    await cog.on_message(msg)
    cog.engine.enqueue_single.assert_not_called()


@pytest.mark.asyncio
async def test_ignores_non_music_channel(cog):
    msg = make_message("https://youtu.be/abcdefghijk", channel_name="general")
    await cog.on_message(msg)
    cog.engine.enqueue_single.assert_not_called()


@pytest.mark.asyncio
async def test_youtube_url_enqueues(cog):
    msg = make_message("текст https://youtu.be/abcdefghijk привет")
    await cog.on_message(msg)
    cog.engine.enqueue_single.assert_called_once_with(msg, "https://youtu.be/abcdefghijk")


@pytest.mark.asyncio
async def test_youtube_watch_url_enqueues(cog):
    msg = make_message("https://www.youtube.com/watch?v=abcdefghijk&t=30")
    await cog.on_message(msg)
    cog.engine.enqueue_single.assert_called_once()


@pytest.mark.asyncio
async def test_short_text_no_search(cog):
    msg = make_message("ab")
    await cog.on_message(msg)
    cog.engine.start_search.assert_not_called()


@pytest.mark.asyncio
async def test_long_text_triggers_search(cog):
    msg = make_message("test search query")
    await cog.on_message(msg)
    cog.engine.start_search.assert_called_once()


@pytest.mark.asyncio
async def test_audio_attachment_enqueues(cog):
    att = MagicMock()
    att.size = 1024
    att.filename = "test.mp3"
    with patch("cogs.listener.is_audio", return_value=True), \
         patch("cogs.listener.download_attachment", new_callable=AsyncMock, return_value="path/to/file.mp3"):
        msg = make_message("", attachments=[att])
        await cog.on_message(msg)

    cog.engine.enqueue_local.assert_called_once()
    cog.engine.cleanup_uploads.assert_called_once()


@pytest.mark.asyncio
async def test_non_audio_attachment_falls_through_to_search(cog):
    att = MagicMock()
    with patch("cogs.listener.is_audio", return_value=False):
        msg = make_message("test text", attachments=[att])
        await cog.on_message(msg)

    cog.engine.enqueue_local.assert_not_called()
    cog.engine.start_search.assert_called_once()


@pytest.mark.asyncio
async def test_multiple_audio_attachments_all_enqueued(cog):
    att1, att2 = MagicMock(), MagicMock()
    att1.size = att2.size = 1024
    att1.filename = "1.mp3"
    att2.filename = "2.mp3"
    with patch("cogs.listener.is_audio", return_value=True), \
         patch("cogs.listener.download_attachment", new_callable=AsyncMock,
               side_effect=["path/1.mp3", "path/2.mp3"]):
        msg = make_message("", attachments=[att1, att2])
        await cog.on_message(msg)

    assert cog.engine.enqueue_local.call_count == 2
    cog.engine.cleanup_uploads.assert_called_once()


@pytest.mark.asyncio
async def test_download_failure_continues(cog):
    att = MagicMock()
    att.size = 1024
    att.filename = "test.mp3"
    with patch("cogs.listener.is_audio", return_value=True), \
         patch("cogs.listener.download_attachment", new_callable=AsyncMock,
               side_effect=Exception("fail")):
        msg = make_message("", attachments=[att])
        await cog.on_message(msg)

    cog.engine.enqueue_local.assert_not_called()
    msg.channel.send.assert_called_once()
