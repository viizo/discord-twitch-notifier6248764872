import discord
from discord import app_commands
from discord.ext import tasks
import aiohttp
import asyncio
import os
import json

# === Discord settings ===
TOKEN = os.environ["DISCORD_TOKEN"]

# === Twitch settings ===
TWITCH_CLIENT_ID = os.environ["TWITCH_CLIENT_ID"]
TWITCH_CLIENT_SECRET = os.environ["TWITCH_CLIENT_SECRET"]

# File to store server data
DATA_FILE = "servers.json"

# Load server data from JSON, or create empty dict
try:
    with open(DATA_FILE, "r") as f:
        servers = json.load(f)
except FileNotFoundError:
    servers = {}

# === Discord bot setup ===
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True

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
            return len(data["data"]) > 0

# === Helper functions ===
def save_servers():
    with open(DATA_FILE, "w") as f:
        json.dump(servers, f, indent=4)

# === Bot commands ===
@tree.command(name="ping", description="Check if bot works")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!")

@tree.command(name="add_streamer", description="Add a Twitch streamer to this server")
async def add_streamer(interaction: discord.Interaction, streamer_name: str):
    guild_id = str(interaction.guild_id)
    channel_id = interaction.channel_id
    if guild_id not in servers:
        servers[guild_id] = {"channel_id": channel_id, "streamers": []}
    if streamer_name.lower() not in [s.lower() for s in servers[guild_id]["streamers"]]:
        servers[guild_id]["streamers"].append(streamer_name)
        save_servers()
        await interaction.response.send_message(f"Added streamer `{streamer_name}`.")
    else:
        await interaction.response.send_message(f"Streamer `{streamer_name}` is already in the list.")

@tree.command(name="remove_streamer", description="Remove a Twitch streamer from this server")
async def remove_streamer(interaction: discord.Interaction, streamer_name: str):
    guild_id = str(interaction.guild_id)
    if guild_id in servers and streamer_name in servers[guild_id]["streamers"]:
        servers[guild_id]["streamers"].remove(streamer_name)
        save_servers()
        await interaction.response.send_message(f"Removed streamer `{streamer_name}`.")
    else:
        await interaction.response.send_message(f"Streamer `{streamer_name}` not found in the list.")

@tree.command(name="list_streamers", description="List all Twitch streamers tracked in this server")
async def list_streamers(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    if guild_id in servers and servers[guild_id]["streamers"]:
        streamers_list = "\n".join(servers[guild_id]["streamers"])
        await interaction.response.send_message(f"Tracked streamers:\n{streamers_list}")
    else:
        await interaction.response.send_message("No streamers are tracked in this server yet.")

# === Task: check Twitch streams ===
live_status = {}  # Tracks live state per server per streamer

@tasks.loop(minutes=2)
async def check_streams():
    token = await get_twitch_token()
    for guild_id, data in servers.items():
        channel = client.get_channel(data["channel_id"])
        for streamer in data["streamers"]:
            live = await is_live(streamer, token)
            key = f"{guild_id}_{streamer}"
            if live and not live_status.get(key, False):
                twitch_url = f"https://www.twitch.tv/{streamer}"
                await channel.send(f"{streamer} is live!\n{twitch_url}")
                live_status[key] = True
            elif not live:
                live_status[key] = False

# === Bot events ===
@client.event
async def on_ready():
    await tree.sync()
    check_streams.start()
    print(f"Logged in as {client.user}")

# === Run bot ===
client.run(TOKEN)
