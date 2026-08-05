"""Команда !help: справка по всем командам бота."""

from lolka.ext import commands

COG_TITLES = {
    "MusicCog": "Голос и очередь",
    "PlaylistCog": "Плейлисты",
}


class HelpCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="help")
    async def help_cmd(self, ctx, *, command: str = ""):
        """Справка по командам бота

        Примеры: `!help` — все команды, `!help pl` — подробно о плейлистах.
        """
        if command:
            text = self._render_command(command)
            if text is None:
                await ctx.send(f"Команда `{command}` не найдена. `!help` — список всех команд.")
                return
        else:
            text = self._render_overview()
        await ctx.send(text)

    def _render_overview(self) -> str:
        lines = ["**Команды бота**", ""]
        for cog in self.bot.cogs.values():
            title = COG_TITLES.get(type(cog).__name__, type(cog).__name__)
            cmds = [c for c in cog.get_commands() if c.name != "help"]
            if not cmds:
                continue
            lines.append(f"__{title}__")
            for cmd in cmds:
                if isinstance(cmd, commands.Group):
                    subs = "|".join(sorted(sub.name for sub in cmd.walk_commands()))
                    lines.append(f"`!{cmd.name} <{subs}>` — {cmd.short_doc}")
                else:
                    lines.append(f"`!{cmd.name}` — {cmd.short_doc}")
            lines.append("")
        lines.append("`!help <команда>` — подробнее о команде (например `!help pl`).")
        return "\n".join(lines)

    def _render_command(self, name: str):
        parts = name.strip().split()
        cmd = self.bot.get_command(parts[0])
        for part in parts[1:]:
            if not isinstance(cmd, commands.Group):
                return None
            cmd = cmd.get_command(part)
            if cmd is None:
                return None
        if cmd is None:
            return None
        usage = f"!{cmd.qualified_name} {cmd.signature}".strip()
        lines = [f"`{usage}`"]
        if cmd.help:
            lines.append("")
            lines.append(cmd.help)
        if isinstance(cmd, commands.Group):
            lines.append("")
            lines.append("Подкоманды:")
            for sub in cmd.walk_commands():
                usage = f"!{sub.qualified_name} {sub.signature}".strip()
                lines.append(f"`{usage}` — {sub.short_doc}")
        return "\n".join(lines)
