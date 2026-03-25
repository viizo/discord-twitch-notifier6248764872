import discord
from discord import app_commands
from discord.ext import tasks
import aiohttp
import asyncio
import os
import datetime
import sqlite3

# === Discord and Twitch credentials ===
TOKEN = os.environ["DISCORD_TOKEN"]
TWITCH_CLIENT_ID = os.environ["TWITCH_CLIENT_ID"]
TWITCH_CLIENT_SECRET = os.environ["TWITCH_CLIENT_SECRET"]

# === SQLite setup ===
DB_FILE = "servers.db"

conn = sqlite3.connect(DB_FILE)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS guilds (
    guild_id TEXT PRIMARY KEY,
    channel_id INTEGER,
    role_id INTEGER
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS streamers (
    guild_id TEXT,
    streamer_name TEXT,
    profile_url TEXT,
    PRIMARY KEY(guild_id, streamer_name)
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS live_status (
    guild_id TEXT,
    streamer_name TEXT,
    stream_id TEXT,
    PRIMARY KEY(guild_id, streamer_name)
)
""")
conn.commit()

# === Discord setup ===
intents = discord.Intents.default()
intents.guilds = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# === HTTP Session and Twitch Token ===
session: aiohttp.ClientSession = None
twitch_token: str = None
token_expiry: datetime.datetime = None

async def get_twitch_token():
    global twitch_token, token_expiry
    if twitch_token and token_expiry and datetime.datetime.utcnow() < token_expiry:
        return twitch_token

    url = "https://id.twitch.tv/oauth2/token"
    params = {
        "client_id": TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
        "grant_type": "client_credentials"
    }
    async with session.post(url, params=params) as resp:
        data = await resp.json()
        twitch_token = data["access_token"]
        # Tokens typically last ~60 days; refresh a bit early just in case
        token_expiry = datetime.datetime.utcnow() + datetime.timedelta(days=50)
        return twitch_token

# === Twitch API Helpers ===
async def get_streams(usernames: list):
    """
    Batch request for multiple streamers.
    Returns list of live stream data.
    """
    token = await get_twitch_token()
    params = [("user_login", name) for name in usernames]
    headers = {
        "Client-ID": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {token}"
    }
    url = "https://api.twitch.tv/helix/streams"

    async with session.get(url, headers=headers, params=params) as resp:
        if resp.status != 200:
            return []
        data = await resp.json()
        return data.get("data", [])

async def get_user_profiles(usernames: list):
    """
    Batch request for Twitch profiles.
    Returns dict {username: profile_url}
    """
    token = await get_twitch_token()
    params = [("login", name) for name in usernames]
    headers = {
        "Client-ID": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {token}"
    }
    url = "https://api.twitch.tv/helix/users"

    async with session.get(url, headers=headers, params=params) as resp:
        if resp.status != 200:
            return {}
        data = await resp.json()
        result = {}
        for user in data.get("data", []):
            result[user["login"].lower()] = user["profile_image_url"]
        return result

# === Commands ===

@tree.command(name="ping", description="Check if bot works")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!")

@tree.command(name="add_streamer", description="Add a Twitch streamer")
async def add_streamer(interaction: discord.Interaction, streamer_name: str):
    guild_id = str(interaction.guild_id)
    streamer_name = streamer_name.lower()

    # Get profile image
    profile = await get_user_profiles([streamer_name])
    profile_url = profile.get(streamer_name)

    cursor.execute("""
        INSERT OR IGNORE INTO streamers (guild_id, streamer_name, profile_url)
        VALUES (?, ?, ?)
    """, (guild_id, streamer_name, profile_url))
    conn.commit()

    await interaction.response.send_message(f"Added `{streamer_name}`.")

@tree.command(name="remove_streamer", description="Remove a Twitch streamer")
async def remove_streamer(interaction: discord.Interaction, streamer_name: str):
    guild_id = str(interaction.guild_id)
    streamer_name = streamer_name.lower()

    cursor.execute("""
        DELETE FROM streamers WHERE guild_id = ? AND streamer_name = ?
    """, (guild_id, streamer_name))
    conn.commit()

    await interaction.response.send_message(f"Removed `{streamer_name}`.")

@tree.command(name="list_streamers", description="List all streamers")
async def list_streamers(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    cursor.execute("SELECT streamer_name FROM streamers WHERE guild_id = ?", (guild_id,))
    rows = cursor.fetchall()
    if rows:
        await interaction.response.send_message("\n".join(r[0] for r in rows))
    else:
        await interaction.response.send_message("No streamers added.")

@tree.command(name="set_role", description="Set a role to ping")
async def set_role(interaction: discord.Interaction, role: discord.Role):
    guild_id = str(interaction.guild_id)
    cursor.execute("""
        INSERT OR REPLACE INTO guilds (guild_id, role_id)
        VALUES (?, ?)
    """, (guild_id, role.id))
    conn.commit()
    await interaction.response.send_message(f"{role.mention} will now be pinged.")

@tree.command(name="set_channel", description="Set notification channel")
async def set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    guild_id = str(interaction.guild_id)
    cursor.execute("""
        INSERT OR REPLACE INTO guilds (guild_id, channel_id)
        VALUES (?, ?)
    """, (guild_id, channel.id))
    conn.commit()
    await interaction.response.send_message(f"Notifications will be sent in {channel.mention}")

# === Stream Checker ===
@tasks.loop(minutes=2)
async def check_streams():
    cursor.execute("SELECT DISTINCT guild_id FROM streamers")
    guilds = cursor.fetchall()

    for (guild_id,) in guilds:
        cursor.execute("SELECT channel_id, role_id FROM guilds WHERE guild_id = ?", (guild_id,))
        guild_data = cursor.fetchone()
        if not guild_data:
            continue
        channel_id, role_id = guild_data
        channel = client.get_channel(channel_id)
        if not channel:
            continue

        # Get all streamers for this guild
        cursor.execute("SELECT streamer_name, profile_url FROM streamers WHERE guild_id = ?", (guild_id,))
        rows = cursor.fetchall()
        streamer_names = [r[0] for r in rows]
        profiles = {r[0]: r[1] for r in rows}

        if not streamer_names:
            continue

        live_streams = await get_streams(streamer_names)

        # Track live status by stream ID
        live_ids = {}
        for stream in live_streams:
            live_ids[stream["user_login"].lower()] = stream["id"]

        for streamer in streamer_names:
            cursor.execute("""
                SELECT stream_id FROM live_status
                WHERE guild_id = ? AND streamer_name = ?
            """, (guild_id, streamer))
            result = cursor.fetchone()
            prev_live_id = result[0] if result else None
            current_live_id = live_ids.get(streamer)

            # Stream just went live
            if current_live_id and current_live_id != prev_live_id:
                stream_data = next((s for s in live_streams if s["user_login"].lower() == streamer), None)
                if not stream_data:
                    continue

                title = stream_data["title"]
                game_name = stream_data["game_name"]
                thumbnail_url = stream_data["thumbnail_url"].replace("{width}", "640").replace("{height}", "360")
                profile_pic = profiles.get(streamer)
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
                role_ping = f"<@&{role_id}>" if role_id else ""
                await channel.send(f"{role_ping}\n**{streamer} is live on Twitch!**\n{twitch_url}", embed=embed)

            # Update live_status
            cursor.execute("""
                INSERT OR REPLACE INTO live_status (guild_id, streamer_name, stream_id)
                VALUES (?, ?, ?)
            """, (guild_id, streamer, current_live_id))
        conn.commit()

# === Events ===
@client.event
async def on_ready():
    global session
    session = aiohttp.ClientSession()
    await tree.sync()
    check_streams.start()
    print(f"Logged in as {client.user}")

@client.event
async def on_close():
    await session.close()
    conn.close()

# === Run ===
client.run(TOKEN)
