"""Команда !perms: панель прав бота (выдача/отзыв прав на управление)."""

from lolka.ext import commands

from perms_panel import open_perms_panel


class PermsCog(commands.Cog):
    def __init__(self, bot, engine):
        self.bot = bot
        self.engine = engine

    @commands.command(name="perms")
    async def perms_cmd(self, ctx):
        """Панель прав: выдай/забери права на управление ботом (доступна в #music)"""
        await open_perms_panel(self.engine, ctx, ctx.guild.id)
