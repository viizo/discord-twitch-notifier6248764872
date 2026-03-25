import discord
from discord import app_commands
from discord.ext import tasks
import aiohttp
import asyncio
import os

# === Discord settings ===
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ.get("GUILD_ID", "1486300828699463680"))
CHANNEL_ID = int(os.environ.get("CHANNEL_ID", "1486316228992569344"))

# === Twitch settings ===
TWITCH_CLIENT_ID = os.environ["TWITCH_CLIENT_ID"]
TWITCH_CLIENT_SECRET = os.environ["TWITCH_CLIENT_SECRET"]
streamers = ["Im_MrJokerTwitch", "Purpelain"]

# Stores which streams are currently live
live_status = {}

# === Discord bot setup ===
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# === Twitch API functions ===
async def get_twitch_token():
    url = "https://id.twitch.tv/oauth2/token"
    params = {
        "client_id": TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
        "grant_type": "client_credentials"
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, params=params) as resp:
            data = await resp.json()
            return data["access_token"]

async def is_live(username, token):
    url = f"https://api.twitch.tv/helix/streams?user_login={username}"
    headers = {
        "Client-ID": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {token}"
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            return len(data["data"]) > 0, data["data"][0]["title"] if data["data"] else None

# === Bot commands ===
@tree.command(name="ping", description="Check if bot works")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!")

# === Task: check Twitch streams ===
@tasks.loop(minutes=2)
async def check_streams():
    token = await get_twitch_token()
    channel = client.get_channel(CHANNEL_ID)
    for streamer in streamers:
        live, _ = await is_live(streamer, token)  # we ignore the title now
        if live and not live_status.get(streamer, False):
            twitch_url = f"https://www.twitch.tv/{streamer}"
            await channel.send(f"{streamer} is live!\n{twitch_url}")
            live_status[streamer] = True
        elif not live:
            live_status[streamer] = False

# === Bot events ===
@client.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    check_streams.start()
    print(f"Logged in as {client.user}")

# === Run bot ===
client.run(TOKEN)