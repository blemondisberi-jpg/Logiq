"""
Music Cog for Logiq
Music player with YouTube support
"""

import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional
import logging
import asyncio
import functools
import shutil
import os
import uuid
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.utils import DownloadError

from utils.embeds import EmbedFactory, EmbedColor
from utils.permissions import is_admin
from database.db_manager import DatabaseManager

logger = logging.getLogger(__name__)

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "extract_flat": False,
    "ignoreerrors": True,
    "geo_bypass": True,
    "source_address": "0.0.0.0"
}
DEFAULT_YOUTUBE_PLAYER_CLIENTS = ("tv", "web_safari", "android")
FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"
VOICE_CONNECT_COOLDOWN_SECONDS = 45


class MusicQueue:
    """Music queue manager"""
    
    def __init__(self):
        self.queue = []
        self.current = None
        self.loop = False
        
    def add(self, track):
        """Add track to queue"""
        self.queue.append(track)
        
    def next(self):
        """Get next track"""
        if self.loop and self.current:
            return self.current
        if self.queue:
            self.current = self.queue.pop(0)
            return self.current
        return None
        
    def clear(self):
        """Clear queue"""
        self.queue = []
        self.current = None

class MusicControlView(discord.ui.View):
    """Music player controls"""
    
    def __init__(self, cog: 'Music'):
        super().__init__(timeout=None)
        self.cog = cog
        
    @discord.ui.button(label="⏸️ Pause", style=discord.ButtonStyle.primary, custom_id="music_pause")
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Pause/Resume music"""
        if not interaction.guild.voice_client:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )
            return
            
        vc = interaction.guild.voice_client
        if vc.is_playing():
            vc.pause()
            button.label = "▶️ Resume"
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(
                embed=EmbedFactory.info("Paused", "Music paused"),
                ephemeral=True
            )
        elif vc.is_paused():
            vc.resume()
            button.label = "⏸️ Pause"
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(
                embed=EmbedFactory.info("Resumed", "Music resumed"),
                ephemeral=True
            )
            
    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary, custom_id="music_skip")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Skip current track"""
        if not interaction.guild.voice_client:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )
            return
            
        vc = interaction.guild.voice_client
        if vc.is_playing() or vc.is_paused():
            vc.stop()
            await interaction.response.send_message(
                embed=EmbedFactory.success("Skipped", "Skipped current track"),
                ephemeral=True
            )
            
    @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.danger, custom_id="music_stop")
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Stop music and disconnect"""
        if not interaction.guild.voice_client:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )
            return
            
        guild_id = interaction.guild.id
        if guild_id in self.cog.queues:
            self.cog.queues[guild_id].clear()
            
        vc = interaction.guild.voice_client
        await vc.disconnect()
        await interaction.response.send_message(
            embed=EmbedFactory.success("Stopped", "Music stopped and disconnected"),
            ephemeral=True
        )


class Music(commands.Cog):
    """Music player cog"""

    def __init__(self, bot: commands.Bot, db: DatabaseManager, config: dict):
        self.bot = bot
        self.db = db
        self.config = config
        self.module_config = config.get('modules', {}).get('music', {})
        self.queues = {}  # guild_id: MusicQueue
        self.volumes = {}  # guild_id: float
        self.playback_locks = {}  # guild_id: asyncio.Lock
        self.voice_connect_cooldowns = {}  # guild_id: unix timestamp

    def _is_enabled(self) -> bool:
        """Whether music commands should be active."""
        return self.module_config.get('enabled', True)

    async def _guard_enabled(self, interaction: discord.Interaction) -> bool:
        """Send a clear response when music is disabled."""
        if self._is_enabled():
            return True

        await interaction.response.send_message(
            embed=EmbedFactory.error(
                "Music Disabled",
                "The music module is currently disabled in the bot configuration."
            ),
            ephemeral=True
        )
        return False

    def _ffmpeg_available(self) -> bool:
        """Check whether FFmpeg is available on the host."""
        return shutil.which("ffmpeg") is not None

    def _get_volume(self, guild_id: int) -> float:
        """Return saved guild volume as a 0.0-1.0 value."""
        return self.volumes.get(guild_id, 0.5)

    def _voice_connect_retry_after(self, guild_id: int) -> int:
        """Return seconds remaining before another voice connect attempt is allowed."""
        retry_at = self.voice_connect_cooldowns.get(guild_id, 0)
        remaining = int(retry_at - discord.utils.utcnow().timestamp())
        return max(0, remaining)

    def _set_voice_connect_cooldown(self, guild_id: int) -> None:
        """Briefly throttle voice attempts after Discord rejects a voice session."""
        retry_at = discord.utils.utcnow().timestamp() + VOICE_CONNECT_COOLDOWN_SECONDS
        self.voice_connect_cooldowns[guild_id] = retry_at

    async def _cleanup_voice_client(self, guild: discord.Guild) -> None:
        """Clear stale voice state after a failed Discord voice handshake."""
        voice_client = guild.voice_client
        if voice_client is None:
            return

        try:
            await voice_client.disconnect(force=True)
        except Exception as error:
            logger.warning("Failed to disconnect stale music voice client in guild %s: %s", guild.id, error)

        cleanup = getattr(voice_client, "cleanup", None)
        if callable(cleanup):
            try:
                cleanup()
            except Exception as error:
                logger.warning("Failed to cleanup stale music voice client in guild %s: %s", guild.id, error)

    def _get_youtube_player_clients(self) -> list[str]:
        """Resolve the YouTube clients yt-dlp should try for public playback."""
        configured = (
            os.getenv("YOUTUBE_PLAYER_CLIENTS")
            or self.module_config.get("youtube_player_clients")
            or ",".join(DEFAULT_YOUTUBE_PLAYER_CLIENTS)
        )
        clients = [client.strip() for client in str(configured).split(",") if client.strip()]
        return clients or list(DEFAULT_YOUTUBE_PLAYER_CLIENTS)

    def _get_youtube_cookiefile(self) -> Optional[str]:
        """Optional cookie file path for hosts that YouTube has challenged."""
        cookiefile = os.getenv("YOUTUBE_COOKIES_FILE") or self.module_config.get("youtube_cookies_file")
        if not cookiefile:
            return None
        return str(cookiefile)

    def _build_ytdl(self, player_clients: list[str]) -> yt_dlp.YoutubeDL:
        """Create a fresh yt-dlp instance for one extraction attempt."""
        options = dict(YTDL_OPTIONS)
        options["extractor_args"] = {"youtube": {"player_client": player_clients}}
        cookiefile = self._get_youtube_cookiefile()
        if cookiefile:
            options["cookiefile"] = cookiefile
        return yt_dlp.YoutubeDL(options)

    def _looks_like_url(self, value: str) -> bool:
        """Detect direct URLs so search terms can use broader fallback search."""
        parsed = urlparse(value.strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    def get_queue(self, guild_id: int) -> MusicQueue:
        """Get or create queue for guild"""
        if guild_id not in self.queues:
            self.queues[guild_id] = MusicQueue()
        return self.queues[guild_id]

    def get_playback_lock(self, guild_id: int) -> asyncio.Lock:
        """Get or create a per-guild playback lock."""
        if guild_id not in self.playback_locks:
            self.playback_locks[guild_id] = asyncio.Lock()
        return self.playback_locks[guild_id]

    async def _extract_track(self, query: str, requester: discord.Member) -> dict:
        """Resolve a search query or URL into a playable audio stream."""
        data = await self._extract_info_with_fallbacks(query)

        if "entries" in data:
            entries = [entry for entry in data.get("entries", []) if entry]
            if not entries:
                raise ValueError("No playable results were found for that search.")
            data = next((entry for entry in entries if entry.get("url")), entries[0])

        stream_url = data.get("url")
        if not stream_url:
            raise ValueError("I couldn't resolve that track into a playable audio stream.")

        return {
            "title": data.get("title") or query,
            "stream_url": stream_url,
            "webpage_url": data.get("webpage_url") or data.get("original_url") or query,
            "duration": data.get("duration"),
            "thumbnail": data.get("thumbnail"),
            "requester_id": requester.id,
            "requester_name": requester.display_name
        }

    async def _extract_info_with_fallbacks(self, query: str) -> dict:
        """Try YouTube extraction with public clients and search fallback."""
        search_terms = [query] if self._looks_like_url(query) else [f"ytsearch5:{query}"]
        player_clients = self._get_youtube_player_clients()
        client_attempts = [
            player_clients,
            ["tv", "web_safari"],
            ["android"],
            ["web"]
        ]
        seen_attempts = set()
        last_error = None

        for clients in client_attempts:
            attempt_key = tuple(clients)
            if attempt_key in seen_attempts:
                continue
            seen_attempts.add(attempt_key)

            ytdl = self._build_ytdl(clients)
            for term in search_terms:
                try:
                    extractor = functools.partial(ytdl.extract_info, term, download=False)
                    loop = asyncio.get_running_loop()
                    data = await loop.run_in_executor(None, extractor)
                    if data:
                        return data
                except DownloadError as error:
                    last_error = str(error)
                    logger.warning("yt-dlp failed for %s with YouTube clients %s: %s", term, ",".join(clients), error)
                except Exception as error:
                    last_error = str(error)
                    logger.warning("Music extraction failed for %s with YouTube clients %s: %s", term, ",".join(clients), error)

        if last_error and "Sign in to confirm" in last_error:
            raise ValueError(
                "YouTube blocked this public extraction with its anti-bot check. "
                "Try searching by song title instead of using the direct link; if it keeps happening on Railway, "
                "we'll need either a different audio source or a `YOUTUBE_COOKIES_FILE` configured."
            )

        raise ValueError(last_error or "No playable results were found for that search.")

    async def _ensure_voice_client(
        self,
        interaction: discord.Interaction,
        attempt_id: Optional[str] = None
    ) -> Optional[discord.VoiceClient]:
        """Join or move to the requester's voice channel."""
        if interaction.guild is None:
            sender = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
            await sender(
                embed=EmbedFactory.error("Server Only", "Music commands can only be used inside a server."),
                ephemeral=True
            )
            return None

        voice_state = getattr(interaction.user, "voice", None)
        if not voice_state or not voice_state.channel:
            sender = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
            await sender(
                embed=EmbedFactory.error("Not in Voice", "You must be in a voice channel to use this command"),
                ephemeral=True
            )
            return None

        voice_client = interaction.guild.voice_client
        if voice_client and not voice_client.is_connected():
            logger.warning(
                "Music attempt %s: cleaning up stale voice client in guild %s before reconnecting",
                attempt_id,
                interaction.guild.id
            )
            await self._cleanup_voice_client(interaction.guild)
            voice_client = interaction.guild.voice_client

        if voice_client and voice_client.channel != voice_state.channel:
            logger.info(
                "Music attempt %s: moving voice client in guild %s from channel %s to %s",
                attempt_id,
                interaction.guild.id,
                getattr(voice_client.channel, "id", None),
                voice_state.channel.id
            )
            await voice_client.move_to(voice_state.channel)
            return voice_client

        if voice_client:
            logger.info(
                "Music attempt %s: reusing existing voice client in guild %s channel %s",
                attempt_id,
                interaction.guild.id,
                getattr(voice_client.channel, "id", None)
            )
            return voice_client

        retry_after = self._voice_connect_retry_after(interaction.guild.id)
        if retry_after:
            sender = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
            await sender(
                embed=EmbedFactory.error(
                    "Voice Connection Cooling Down",
                    (
                        "Discord rejected the last voice connection attempt. "
                        f"Please wait about {retry_after}s before trying again."
                    )
                ),
                ephemeral=True
            )
            return None

        try:
            logger.info(
                "Music attempt %s: connecting voice in guild %s to channel %s",
                attempt_id,
                interaction.guild.id,
                voice_state.channel.id
            )
            voice_client = await voice_state.channel.connect(reconnect=False, timeout=30.0)
            logger.info(
                "Music attempt %s: voice connected in guild %s to channel %s",
                attempt_id,
                interaction.guild.id,
                voice_state.channel.id
            )
            return voice_client
        except Exception as error:
            await self._cleanup_voice_client(interaction.guild)
            if isinstance(error, discord.ConnectionClosed) and getattr(error, "code", None) == 4006:
                self._set_voice_connect_cooldown(interaction.guild.id)

            logger.error(
                "Music attempt %s: voice connection failed in guild %s to channel %s: %s",
                attempt_id,
                interaction.guild.id,
                voice_state.channel.id,
                error,
                exc_info=True
            )
            error_message = (
                "Discord rejected the voice session handshake. I cleaned up the stale voice state; "
                "please try again in about 45 seconds."
                if isinstance(error, discord.ConnectionClosed) and getattr(error, "code", None) == 4006
                else f"Could not join voice channel: {error}"
            )
            sender = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
            await sender(
                embed=EmbedFactory.error("Connection Failed", error_message),
                ephemeral=True
            )
            return None

    def _format_duration(self, seconds: Optional[int]) -> str:
        """Format a track duration for embeds."""
        if not seconds:
            return "Live or unknown length"
        minutes, secs = divmod(int(seconds), 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    def _track_label(self, track: dict) -> str:
        """Format a track title with a link when available."""
        title = discord.utils.escape_markdown(str(track.get("title") or "Unknown track"))
        url = track.get("webpage_url")
        return f"[{title}]({url})" if url else title

    async def _play_next(self, guild_id: int, attempt_id: Optional[str] = None) -> tuple[bool, Optional[str]]:
        """Start the next queued track and report whether playback actually began."""
        async with self.get_playback_lock(guild_id):
            guild = self.bot.get_guild(guild_id)
            if guild is None or guild.voice_client is None:
                return False, "I am not connected to a voice channel."

            voice_client = guild.voice_client
            if not voice_client.is_connected():
                logger.warning("Music playback requested in guild %s but voice client is disconnected", guild_id)
                return False, "The voice connection dropped before playback could start."

            if voice_client.is_playing() or voice_client.is_paused():
                return True, None

            queue = self.get_queue(guild_id)
            track = queue.next()
            if not track:
                queue.current = None
                logger.info("Music queue empty in guild %s", guild_id)
                return False, "The music queue is empty."
            attempt_id = attempt_id or track.get("attempt_id")

            try:
                source = discord.FFmpegPCMAudio(
                    track["stream_url"],
                    before_options=FFMPEG_BEFORE_OPTIONS,
                    options=FFMPEG_OPTIONS
                )
                audio = discord.PCMVolumeTransformer(source, volume=self._get_volume(guild_id))
            except Exception as error:
                queue.current = None
                logger.error(
                    "Music attempt %s: failed to create FFmpeg audio source in guild %s for %s: %s",
                    attempt_id,
                    guild_id,
                    track.get("webpage_url") or track.get("title"),
                    error,
                    exc_info=True
                )
                return False, "I could not create the audio stream for that track."

            def after_play(error: Optional[Exception]) -> None:
                logger.info(
                    "Music attempt %s: after_play callback fired in guild %s with error=%s",
                    attempt_id,
                    guild_id,
                    error
                )
                future = asyncio.run_coroutine_threadsafe(
                    self._handle_track_finished(guild_id, track, error),
                    self.bot.loop
                )
                future.add_done_callback(
                    lambda completed: logger.error(
                        "Failed to finish music queue step in guild %s: %s",
                        guild_id,
                        completed.exception(),
                        exc_info=completed.exception()
                    ) if completed.exception() else None
                )

            try:
                voice_client.play(audio, after=after_play)
                logger.info(
                    "Music attempt %s: playback started in guild %s: %s (%s)",
                    attempt_id,
                    guild_id,
                    track.get("title"),
                    track.get("webpage_url")
                )
                return True, None
            except Exception as error:
                queue.current = None
                logger.error(
                    "Music attempt %s: failed to start playback in guild %s for %s: %s",
                    attempt_id,
                    guild_id,
                    track.get("webpage_url") or track.get("title"),
                    error,
                    exc_info=True
                )
                return False, "Discord accepted the voice connection, but playback could not be started."

    async def _handle_track_finished(
        self,
        guild_id: int,
        track: dict,
        error: Optional[Exception]
    ) -> None:
        """Handle FFmpeg completion and advance the queue once."""
        queue = self.get_queue(guild_id)
        if error:
            logger.error(
                "Music playback failed in guild %s for %s: %s",
                guild_id,
                track.get("webpage_url") or track.get("title"),
                error,
                exc_info=(type(error), error, error.__traceback__)
            )
        else:
            logger.info("Music playback finished in guild %s: %s", guild_id, track.get("title"))

        queue.current = None
        await self._play_next(guild_id)

    async def _start_playback_if_idle(
        self,
        guild_id: int,
        attempt_id: Optional[str] = None
    ) -> tuple[bool, Optional[str]]:
        """Kick the queue if the voice client is idle and report the result."""
        guild = self.bot.get_guild(guild_id)
        if not guild or not guild.voice_client:
            return False, "I am not connected to a voice channel."

        voice_client = guild.voice_client
        if not voice_client.is_playing() and not voice_client.is_paused():
            return await self._play_next(guild_id, attempt_id=attempt_id)

        return True, None

    @app_commands.command(name="play", description="Play music from YouTube")
    @app_commands.describe(query="Song name or YouTube URL")
    async def play(self, interaction: discord.Interaction, query: str):
        """Play music from YouTube"""
        attempt_id = uuid.uuid4().hex[:8]
        if not await self._guard_enabled(interaction):
            return

        if interaction.guild is None:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Server Only", "Music commands can only be used inside a server."),
                ephemeral=True
            )
            return

        voice_state = getattr(interaction.user, "voice", None)
        if not voice_state or not voice_state.channel:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not in Voice", "You must be in a voice channel to use this command"),
                ephemeral=True
            )
            return

        logger.info(
            "Music attempt %s: /play requested in guild %s by user %s in channel %s with query %r",
            attempt_id,
            interaction.guild.id,
            interaction.user.id,
            voice_state.channel.id,
            query
        )

        if not self._ffmpeg_available():
            logger.error("Music attempt %s: FFmpeg missing on host", attempt_id)
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "FFmpeg Missing",
                    "Music playback needs FFmpeg installed on the host. Railway will install it from `nixpacks.toml` after the next deploy."
                ),
                ephemeral=True
            )
            return

        await interaction.response.defer()

        try:
            track = await self._extract_track(query, interaction.user)
        except Exception as error:
            logger.error("Music attempt %s: failed to resolve music query %s: %s", attempt_id, query, error, exc_info=True)
            await interaction.followup.send(
                embed=EmbedFactory.error("Track Not Found", str(error)),
                ephemeral=True
            )
            return
        logger.info(
            "Music attempt %s: track resolved in guild %s by %s: %s (%s)",
            attempt_id,
            interaction.guild.id,
            interaction.user,
            track.get("title"),
            track.get("webpage_url")
        )
        track["attempt_id"] = attempt_id

        voice_client = await self._ensure_voice_client(interaction, attempt_id=attempt_id)
        if voice_client is None:
            return

        # Add to queue
        queue = self.get_queue(interaction.guild.id)
        was_idle = not voice_client.is_playing() and not voice_client.is_paused() and queue.current is None
        logger.info(
            "Music attempt %s: queueing track in guild %s; was_idle=%s queue_len_before=%s",
            attempt_id,
            interaction.guild.id,
            was_idle,
            len(queue.queue)
        )
        queue.add(track)

        if was_idle:
            started, playback_error = await self._start_playback_if_idle(interaction.guild.id, attempt_id=attempt_id)
            if not started:
                queue.clear()
                logger.warning(
                    "Music attempt %s: track resolved but playback did not start in guild %s for %s: %s",
                    attempt_id,
                    interaction.guild.id,
                    track.get("webpage_url") or track.get("title"),
                    playback_error
                )
                await interaction.followup.send(
                    embed=EmbedFactory.error(
                        "Playback Failed",
                        playback_error or "I could not start playback for that track."
                    ),
                    ephemeral=True
                )
                return

        embed = EmbedFactory.success(
            "Now Playing" if was_idle else "Added to Queue",
            f"**Track:** {self._track_label(track)}\n"
            f"**Duration:** {self._format_duration(track.get('duration'))}\n"
            f"**Requested by:** {interaction.user.mention}\n"
            f"**Position in queue:** {1 if was_idle else len(queue.queue)}"
        )
        if track.get("thumbnail"):
            embed.set_thumbnail(url=track["thumbnail"])

        await interaction.followup.send(embed=embed, view=MusicControlView(self))
        if not was_idle:
            await self._start_playback_if_idle(interaction.guild.id, attempt_id=attempt_id)
        logger.info("Music attempt %s: user-facing response sent for %s: %s", attempt_id, interaction.user, track["title"])

    @app_commands.command(name="join", description="Join your voice channel")
    async def join(self, interaction: discord.Interaction):
        """Join voice channel"""
        if not await self._guard_enabled(interaction):
            return

        voice_client = await self._ensure_voice_client(interaction)
        if voice_client is None:
            return

        embed = EmbedFactory.success("Joined", f"Joined {voice_client.channel.mention}")
        sender = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await sender(embed=embed)

    @app_commands.command(name="leave", description="Leave voice channel")
    async def leave(self, interaction: discord.Interaction):
        """Leave voice channel"""
        if not await self._guard_enabled(interaction):
            return

        if not interaction.guild.voice_client:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Connected", "I'm not in a voice channel"),
                ephemeral=True
            )
            return

        guild_id = interaction.guild.id
        if guild_id in self.queues:
            self.queues[guild_id].clear()

        await interaction.guild.voice_client.disconnect()
        embed = EmbedFactory.success("Disconnected", "Left voice channel")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="queue", description="View music queue")
    async def view_queue(self, interaction: discord.Interaction):
        """View music queue"""
        if not await self._guard_enabled(interaction):
            return

        guild_id = interaction.guild.id
        queue = self.get_queue(guild_id)

        if not queue.current and not queue.queue:
            await interaction.response.send_message(
                embed=EmbedFactory.info("Empty Queue", "The music queue is empty"),
                ephemeral=True
            )
            return

        description = ""
        if queue.current:
            description += f"**Now Playing:**\n{self._track_label(queue.current)}\n\n"

        if queue.queue:
            description += "**Up Next:**\n"
            for i, track in enumerate(queue.queue[:10], 1):
                description += f"{i}. {self._track_label(track)}\n"

        embed = EmbedFactory.create(
            title="🎵 Music Queue",
            description=description,
            color=EmbedColor.INFO
        )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="skip", description="Skip current track")
    async def skip(self, interaction: discord.Interaction):
        """Skip current track"""
        if not await self._guard_enabled(interaction):
            return

        if not interaction.guild.voice_client:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )
            return

        vc = interaction.guild.voice_client
        if vc.is_playing() or vc.is_paused():
            vc.stop()
            embed = EmbedFactory.success("Skipped", "Skipped current track")
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )

    @app_commands.command(name="pause", description="Pause music")
    async def pause(self, interaction: discord.Interaction):
        """Pause music"""
        if not await self._guard_enabled(interaction):
            return

        if not interaction.guild.voice_client:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )
            return

        vc = interaction.guild.voice_client
        if vc.is_playing():
            vc.pause()
            embed = EmbedFactory.success("Paused", "Music paused")
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )

    @app_commands.command(name="resume", description="Resume music")
    async def resume(self, interaction: discord.Interaction):
        """Resume music"""
        if not await self._guard_enabled(interaction):
            return

        if not interaction.guild.voice_client:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is paused"),
                ephemeral=True
            )
            return

        vc = interaction.guild.voice_client
        if vc.is_paused():
            vc.resume()
            embed = EmbedFactory.success("Resumed", "Music resumed")
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Paused", "Music is not paused"),
                ephemeral=True
            )

    @app_commands.command(name="volume", description="Set volume (Admin)")
    @app_commands.describe(volume="Volume level (0-100)")
    @is_admin()
    async def volume(self, interaction: discord.Interaction, volume: int):
        """Set volume"""
        if not await self._guard_enabled(interaction):
            return

        if volume < 0 or volume > 100:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Volume", "Volume must be between 0 and 100"),
                ephemeral=True
            )
            return

        if not interaction.guild.voice_client:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Playing", "No music is playing"),
                ephemeral=True
            )
            return

        guild_id = interaction.guild.id
        self.volumes[guild_id] = volume / 100

        source = getattr(interaction.guild.voice_client, "source", None)
        if isinstance(source, discord.PCMVolumeTransformer):
            source.volume = self._get_volume(guild_id)

        embed = EmbedFactory.success("Volume", f"Volume set to {volume}%")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="nowplaying", description="Show currently playing track")
    async def nowplaying(self, interaction: discord.Interaction):
        """Show currently playing track"""
        if not await self._guard_enabled(interaction):
            return

        guild_id = interaction.guild.id
        queue = self.get_queue(guild_id)

        if not queue.current:
            await interaction.response.send_message(
                embed=EmbedFactory.info("Nothing Playing", "No music is currently playing"),
                ephemeral=True
            )
            return

        embed = EmbedFactory.create(
            title="🎵 Now Playing",
            description=(
                f"**Track:** {self._track_label(queue.current)}\n"
                f"**Duration:** {self._format_duration(queue.current.get('duration'))}\n"
                f"**Requested by:** {queue.current.get('requester_name', 'Unknown')}"
            ),
            color=EmbedColor.INFO
        )
        if queue.current.get("thumbnail"):
            embed.set_thumbnail(url=queue.current["thumbnail"])

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    """Setup function for cog loading"""
    await bot.add_cog(Music(bot, bot.db, bot.config))
