import asyncio
from collections import defaultdict
import datetime as dt
import logging
import os
import random
from dataclasses import dataclass
from typing import Optional

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks


TOKEN = os.environ["DISCORD_TOKEN"]
TWITCH_CLIENT_ID = os.environ["TWITCH_CLIENT_ID"]
TWITCH_CLIENT_SECRET = os.environ["TWITCH_CLIENT_SECRET"]

DATABASE_PATH = os.getenv("DATABASE_PATH", "servers.db")
STREAMER_LIMIT = int(os.getenv("STREAMER_LIMIT", "20"))
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "2"))
DEFAULT_MESSAGE = "**{streamer} is live now on Twitch!**"
TWITCH_COLOR = 0x9146FF
TWITCH_BATCH_SIZE = 100


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("twitch-live-bot")


@dataclass(slots=True)
class GuildConfig:
    guild_id: int
    channel_id: Optional[int]
    role_id: Optional[int]
    manager_role_id: Optional[int]
    custom_message: Optional[str]


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None

    async def start(self) -> None:
        if self.conn is not None:
            return

        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self._init_schema()

    async def _init_schema(self) -> None:
        conn = self._require_conn()
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER,
                role_id INTEGER,
                manager_role_id INTEGER,
                custom_message TEXT
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS streamers (
                guild_id INTEGER NOT NULL,
                streamer_name TEXT NOT NULL,
                profile_url TEXT,
                PRIMARY KEY (guild_id, streamer_name)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS live_status (
                guild_id INTEGER NOT NULL,
                streamer_name TEXT NOT NULL,
                stream_id TEXT,
                PRIMARY KEY (guild_id, streamer_name)
            )
            """
        )
        await self._migrate_legacy_schema()
        await conn.commit()

    async def _migrate_legacy_schema(self) -> None:
        conn = self._require_conn()
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ) as cursor:
            table_rows = await cursor.fetchall()

        existing_tables = {row["name"] for row in table_rows}
        legacy_tables = {"guilds", "settings", "manager_roles"}
        if not legacy_tables.issubset(existing_tables):
            return

        await conn.execute(
            """
            INSERT INTO guild_settings (
                guild_id,
                channel_id,
                role_id,
                manager_role_id,
                custom_message
            )
            SELECT
                CAST(keys.guild_id AS INTEGER),
                guilds.channel_id,
                guilds.role_id,
                manager_roles.role_id,
                settings.custom_message
            FROM (
                SELECT guild_id FROM guilds
                UNION
                SELECT guild_id FROM settings
                UNION
                SELECT guild_id FROM manager_roles
            ) AS keys
            LEFT JOIN guilds ON guilds.guild_id = keys.guild_id
            LEFT JOIN settings ON settings.guild_id = keys.guild_id
            LEFT JOIN manager_roles ON manager_roles.guild_id = keys.guild_id
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id = COALESCE(guild_settings.channel_id, excluded.channel_id),
                role_id = COALESCE(guild_settings.role_id, excluded.role_id),
                manager_role_id = COALESCE(
                    guild_settings.manager_role_id,
                    excluded.manager_role_id
                ),
                custom_message = COALESCE(
                    guild_settings.custom_message,
                    excluded.custom_message
                )
            """
        )

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    def _require_conn(self) -> aiosqlite.Connection:
        if self.conn is None:
            raise RuntimeError("Database has not been started yet.")
        return self.conn

    async def get_guild_config(self, guild_id: int) -> GuildConfig:
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT guild_id, channel_id, role_id, manager_role_id, custom_message
            FROM guild_settings
            WHERE guild_id = ?
            """,
            (guild_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return GuildConfig(
                guild_id=guild_id,
                channel_id=None,
                role_id=None,
                manager_role_id=None,
                custom_message=None,
            )

        return GuildConfig(
            guild_id=row["guild_id"],
            channel_id=row["channel_id"],
            role_id=row["role_id"],
            manager_role_id=row["manager_role_id"],
            custom_message=row["custom_message"],
        )

    async def upsert_guild_config(
        self,
        guild_id: int,
        *,
        channel_id: Optional[int] = None,
        role_id: Optional[int] = None,
        manager_role_id: Optional[int] = None,
        custom_message: Optional[str] = None,
    ) -> None:
        current = await self.get_guild_config(guild_id)
        conn = self._require_conn()
        await conn.execute(
            """
            INSERT INTO guild_settings (
                guild_id,
                channel_id,
                role_id,
                manager_role_id,
                custom_message
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                role_id = excluded.role_id,
                manager_role_id = excluded.manager_role_id,
                custom_message = excluded.custom_message
            """,
            (
                guild_id,
                current.channel_id if channel_id is None else channel_id,
                current.role_id if role_id is None else role_id,
                current.manager_role_id if manager_role_id is None else manager_role_id,
                current.custom_message if custom_message is None else custom_message,
            ),
        )
        await conn.commit()

    async def get_streamers(self, guild_id: int) -> list[aiosqlite.Row]:
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT streamer_name, profile_url
            FROM streamers
            WHERE guild_id = ?
            ORDER BY streamer_name COLLATE NOCASE
            """,
            (guild_id,),
        ) as cursor:
            return await cursor.fetchall()

    async def count_streamers(self, guild_id: int) -> int:
        conn = self._require_conn()
        async with conn.execute(
            "SELECT COUNT(*) AS count FROM streamers WHERE guild_id = ?",
            (guild_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["count"])

    async def add_streamer(self, guild_id: int, streamer_name: str, profile_url: str) -> bool:
        conn = self._require_conn()
        try:
            await conn.execute(
                """
                INSERT INTO streamers (guild_id, streamer_name, profile_url)
                VALUES (?, ?, ?)
                """,
                (guild_id, streamer_name, profile_url),
            )
            await conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def remove_streamer(self, guild_id: int, streamer_name: str) -> int:
        conn = self._require_conn()
        await conn.execute(
            "DELETE FROM live_status WHERE guild_id = ? AND streamer_name = ?",
            (guild_id, streamer_name),
        )
        cursor = await conn.execute(
            "DELETE FROM streamers WHERE guild_id = ? AND streamer_name = ?",
            (guild_id, streamer_name),
        )
        await conn.commit()
        return cursor.rowcount

    async def get_all_tracked_streamers(self) -> list[aiosqlite.Row]:
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT guild_id, streamer_name, profile_url
            FROM streamers
            ORDER BY guild_id, streamer_name COLLATE NOCASE
            """
        ) as cursor:
            return await cursor.fetchall()

    async def get_live_statuses(self, guild_id: int) -> dict[str, Optional[str]]:
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT streamer_name, stream_id
            FROM live_status
            WHERE guild_id = ?
            """,
            (guild_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return {row["streamer_name"]: row["stream_id"] for row in rows}

    async def set_live_statuses(
        self, entries: list[tuple[int, str, Optional[str]]]
    ) -> None:
        if not entries:
            return

        conn = self._require_conn()
        await conn.executemany(
            """
            INSERT INTO live_status (guild_id, streamer_name, stream_id)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, streamer_name) DO UPDATE SET
                stream_id = excluded.stream_id
            """,
            entries,
        )
        await conn.commit()


class TwitchClient:
    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.session: Optional[aiohttp.ClientSession] = None
        self.access_token: Optional[str] = None
        self.token_expiry: Optional[dt.datetime] = None

    async def start(self) -> None:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=20)
            self.session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def _get_access_token(self) -> str:
        now = dt.datetime.now(dt.timezone.utc)
        if (
            self.access_token
            and self.token_expiry is not None
            and now < self.token_expiry - dt.timedelta(minutes=5)
        ):
            return self.access_token

        if self.session is None:
            await self.start()

        assert self.session is not None
        async with self.session.post(
            "https://id.twitch.tv/oauth2/token",
            params={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
        ) as response:
            response.raise_for_status()
            data = await response.json()

        self.access_token = data["access_token"]
        self.token_expiry = now + dt.timedelta(seconds=int(data["expires_in"]))
        return self.access_token

    async def _request(self, url: str, params: list[tuple[str, str]]) -> dict:
        token = await self._get_access_token()
        if self.session is None:
            await self.start()

        assert self.session is not None
        headers = {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {token}",
        }
        async with self.session.get(url, headers=headers, params=params) as response:
            if response.status == 401:
                self.access_token = None
                token = await self._get_access_token()
                headers["Authorization"] = f"Bearer {token}"
                async with self.session.get(url, headers=headers, params=params) as retry_response:
                    retry_response.raise_for_status()
                    return await retry_response.json()

            response.raise_for_status()
            return await response.json()

    @staticmethod
    def _chunked(items: list[str], size: int) -> list[list[str]]:
        return [items[index : index + size] for index in range(0, len(items), size)]

    async def get_profiles(self, usernames: list[str]) -> dict[str, dict]:
        if not usernames:
            return {}

        profiles: dict[str, dict] = {}
        for batch in self._chunked(usernames, TWITCH_BATCH_SIZE):
            data = await self._request(
                "https://api.twitch.tv/helix/users",
                [("login", username) for username in batch],
            )
            for entry in data.get("data", []):
                profiles[entry["login"].lower()] = entry
        return profiles

    async def get_streams(self, usernames: list[str]) -> dict[str, dict]:
        if not usernames:
            return {}

        live_streams: dict[str, dict] = {}
        for batch in self._chunked(usernames, TWITCH_BATCH_SIZE):
            data = await self._request(
                "https://api.twitch.tv/helix/streams",
                [("user_login", username) for username in batch],
            )
            for entry in data.get("data", []):
                live_streams[entry["user_login"].lower()] = entry
        return live_streams


class TwitchLiveBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.db = Database(DATABASE_PATH)
        self.twitch = TwitchClient(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET)
        self.tree.on_error = self.on_app_command_error

    async def setup_hook(self) -> None:
        await self.db.start()
        await self.twitch.start()
        if not self.check_streams.is_running():
            self.check_streams.start()

    async def close(self) -> None:
        self.check_streams.cancel()
        await self.twitch.close()
        await self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        synced = await self.tree.sync()
        logger.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")
        logger.info("Synced %s application command(s)", len(synced))

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        root_error = getattr(error, "original", error)
        message = "Something went wrong while running that command."

        if isinstance(error, app_commands.CheckFailure):
            message = "You do not have permission to use that command."
        elif isinstance(error, app_commands.CommandOnCooldown):
            message = f"That command is on cooldown. Try again in {error.retry_after:.1f}s."
        elif isinstance(root_error, aiohttp.ClientError):
            message = "Twitch could not be reached right now. Please try again in a moment."
            logger.warning("Twitch request failed during command execution", exc_info=root_error)
        else:
            logger.exception("Application command error", exc_info=error)

        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def is_manager(self, member: discord.Member) -> bool:
        if member.guild_permissions.manage_guild or member.id == member.guild.owner_id:
            return True

        config = await self.db.get_guild_config(member.guild.id)
        if config.manager_role_id is None:
            return False

        return any(role.id == config.manager_role_id for role in member.roles)

    async def get_notification_channel(self, channel_id: int) -> Optional[discord.TextChannel]:
        channel = self.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel

        try:
            fetched = await self.fetch_channel(channel_id)
        except discord.Forbidden:
            logger.warning("Missing permission to fetch channel %s", channel_id)
            return None
        except discord.NotFound:
            logger.warning("Configured channel %s no longer exists", channel_id)
            return None
        except discord.HTTPException:
            logger.exception("Failed to fetch configured channel %s", channel_id)
            return None

        return fetched if isinstance(fetched, discord.TextChannel) else None

    def build_live_embed(
        self,
        streamer_name: str,
        *,
        stream_title: str,
        game_name: str,
        profile_url: Optional[str],
        thumbnail_url: Optional[str] = None,
        started_at: Optional[str] = None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=stream_title,
            url=f"https://twitch.tv/{streamer_name}",
            color=TWITCH_COLOR,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
        embed.add_field(name="Streamer", value=streamer_name, inline=True)
        embed.add_field(name="Game", value=game_name or "Unknown", inline=True)
        embed.set_footer(text="Twitch Live Notification")

        if profile_url:
            embed.set_thumbnail(url=profile_url)

        if thumbnail_url:
            embed.set_image(
                url=thumbnail_url.replace("{width}", "1280").replace("{height}", "720")
            )

        if started_at:
            try:
                started = dt.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                embed.description = f"Started <t:{int(started.timestamp())}:R>"
            except ValueError:
                pass

        return embed

    def format_custom_message(
        self, template: Optional[str], *, streamer: str, game: str, title: str
    ) -> str:
        message = template or DEFAULT_MESSAGE
        return (
            message.replace("{streamer}", streamer)
            .replace("{game}", game or "Unknown")
            .replace("{title}", title or "Untitled stream")
        )

    async def send_stream_notification(
        self,
        guild_id: int,
        streamer_name: str,
        profile_url: Optional[str],
        stream_data: dict,
    ) -> bool:
        config = await self.db.get_guild_config(guild_id)
        if config.channel_id is None:
            return False

        channel = await self.get_notification_channel(config.channel_id)
        if channel is None:
            return False

        message = self.format_custom_message(
            config.custom_message,
            streamer=streamer_name,
            game=stream_data.get("game_name", "Unknown"),
            title=stream_data.get("title", "Untitled stream"),
        )
        role_ping = f"<@&{config.role_id}>\n" if config.role_id else ""
        embed = self.build_live_embed(
            streamer_name,
            stream_title=stream_data.get("title", "Untitled stream"),
            game_name=stream_data.get("game_name", "Unknown"),
            profile_url=profile_url,
            thumbnail_url=stream_data.get("thumbnail_url"),
            started_at=stream_data.get("started_at"),
        )

        try:
            await channel.send(f"{role_ping}{message}", embed=embed)
            return True
        except discord.Forbidden:
            logger.warning(
                "Missing permission to send messages in channel %s for guild %s",
                channel.id,
                guild_id,
            )
        except discord.NotFound:
            logger.warning("Notification channel %s disappeared before sending", channel.id)
        except discord.HTTPException:
            logger.exception(
                "Discord rejected a live notification for guild %s and streamer %s",
                guild_id,
                streamer_name,
            )

        return False

    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def check_streams(self) -> None:
        tracked_rows = await self.db.get_all_tracked_streamers()
        if not tracked_rows:
            return

        streamers_by_guild: dict[int, list[aiosqlite.Row]] = defaultdict(list)
        unique_usernames: set[str] = set()

        for row in tracked_rows:
            guild_id = int(row["guild_id"])
            streamers_by_guild[guild_id].append(row)
            unique_usernames.add(row["streamer_name"])

        try:
            live_map = await self.twitch.get_streams(sorted(unique_usernames))
        except aiohttp.ClientError:
            logger.exception("Failed to fetch Twitch live data")
            return

        status_updates: list[tuple[int, str, Optional[str]]] = []

        for guild_id, streamer_rows in streamers_by_guild.items():
            previous_statuses = await self.db.get_live_statuses(guild_id)

            for row in streamer_rows:
                streamer_name = row["streamer_name"]
                current_stream = live_map.get(streamer_name)
                current_stream_id = current_stream["id"] if current_stream else None
                previous_stream_id = previous_statuses.get(streamer_name)

                next_status = current_stream_id
                if current_stream and current_stream_id != previous_stream_id:
                    sent = await self.send_stream_notification(
                        guild_id,
                        streamer_name,
                        row["profile_url"],
                        current_stream,
                    )
                    if not sent:
                        next_status = previous_stream_id

                status_updates.append((guild_id, streamer_name, next_status))

        await self.db.set_live_statuses(status_updates)

    @check_streams.before_loop
    async def before_check_streams(self) -> None:
        await self.wait_until_ready()


bot = TwitchLiveBot()


def guild_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.guild is not None and isinstance(interaction.user, discord.Member)

    return app_commands.check(predicate)


def manager_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        return await bot.is_manager(interaction.user)

    return app_commands.check(predicate)


def admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        member = interaction.user
        return member.guild_permissions.manage_guild or member.id == member.guild.owner_id

    return app_commands.check(predicate)


@bot.tree.command(name="ping", description="Check if the bot is online.")
async def ping(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("Pong!")


@bot.tree.command(name="setup", description="Set the current channel as the live alert channel.")
@guild_only()
@manager_only()
async def setup(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    await bot.db.upsert_guild_config(interaction.guild.id, channel_id=interaction.channel_id)
    await interaction.response.send_message(
        f"Setup complete. Live notifications will be sent in {interaction.channel.mention}."
    )


@bot.tree.command(name="set_channel", description="Choose the channel used for live alerts.")
@guild_only()
@manager_only()
async def set_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    assert interaction.guild is not None
    await bot.db.upsert_guild_config(interaction.guild.id, channel_id=channel.id)
    await interaction.response.send_message(f"Live alert channel set to {channel.mention}.")


@bot.tree.command(name="set_role", description="Choose which role gets pinged for live alerts.")
@guild_only()
@manager_only()
async def set_role(interaction: discord.Interaction, role: discord.Role) -> None:
    assert interaction.guild is not None
    await bot.db.upsert_guild_config(interaction.guild.id, role_id=role.id)
    await interaction.response.send_message(f"{role.mention} will now be pinged for live alerts.")


@bot.tree.command(name="set_manager_role", description="Choose which role can manage the bot.")
@guild_only()
@admin_only()
async def set_manager_role(interaction: discord.Interaction, role: discord.Role) -> None:
    assert interaction.guild is not None
    await bot.db.upsert_guild_config(interaction.guild.id, manager_role_id=role.id)
    await interaction.response.send_message(f"{role.mention} can now manage this bot.")


@bot.tree.command(name="set_message", description="Set the custom live message.")
@guild_only()
@manager_only()
@app_commands.describe(
    message="You can use {streamer}, {game}, and {title} placeholders."
)
async def set_message(interaction: discord.Interaction, message: str) -> None:
    assert interaction.guild is not None
    await bot.db.upsert_guild_config(interaction.guild.id, custom_message=message)
    await interaction.response.send_message("Custom live message updated.")


@bot.tree.command(name="list_streamers", description="List all Twitch streamers tracked in this server.")
@guild_only()
async def list_streamers(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    rows = await bot.db.get_streamers(interaction.guild.id)
    if not rows:
        await interaction.response.send_message("No streamers have been added yet.")
        return

    streamer_list = "\n".join(f"- `{row['streamer_name']}`" for row in rows)
    await interaction.response.send_message(f"Tracked streamers:\n{streamer_list}")


@bot.tree.command(name="add_streamer", description="Add a Twitch streamer to track.")
@guild_only()
@manager_only()
async def add_streamer(interaction: discord.Interaction, streamer_name: str) -> None:
    assert interaction.guild is not None
    normalized_name = streamer_name.strip().lower()

    if not normalized_name:
        await interaction.response.send_message("Please provide a valid streamer name.", ephemeral=True)
        return

    if await bot.db.count_streamers(interaction.guild.id) >= STREAMER_LIMIT:
        await interaction.response.send_message(
            f"You have reached the limit of {STREAMER_LIMIT} streamers.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    profiles = await bot.twitch.get_profiles([normalized_name])
    profile = profiles.get(normalized_name)
    if profile is None:
        await interaction.followup.send("That Twitch streamer could not be found.", ephemeral=True)
        return

    added = await bot.db.add_streamer(
        interaction.guild.id,
        normalized_name,
        profile.get("profile_image_url", ""),
    )
    if not added:
        await interaction.followup.send("That streamer is already being tracked.", ephemeral=True)
        return

    await interaction.followup.send(f"Added `{normalized_name}` to the tracked streamer list.")


@bot.tree.command(name="remove_streamer", description="Remove a tracked Twitch streamer.")
@guild_only()
@manager_only()
async def remove_streamer(interaction: discord.Interaction, streamer_name: str) -> None:
    assert interaction.guild is not None
    normalized_name = streamer_name.strip().lower()
    deleted = await bot.db.remove_streamer(interaction.guild.id, normalized_name)

    if deleted:
        await interaction.response.send_message(f"Removed `{normalized_name}` from the tracked list.")
    else:
        await interaction.response.send_message(
            f"`{normalized_name}` was not in the tracked list.",
            ephemeral=True,
        )


@bot.tree.command(name="test", description="Send a test live notification using a random tracked streamer.")
@guild_only()
@manager_only()
async def test(interaction: discord.Interaction) -> None:
    assert interaction.guild is not None
    config = await bot.db.get_guild_config(interaction.guild.id)

    if config.channel_id is None:
        await interaction.response.send_message(
            "Set a live alert channel first with `/setup` or `/set_channel`.",
            ephemeral=True,
        )
        return

    rows = await bot.db.get_streamers(interaction.guild.id)
    if not rows:
        await interaction.response.send_message(
            "Add at least one streamer first with `/add_streamer`.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    selected = random.choice(rows)
    streamer_name = selected["streamer_name"]
    live_data = await bot.twitch.get_streams([streamer_name])
    stream = live_data.get(streamer_name)

    mock_stream = {
        "title": "Test Stream Title" if stream is None else stream.get("title", "Test Stream Title"),
        "game_name": "Just Chatting" if stream is None else stream.get("game_name", "Just Chatting"),
        "thumbnail_url": None if stream is None else stream.get("thumbnail_url"),
        "started_at": None if stream is None else stream.get("started_at"),
    }

    sent = await bot.send_stream_notification(
        interaction.guild.id,
        streamer_name,
        selected["profile_url"],
        mock_stream,
    )
    if sent:
        await interaction.followup.send("Test notification sent.")
    else:
        await interaction.followup.send(
            "The test notification could not be sent. Check the configured channel and bot permissions.",
            ephemeral=True,
        )


async def main() -> None:
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
