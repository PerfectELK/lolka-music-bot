"""Команда !bot: панель управления ботом (кнопки вместо длинных команд)."""

from lolka.ext import commands

from bot_panel import open_panel


class PanelCog(commands.Cog):
    def __init__(self, bot, engine):
        self.bot = bot
        self.engine = engine
        self.panels = {}

    @commands.command(name="bot")
    async def panel_cmd(self, ctx):
        """Панель управления ботом

        Открывает кнопочную панель: выбор плейлиста и действия с ним
        (▶ играть, 🔀 вперемешку, 📋 показать, ➕ создать, 🗑 удалить).
        ▶/🔀 подключат бота к твоему голосовому каналу.
        """
        await open_panel(self.engine, ctx.guild.id, ctx.channel, self.panels)
