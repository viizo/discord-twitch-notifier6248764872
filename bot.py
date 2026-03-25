import discord
from discord import app_commands
from discord.ext import tasks
import aiohttp
import asyncio
import os
import json
import datetime

# === YOUR SERVER ID (IMPORTANT) ===
GUILD_ID = 1486300828699463680

# === Discord bot token ===
TOKEN = os.environ["DISCORD_TOKEN"]

# === Twitch settings ===
TWITCH_CLIENT_ID = os.environ["TWITCH_CLIENT_ID"]
TWITCH_CLIENT_SECRET = os.environ["TWITCH_CLIENT_SECRET"]

# JSON file to store server data
DATA_FILE = "servers.json"

# Load data
try:
    with open(DATA_FILE, "r") as f:
        servers = json.load(f)
except FileNotFoundError:
    servers = {}

# === Discord setup ===
intents = discord.Intents.default()
intents.guilds = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# === Twitch API ===
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

async def get_stream_data(username, token):
    url = f"https://api.twitch.tv/helix/streams?user_login={username}"
    headers = {
        "Client-ID": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {token}"
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            if len(data["data"]) == 0:
                return None
            return data["data"][0]

async def get_user_profile(username, token):
    url = f"https://api.twitch.tv/helix/users?login={username}"
    headers = {
        "Client-ID": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {token}"
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            if len(data["data"]) == 0:
                return None
            return data["data"][0]["profile_image_url"]

# === Save ===
def save_servers():
    with open(DATA_FILE, "w") as f:
        json.dump(servers, f, indent=4)

# === Commands ===

@tree.command(name="ping", description="Check if bot works", guild=discord.Object(id=GUILD_ID))
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!")

@tree.command(name="add_streamer", description="Add a Twitch streamer", guild=discord.Object(id=GUILD_ID))
async def add_streamer(interaction: discord.Interaction, streamer_name: str):
    guild_id = str(interaction.guild_id)

    if guild_id not in servers:
        servers[guild_id] = {
            "channel_id": interaction.channel_id,
            "streamers": [],
            "role_id": None
        }

    if streamer_name.lower() not in [s.lower() for s in servers[guild_id]["streamers"]]:
        servers[guild_id]["streamers"].append(streamer_name)
        save_servers()
        await interaction.response.send_message(f"Added `{streamer_name}`.")
    else:
        await interaction.response.send_message("Already added.")

@tree.command(name="remove_streamer", description="Remove a Twitch streamer", guild=discord.Object(id=GUILD_ID))
async def remove_streamer(interaction: discord.Interaction, streamer_name: str):
    guild_id = str(interaction.guild_id)

    if guild_id in servers and streamer_name in servers[guild_id]["streamers"]:
        servers[guild_id]["streamers"].remove(streamer_name)
        save_servers()
        await interaction.response.send_message(f"Removed `{streamer_name}`.")
    else:
        await interaction.response.send_message("Not found.")

@tree.command(name="list_streamers", description="List all streamers", guild=discord.Object(id=GUILD_ID))
async def list_streamers(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)

    if guild_id in servers and servers[guild_id]["streamers"]:
        await interaction.response.send_message(
            "\n".join(servers[guild_id]["streamers"])
        )
    else:
        await interaction.response.send_message("No streamers added.")

@tree.command(name="set_role", description="Set a role to ping", guild=discord.Object(id=GUILD_ID))
async def set_role(interaction: discord.Interaction, role: discord.Role):
    guild_id = str(interaction.guild_id)

    if guild_id not in servers:
        servers[guild_id] = {
            "channel_id": interaction.channel_id,
            "streamers": [],
            "role_id": None
        }

    servers[guild_id]["role_id"] = role.id
    save_servers()

    await interaction.response.send_message(f"{role.mention} will now be pinged.")

@tree.command(name="set_channel", description="Set notification channel", guild=discord.Object(id=GUILD_ID))
async def set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    guild_id = str(interaction.guild_id)

    if guild_id not in servers:
        servers[guild_id] = {
            "channel_id": channel.id,
            "streamers": [],
            "role_id": None
        }
    else:
        servers[guild_id]["channel_id"] = channel.id

    save_servers()

    await interaction.response.send_message(f"Notifications will be sent in {channel.mention}")

# === Stream checker ===
live_status = {}

@tasks.loop(minutes=2)
async def check_streams():
    token = await get_twitch_token()

    for guild_id, data in servers.items():
        channel = client.get_channel(data["channel_id"])
        if not channel:
            continue

        for streamer in data["streamers"]:
            stream_data = await get_stream_data(streamer, token)
            key = f"{guild_id}_{streamer}"

            if stream_data and not live_status.get(key, False):
                title = stream_data["title"]
                game_name = stream_data["game_name"]
                thumbnail_url = stream_data["thumbnail_url"].replace("{width}", "640").replace("{height}", "360")

                profile_pic = await get_user_profile(streamer, token)
                twitch_url = f"https://www.twitch.tv/{streamer}"

                embed = discord.Embed(
                    title=title,
                    url=twitch_url,
                    color=0x9146FF,
                    timestamp=datetime.datetime.utcnow()
                )

                embed.add_field(name="Game", value=game_name, inline=True)

                if profile_pic:
                    embed.set_thumbnail(url=profile_pic)

                embed.set_image(url=thumbnail_url)

                embed.set_footer(
                    text="Twitch",
                    icon_url="https://static.twitchcdn.net/assets/favicon-32-e29e246c157142c94346.png"
                )

                role_id = data.get("role_id")
                role_ping = f"<@&{role_id}>" if role_id else ""

                await channel.send(
                    f"{role_ping}\n**{streamer} is live on Twitch!**\n{twitch_url}",
                    embed=embed
                )

                live_status[key] = True

            elif not stream_data:
                live_status[key] = False

# === Ready ===
@client.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    await tree.sync(guild=guild)
    check_streams.start()
    print(f"Logged in as {client.user}")

# === Run ===
client.run(TOKEN)
