"""Админ-команды: статус бота."""

from lolka.ext import commands

from config import CACHE_MAX_BYTES
from ui_utils import esc

_GB = 1024**3
_MB = 1024**2


class AdminCog(commands.Cog):
    def __init__(self, bot, engine):
        self.bot = bot
        self.engine = engine

    @commands.command(name="status")
    async def status_cmd(self, ctx):
        """Статус бота: активные сервера, очередь, кеш, uptime

        Доступно только владельцу бота на сервере.
        """
        if not self.engine.is_owner(ctx.guild.id, ctx.author.id):
            await ctx.send("Эта команда только для владельца бота на сервере.")
            return
        s = self.engine.get_status()
        uptime_h = int(s["uptime"] // 3600)
        uptime_m = int((s["uptime"] % 3600) // 60)
        cache_mb = s["cache_bytes"] / _MB
        cache_max_mb = s["cache_max"] / _MB
        upload_mb = s["upload_bytes"] / _MB
        upload_max_mb = s["upload_max"] / _MB

        lines = [
            "📊 **Статус бота**",
            "",
            f"- Серверов: {s['servers']}",
            f"- Активных голосовых сессий: {s['active_sessions']}",
            f"- Треков в очередях: {s['total_queue']}",
            f"- Кеш: {cache_mb:.0f} МБ / {cache_max_mb:.0f} МБ ({s['cache_files']} треков)",
            f"- Загрузки: {upload_mb:.0f} МБ / {upload_max_mb:.0f} МБ",
            f"- Uptime: {uptime_h}ч {uptime_m}м",
        ]
        if s["active"]:
            lines += ["", "**Активные сервера:**"]
            for info in s["active"]:
                line = f"- **{esc(info['name'])}**: {info['status']}"
                if info["playing"]:
                    line += f" «{esc(info['playing'])}»"
                if info["queue"]:
                    line += f", в очереди {info['queue']}"
                lines.append(line)

        await ctx.send("\n".join(lines))
