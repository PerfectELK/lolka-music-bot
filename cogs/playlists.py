"""Команды плейлистов (!pl ...)."""

import asyncio

from lolka.ext import commands

from config import YOUTUBE_RE
from engine import friendly_error
from ui_utils import esc, fmt_duration, paginate
from permissions_db import deny_text
from resolver import fetch_info
from upload_util import download_attachment, is_audio, local_url


class PlaylistCog(commands.Cog):
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

    @commands.group(name="pl", invoke_without_command=True)
    async def pl_cmd(self, ctx):
        """Плейлисты: создание, добавление треков, проигрывание

        Треки, скинутые в #music, автоматически сохраняются в плейлист `default`.
        В `!pl add` можно прикреплять аудиофайлы — они сохраняются на сервер
        (папка uploads/) и играются без YouTube.
        """
        await ctx.send(
            "Команды плейлистов:\n"
            "`!pl create <имя>` — создать\n"
            "`!pl add <имя> <ссылка> [<ссылка>...]` — добавить треки "
            "(несколько сразу, можно и аудиофайлом во вложении)\n"
            "`!pl play <имя> [<номер>]` — проиграть (заменяет текущую очередь; "
            "треки докачиваются в фоне по ходу проигрывания; "
            "номер — начать с этого трека, дальше по кругу)\n"
            "`!pl shuffle <имя>` — перемешать треки и проиграть в случайном порядке\n"
            "`!pl replay` — переиграть последний плейлист\n"
            "`!pl list` — список плейлистов\n"
            "`!pl show <имя>` — треки плейлиста\n"
            "`!pl remove <имя> <номер>` — убрать трек\n"
            "`!pl rename <имя> <новое>` — переименовать\n"
            "`!pl delete <имя>` — удалить плейлист\n\n"
            "Кнопка ➕ «В плейлист» на сообщении после поиска и на панели "
            "«Сейчас играет» — добавить трек без команд.\n"
            "Навигация: `!next` / `!skip` — следующий трек, `!prev` — предыдущий.\n"
            "Цикл: `!loop` (по умолчанию включён — после последнего трека играет первый).\n"
            "Треки, скинутые в #music, автоматически сохраняются в плейлист `default`."
        )

    @pl_cmd.command(name="create")
    async def pl_create(self, ctx, name: str):
        """Создать плейлист"""
        if not await self._require_perm(ctx, "playlists"):
            return
        if len(name) > 100:
            await ctx.send("Название плейлиста не должно быть длиннее 100 символов.")
            return
        if self.engine.db.create_playlist(ctx.guild.id, name):
            await ctx.send(f"Плейлист **{esc(name)}** создан.")
        else:
            await ctx.send(f"Плейлист **{esc(name)}** уже существует.")

    @pl_cmd.command(name="add")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def pl_add(self, ctx, name: str, *urls: str):
        """Добавить треки по ссылкам на YouTube или аудио-вложениями (можно несколько сразу)"""
        if not await self._require_perm(ctx, "playlists"):
            return
        attachments = [a for a in ctx.message.attachments if is_audio(a)]
        if not urls and not attachments:
            await ctx.send("Пришли ссылку на YouTube или прикрепи аудиофайл.")
            return
        if self.engine.db.get_playlist(ctx.guild.id, name) is None:
            await ctx.send(f"Плейлист **{esc(name)}** не найден — сначала `!pl create {name}`.")
            return

        added, failed, queued = [], [], []
        for att in attachments:
            try:
                path = await download_attachment(att)
                self.engine.cleanup_uploads()
            except Exception as exc:
                failed.append((att.filename, str(exc)))
                continue
            ok = await asyncio.to_thread(
                self.engine.db.add_track, ctx.guild.id, name, local_url(path), path.stem
            )
            if ok:
                added.append(path.stem)
                if self.engine.on_playlist_track_added(
                    ctx.guild.id, name, local_url(path), path.stem
                ):
                    queued.append(path.stem)
            else:
                failed.append((att.filename, "не удалось сохранить в плейлист"))

        if urls:
            pending = []
            for raw in urls:
                m = YOUTUBE_RE.search(raw)
                pending.append((raw, m.group(0) if m else None))

            done = await asyncio.gather(
                *(fetch_info(url) for _, url in pending if url is not None),
                return_exceptions=True,
            )
            results = iter(done)

            for raw, url in pending:
                if url is None:
                    failed.append((raw, "не похоже на YouTube-ссылку"))
                    continue
                result = next(results)
                if isinstance(result, BaseException):
                    failed.append((raw, friendly_error(result)))
                    continue
                title, page_url, duration = result
                ok = await asyncio.to_thread(
                    self.engine.db.add_track, ctx.guild.id, name, page_url, title, duration
                )
                if ok:
                    added.append(title)
                    if self.engine.on_playlist_track_added(
                        ctx.guild.id, name, page_url, title, duration
                    ):
                        queued.append(title)
                else:
                    failed.append((raw, "не удалось сохранить в плейлист"))

        lines = []
        if added:
            lines.append(f"Добавил в **{esc(name)}**: " + ", ".join(esc(t) for t in added))
        if queued:
            lines.append(
                "Добавил в конец очереди (плейлист сейчас играет) — качаются, "
                "заиграют следом: " + ", ".join(esc(t) for t in queued)
            )
        if added and not queued:
            lines.append(
                f"Треки скачаются при проигрывании **{esc(name)}** "
                "(докачиваются в фоне, по ходу)."
            )
        if failed:
            lines.append("Не получилось: " + "; ".join(f"{esc(raw)} — {err}" for raw, err in failed))
        await ctx.send("\n".join(lines))

    @pl_cmd.command(name="play")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def pl_play(self, ctx, name: str, position: int = 1):
        """Проиграть плейлист с указанного трека, дальше по кругу (заменяет текущую очередь)"""
        await self.engine.play_playlist(ctx, name, position)

    @pl_cmd.command(name="shuffle")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def pl_shuffle(self, ctx, name: str):
        """Перемешать треки плейлиста в случайном порядке и проиграть (заменяет текущую очередь)"""
        await self.engine.play_playlist(ctx, name, shuffle=True)

    @pl_cmd.command(name="replay")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def pl_replay(self, ctx):
        """Переиграть последний плейлист"""
        name = self.engine.last_played.get(ctx.guild.id)
        if name is None:
            await ctx.send("Нет плейлиста для повтора — сначала проиграй какой-нибудь.")
            return
        await self.engine.play_playlist(ctx, name)

    @pl_cmd.command(name="list")
    async def pl_list(self, ctx):
        """Список плейлистов сервера"""
        rows = self.engine.db.list_playlists(ctx.guild.id)
        if not rows:
            await ctx.send("Плейлистов пока нет.")
            return
        await ctx.send("Плейлисты сервера:\n" + "\n".join(f"- **{esc(name)}** ({n} треков)" for name, n in rows))

    @pl_cmd.command(name="show")
    async def pl_show(self, ctx, name: str, page: int = 1):
        """Показать треки плейлиста (постранично)"""
        tracks = self.engine.db.get_playlist(ctx.guild.id, name)
        if tracks is None:
            await ctx.send(f"Плейлист **{esc(name)}** не найден.")
            return
        if not tracks:
            await ctx.send(f"Плейлист **{esc(name)}** пуст.")
            return
        chunk, pages, _, _, start = paginate(tracks, page)
        if chunk is None:
            await ctx.send(f"Страница {page} не найдена — всего {pages}.")
            return
        lines = [
            f"{i}. {esc(title)}" + (f" ({fmt_duration(d)})" if d else "")
            for i, (_, title, d) in enumerate(chunk, start=start)
        ]
        header = f"Плейлист **{esc(name)}** ({len(tracks)} треков)"
        if pages > 1:
            header += f" — стр. {page}/{pages} (всего страниц: {pages}, `!pl show {name} <номер>`)"
        await ctx.send(header + ":\n" + "\n".join(lines))

    @pl_cmd.command(name="remove")
    async def pl_remove(self, ctx, name: str, position: int):
        """Убрать трек по номеру"""
        if not await self._require_perm(ctx, "playlists"):
            return
        if position < 1:
            await ctx.send("Номер трека должен быть >= 1.")
            return
        if self.engine.db.remove_track(ctx.guild.id, name, position):
            await ctx.send(f"Убрал трек {position} из **{esc(name)}**.")
        else:
            await ctx.send(f"Трек {position} не найден в **{esc(name)}**.")

    @pl_cmd.command(name="rename")
    async def pl_rename(self, ctx, name: str, new_name: str):
        """Переименовать плейлист"""
        if not await self._require_perm(ctx, "playlists"):
            return
        if len(new_name) > 100:
            await ctx.send("Название плейлиста не должно быть длиннее 100 символов.")
            return
        if self.engine.db.rename_playlist(ctx.guild.id, name, new_name):
            await ctx.send(f"Переименовал **{esc(name)}** → **{esc(new_name)}**.")
        else:
            await ctx.send("Не удалось переименовать: плейлист не найден или имя занято.")

    @pl_cmd.command(name="delete")
    async def pl_delete(self, ctx, name: str):
        """Удалить плейлист"""
        if not await self._require_perm(ctx, "playlists"):
            return
        if self.engine.db.delete_playlist(ctx.guild.id, name):
            await ctx.send(f"Плейлист **{esc(name)}** удалён.")
        else:
            await ctx.send(f"Плейлист **{esc(name)}** не найден.")
