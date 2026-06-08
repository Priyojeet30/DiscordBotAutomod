import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import sys
from dotenv import load_dotenv


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import init_db

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.all()
bot     = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash command(s) globally.")
    except Exception as e:
        print(f"❌ Error syncing commands: {e}")


async def main():
    async with bot:
        await init_db()
        await bot.load_extension("automod")
        print("✅ AutoMod cog loaded.")
        await bot.start(TOKEN)


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\n✅ Bot disconnected successfully.")
except Exception as e:
    print(f"\n❌ Bot crashed: {e}")
