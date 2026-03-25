import discord
from discord import app_commands
from discord.ext import tasks
import aiohttp
import asyncio
import os
import datetime
import sqlite3

# === ENV ===
TOKEN = os.environ["DISCORD_TOKEN"]
TWITCH_CLIENT_ID = os.environ["TWITCH_CLIENT_ID"]
TWITCH_CLIENT_SECRET = os.environ["TWITCH_CLIENT_SECRET"]

# === DATABASE ===
conn = sqlite3.connect("servers.db")
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

# === DISCORD ===
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# === GLOBALS ===
session = None
twitch_token = None
token_expiry = None

# === TWITCH TOKEN ===
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
        token_expiry = datetime.datetime.utcnow() + datetime.timedelta(days=50)
        return twitch_token

# === TWITCH API ===
async def get_streams(usernames):
    token = await get_twitch_token()

    params = [("user_login", u) for u in usernames]
    headers = {
        "Client-ID": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {token}"
    }

    async with session.get("https://api.twitch.tv/helix/streams", headers=headers, params=params) as resp:
        if resp.status != 200:
            return []
        data = await resp.json()
        return data.get("data", [])

async def get_profiles(usernames):
    token = await get_twitch_token()

    params = [("login", u) for u in usernames]
    headers = {
        "Client-ID": TWITCH_CLIENT_ID,
        "Authorization": f"Bearer {token}"
    }

    async with session.get("https://api.twitch.tv/helix/users", headers=headers, params=params) as resp:
        if resp.status != 200:
            return {}
        data = await resp.json()
        return {u["login"].lower(): u["profile_image_url"] for u in data.get("data", [])}

# === ADMIN CHECK ===
def is_admin():
    async def predicate(interaction: discord.Interaction):
        return interaction.user.guild_permissions.manage_guild or interaction.user.id == interaction.guild.owner_id
    return app_commands.check(predicate)

# === COMMANDS ===

@tree.command(name="ping", description="Check if bot works")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!")

@tree.command(name="add_streamer", description="Add a Twitch streamer")
@is_admin()
async def add_streamer(interaction: discord.Interaction, streamer_name: str):
    guild_id = str(interaction.guild_id)
    streamer_name = streamer_name.lower()

    profiles = await get_profiles([streamer_name])
    profile_url = profiles.get(streamer_name)

    cursor.execute("""
        INSERT OR IGNORE INTO streamers VALUES (?, ?, ?)
    """, (guild_id, streamer_name, profile_url))
    conn.commit()

    await interaction.response.send_message(f"Added `{streamer_name}`.")

@tree.command(name="remove_streamer", description="Remove a Twitch streamer")
@is_admin()
async def remove_streamer(interaction: discord.Interaction, streamer_name: str):
    guild_id = str(interaction.guild_id)
    streamer_name = streamer_name.lower()

    cursor.execute("DELETE FROM streamers WHERE guild_id=? AND streamer_name=?", (guild_id, streamer_name))
    conn.commit()

    await interaction.response.send_message(f"Removed `{streamer_name}`.")

@tree.command(name="list_streamers", description="List streamers")
async def list_streamers(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)

    cursor.execute("SELECT streamer_name FROM streamers WHERE guild_id=?", (guild_id,))
    rows = cursor.fetchall()

    if rows:
        await interaction.response.send_message("\n".join(r[0] for r in rows))
    else:
        await interaction.response.send_message("No streamers added.")

@tree.command(name="set_channel", description="Set notification channel")
@is_admin()
async def set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    guild_id = str(interaction.guild_id)

    cursor.execute("""
        INSERT INTO guilds (guild_id, channel_id)
        VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id
    """, (guild_id, channel.id))

    conn.commit()
    await interaction.response.send_message(f"Channel set to {channel.mention}")

@tree.command(name="set_role", description="Set role to ping")
@is_admin()
async def set_role(interaction: discord.Interaction, role: discord.Role):
    guild_id = str(interaction.guild_id)

    cursor.execute("""
        INSERT INTO guilds (guild_id, role_id)
        VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET role_id=excluded.role_id
    """, (guild_id, role.id))

    conn.commit()
    await interaction.response.send_message(f"{role.mention} will be pinged.")

@tree.command(name="test", description="Send a test notification")
@is_admin()
async def test(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)

    cursor.execute("SELECT channel_id, role_id FROM guilds WHERE guild_id=?", (guild_id,))
    result = cursor.fetchone()

    if not result:
        await interaction.response.send_message("Set a channel first with `/set_channel`.", ephemeral=True)
        return

    channel_id, role_id = result
    channel = client.get_channel(channel_id)

    embed = discord.Embed(
        title="Test Stream",
        url="https://twitch.tv/",
        color=0x9146FF
    )
    embed.add_field(name="Game", value="Just Chatting")
    embed.set_image(url="https://static-cdn.jtvnw.net/previews-ttv/live_user_test-640x360.jpg")

    role_ping = f"<@&{role_id}>" if role_id else ""

    await channel.send(f"{role_ping}\nTest streamer is live!", embed=embed)
    await interaction.response.send_message("Test sent!", ephemeral=True)

# === LOOP ===
@tasks.loop(minutes=2)
async def check_streams():
    cursor.execute("SELECT DISTINCT guild_id FROM streamers")
    guilds = cursor.fetchall()

    for (guild_id,) in guilds:
        cursor.execute("SELECT channel_id, role_id FROM guilds WHERE guild_id=?", (guild_id,))
        guild_data = cursor.fetchone()
        if not guild_data:
            continue

        channel = client.get_channel(guild_data[0])
        role_id = guild_data[1]

        cursor.execute("SELECT streamer_name, profile_url FROM streamers WHERE guild_id=?", (guild_id,))
        rows = cursor.fetchall()

        names = [r[0] for r in rows]
        profiles = {r[0]: r[1] for r in rows}

        live = await get_streams(names)
        live_map = {s["user_login"].lower(): s for s in live}

        for name in names:
            cursor.execute("SELECT stream_id FROM live_status WHERE guild_id=? AND streamer_name=?", (guild_id, name))
            prev = cursor.fetchone()
            prev_id = prev[0] if prev else None

            current = live_map.get(name)
            current_id = current["id"] if current else None

            if current and current_id != prev_id:
                embed = discord.Embed(
                    title=current["title"],
                    url=f"https://twitch.tv/{name}",
                    color=0x9146FF,
                    timestamp=datetime.datetime.utcnow()
                )
                embed.add_field(name="Game", value=current["game_name"])
                embed.set_image(url=current["thumbnail_url"].replace("{width}", "640").replace("{height}", "360"))

                if profiles.get(name):
                    embed.set_thumbnail(url=profiles[name])

                role_ping = f"<@&{role_id}>" if role_id else ""
                await channel.send(f"{role_ping}\n**{name} is live!**", embed=embed)

            cursor.execute("""
                INSERT OR REPLACE INTO live_status VALUES (?, ?, ?)
            """, (guild_id, name, current_id))

        conn.commit()

# === EVENTS ===
@client.event
async def on_ready():
    global session
    session = aiohttp.ClientSession()
    await tree.sync()
    check_streams.start()
    print(f"Logged in as {client.user}")

# === RUN ===
client.run(TOKEN)
