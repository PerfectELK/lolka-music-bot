"""Голосовые команды и управление очередью."""

import random

from lolka.ext import commands

from config import DEFAULT_VOLUME, VOLUME_MAX, VOLUME_MIN, YOUTUBE_RE
from ui_utils import esc, fmt_duration, paginate
from permissions_db import deny_text


class MusicCog(commands.Cog):
    def __init__(self, bot, engine):
        self.bot = bot
        self.engine = engine

    async def _require_perm(self, ctx, perm: str) -> bool:
        """Проверить право: ensure_owner + has_perm, при отказе — deny_text."""
        await self.engine.ensure_owner(ctx.guild)
        if self.engine.has_perm(ctx.guild.id, ctx.author.id, perm):
            return True
        await ctx.send(deny_text(perm))
        return False

    @commands.command(name="join")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def join_cmd(self, ctx):
        """Подключиться к голосовому каналу"""
        vc, err = await self.engine.ensure_voice(ctx.message)
        if err is not None:
            await ctx.send(err)
            return
        await ctx.send(f"Подключился к **{vc.channel.name}**")

    @commands.command(name="play")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def play_cmd(self, ctx, *, query: str):
        """Сыграть трек по ссылке на YouTube или найти по тексту

        Примеры: `!play https://youtube.com/watch?v=...` — по ссылке,
        `!play <текст>` — поиск по YouTube (выбери результат реакцией).
        Ссылку на трек бот пришлёт в сообщении «Сейчас играет».
        """
        m = YOUTUBE_RE.search(query)
        if m:
            await self.engine.enqueue_single(ctx.message, m.group(0))
            return
        await self.engine.start_search(ctx.guild.id, ctx.author.id, ctx.channel, query)

    @commands.command(name="skip")
    @commands.cooldown(2, 1, commands.BucketType.user)
    async def skip_cmd(self, ctx):
        """Следующий трек"""
        if not await self._require_perm(ctx, "control"):
            return
        status = self.engine.skip_current(ctx.guild.id)
        if status == "nothing":
            await ctx.send("Сейчас ничего не играет.")
        elif status == "last":
            await ctx.send("Это был последний трек плейлиста.")
        else:
            await ctx.send("Следующий трек...")

    @commands.command(name="next")
    @commands.cooldown(2, 1, commands.BucketType.user)
    async def next_cmd(self, ctx):
        """Следующий трек (как !skip)"""
        if not await self._require_perm(ctx, "control"):
            return
        await self.skip_cmd(ctx)

    @commands.command(name="prev")
    @commands.cooldown(2, 1, commands.BucketType.user)
    async def prev_cmd(self, ctx):
        """Предыдущий трек"""
        if not await self._require_perm(ctx, "control"):
            return
        status = self.engine.prev_current(ctx.guild.id)
        if status == "nothing":
            await ctx.send("Сейчас ничего не играет.")
        elif status == "no_prev":
            await ctx.send("Нет предыдущего трека.")
        else:
            q = self.engine.queues.get(ctx.guild.id, [])
            item = q[0] if q else None
            await ctx.send(
                f"Предыдущий трек: **{esc(item['title'])}**"
                if item
                else "Предыдущий трек..."
            )

    @commands.command(name="loop")
    @commands.cooldown(2, 1, commands.BucketType.user)
    async def loop_cmd(self, ctx):
        """Включить/выключить цикл очереди (по умолчанию включён)"""
        if not await self._require_perm(ctx, "control"):
            return
        on = self.engine.loop_toggle(ctx.guild.id)
        await ctx.send(
            "Цикл **включён**: после последнего трека заиграет первый."
            if on
            else "Цикл выключен."
        )

    @commands.command(name="stop")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def stop_cmd(self, ctx):
        """Остановить воспроизведение и очистить очередь"""
        if not await self._require_perm(ctx, "control"):
            return
        vc = ctx.guild.voice_client
        if vc is None:
            await ctx.send("Бот не в голосовом канале.")
            return
        self.engine.clear_guild(ctx.guild.id)
        vc.stop()
        await ctx.send("Остановил и очистил очередь.")

    @commands.command(name="leave")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def leave_cmd(self, ctx):
        """Выйти из голосового канала"""
        if not await self._require_perm(ctx, "control"):
            return
        vc = ctx.guild.voice_client
        if vc is None:
            await ctx.send("Бот не в голосовом канале.")
            return
        await self.engine.stop_and_leave(ctx.guild.id)
        await ctx.send("Вышел из голосового канала.")

    @commands.command(name="queue")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def queue_cmd(self, ctx, page: int = 1):
        """Показать ближайшие треки очереди (постранично)

        Пример: `!queue` — первая страница, `!queue 2` — вторая.
        """
        q = self.engine.queues.get(ctx.guild.id, [])
        if not q:
            await ctx.send("Очередь пуста.")
            return
        chunk, pages, _, _, start = paginate(q, page)
        if chunk is None:
            await ctx.send(f"Страница {page} не найдена — всего {pages}.")
            return
        lines = [
            f"{i}. {esc(item['title'])}" + (f" ({fmt_duration(item.get('duration'))})" if item.get("duration") else "")
            for i, item in enumerate(chunk, start=start)
        ]
        header = f"Очередь ({len(q)} треков)"
        if pages > 1:
            header += f" — стр. {page}/{pages} (`!queue <номер>`)"
        await ctx.send(header + ":\n" + "\n".join(lines))

    @commands.command(name="remove")
    @commands.cooldown(2, 1, commands.BucketType.user)
    async def remove_cmd(self, ctx, position: int):
        """Убрать трек из очереди по номеру (1 — следующий после играющего)"""
        if not await self._require_perm(ctx, "control"):
            return
        q = self.engine.queues.get(ctx.guild.id, [])
        if position < 1 or position > len(q):
            await ctx.send(f"Трек {position} не найден в очереди (всего {len(q)}).")
            return
        entry = q.pop(position - 1)
        fut = entry.get("future")
        if fut is not None and not fut.done():
            fut.cancel()
        self.engine.sync_nav_after_mutation(ctx.guild.id)
        await ctx.send(f"Убрал из очереди: **{esc(entry['title'])}**")

    @commands.command(name="move")
    @commands.cooldown(2, 1, commands.BucketType.user)
    async def move_cmd(self, ctx, from_pos: int, to_pos: int):
        """Переместить трек в очереди: !move <откуда> <куда>"""
        if not await self._require_perm(ctx, "control"):
            return
        q = self.engine.queues.get(ctx.guild.id, [])
        if from_pos < 1 or from_pos > len(q):
            await ctx.send(f"Трек {from_pos} не найден в очереди (всего {len(q)}).")
            return
        if to_pos < 1 or to_pos > len(q):
            await ctx.send(f"Позиция {to_pos} вне очереди (всего {len(q)}).")
            return
        entry = q.pop(from_pos - 1)
        q.insert(to_pos - 1, entry)
        self.engine.sync_nav_after_mutation(ctx.guild.id)
        await ctx.send(f"Переместил **{esc(entry['title'])}** на позицию {to_pos}.")

    @commands.command(name="shuffle")
    @commands.cooldown(2, 1, commands.BucketType.user)
    async def shuffle_cmd(self, ctx):
        """Перемешать будущие треки очереди (играющий не трогается)"""
        if not await self._require_perm(ctx, "control"):
            return
        q = self.engine.queues.get(ctx.guild.id, [])
        if len(q) < 2:
            await ctx.send("В очереди меньше двух треков — перемешивать нечего.")
            return
        random.shuffle(q)
        self.engine.sync_nav_after_mutation(ctx.guild.id)
        await ctx.send(f"Перемешал очередь ({len(q)} треков).")

    @commands.command(name="history")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def history_cmd(self, ctx):
        """Последние проигранные треки"""
        h = self.engine.history.get(ctx.guild.id, [])
        if not h:
            await ctx.send("История пуста.")
            return
        lines = [f"{i}. {esc(item['title'])}" for i, item in enumerate(h[-10:], start=1)]
        await ctx.send("Последние треки:\n" + "\n".join(lines))

    @commands.command(name="pause")
    @commands.cooldown(2, 1, commands.BucketType.user)
    async def pause_cmd(self, ctx):
        """Поставить на паузу"""
        if not await self._require_perm(ctx, "control"):
            return
        vc = ctx.guild.voice_client
        if vc is None or not vc.is_playing():
            await ctx.send("Сейчас ничего не играет.")
            return
        vc.pause()
        await ctx.send("Пауза.")

    @commands.command(name="resume")
    @commands.cooldown(2, 1, commands.BucketType.user)
    async def resume_cmd(self, ctx):
        """Продолжить после паузы"""
        if not await self._require_perm(ctx, "control"):
            return
        vc = ctx.guild.voice_client
        if vc is None or not vc.is_paused():
            await ctx.send("Не на паузе.")
            return
        vc.resume()
        await ctx.send("Продолжаю.")

    @commands.command(name="now")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def now_cmd(self, ctx):
        """Пересоздать панель управления с кнопками в текущем канале"""
        state = self.engine.play_state.get(ctx.guild.id)
        vc = ctx.guild.voice_client
        if vc is None or vc.source is None:
            await ctx.send("Сейчас ничего не играет.")
            return
        title = state.get("title") if state else None
        if not title:
            await ctx.send("Сейчас ничего не играет.")
            return
        text = f"Сейчас играет: **{esc(title)}**"
        if state.get("duration"):
            text += f" ({fmt_duration(state['duration'])})"
        if state.get("page_url"):
            text += f"\n{state['page_url']}"
        await self.engine.report(ctx.guild.id, text, fresh=True, channel=ctx.channel)

    @commands.command(name="volume")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def volume_cmd(self, ctx, vol: float | None = None):
        """Изменить громкость бота на сервере (0.0–2.0, 1.0 = 100%)

        Пример: `!volume 0.5` — тише, `!volume 1.5` — громче,
        `!volume` — показать текущий уровень.
        Громкость применяется к будущим трекам.
        """
        if not await self._require_perm(ctx, "control"):
            return
        if vol is None:
            cur = self.engine.volumes.get(ctx.guild.id, DEFAULT_VOLUME)
            pct = int(cur * 100)
            await ctx.send(f"Текущая громкость: **{pct}%**.")
            return
        if vol < VOLUME_MIN or vol > VOLUME_MAX:
            await ctx.send(f"Громкость должна быть от **{VOLUME_MIN}** до **{VOLUME_MAX}**.")
            return
        clamped = self.engine.set_volume(ctx.guild.id, vol)
        pct = int(clamped * 100)
        await ctx.send(f"Громкость: **{pct}%** (применится к будущим трекам).")
