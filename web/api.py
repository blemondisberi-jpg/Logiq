"""
FastAPI Web Dashboard for Logiq
REST API endpoints for bot statistics and management
"""

from datetime import datetime, timedelta, timezone
import json
import re

from fastapi import FastAPI, HTTPException, Depends, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from typing import Optional, Dict, Any, List
import logging
import os

logger = logging.getLogger(__name__)
TWITCH_EVENTSUB_PATH = "/webhooks/twitch/eventsub"
KICK_EVENTS_PATH = "/webhooks/kick/events"
YOUTUBE_OAUTH_CALLBACK_PATH = "/oauth/youtube/callback"
TIKTOK_OAUTH_CALLBACK_PATH = "/oauth/tiktok/callback"


def parse_rfc3339_timestamp(timestamp: str) -> datetime | None:
    """Parse RFC3339 timestamps, including nanosecond precision."""
    if not timestamp:
        return None

    match = re.match(r"^(?P<prefix>.+\.\d{1,})(?P<suffix>Z|[+-]\d{2}:\d{2})$", timestamp)
    if match:
        prefix = match.group("prefix")
        suffix = match.group("suffix")
        head, frac = prefix.split(".", 1)
        timestamp = f"{head}.{frac[:6]}{suffix}"

    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def create_app(bot) -> FastAPI:
    """Create FastAPI application"""

    app = FastAPI(
        title="Logiq API",
        description="REST API for Logiq Discord Bot",
        version="1.0.0"
    )

    # CORS middleware
    cors_origins = bot.config.get('web', {}).get('cors_origins', ['http://localhost:3000'])
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/", response_class=HTMLResponse)
    async def root():
        """Admin Dashboard Homepage"""
        html_file = os.path.join(os.path.dirname(__file__), "templates", "index.html")
        if os.path.exists(html_file):
            with open(html_file, "r", encoding="utf-8") as f:
                return f.read()
        return """
        <html>
            <head><title>Logiq Admin Dashboard</title></head>
            <body>
                <h1>Logiq API</h1>
                <p>Version: 1.0.0</p>
                <p>Status: Online</p>
                <p>Bot User: {}</p>
                <p><a href="/admin">Go to Admin Dashboard</a></p>
            </body>
        </html>
        """.format(str(bot.user) if bot.user else "Loading...")

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_dashboard():
        """Admin Dashboard"""
        html_file = os.path.join(os.path.dirname(__file__), "templates", "admin.html")
        if os.path.exists(html_file):
            with open(html_file, "r", encoding="utf-8") as f:
                return f.read()
        # Return fallback if template doesn't exist
        return "<h1>Admin Dashboard - Template not found</h1>"

    @app.get("/stats")
    async def get_stats():
        """Get bot statistics"""
        return {
            "guilds": len(bot.guilds),
            "users": sum(g.member_count for g in bot.guilds),
            "channels": sum(len(g.channels) for g in bot.guilds),
            "uptime": str(datetime.utcnow() - bot.start_time).split('.')[0] if hasattr(bot, 'start_time') else "Unknown",
            "latency": round(bot.latency * 1000)
        }

    @app.get("/guilds")
    async def get_guilds():
        """Get list of guilds"""
        return {
            "guilds": [
                {
                    "id": guild.id,
                    "name": guild.name,
                    "member_count": guild.member_count,
                    "icon_url": str(guild.icon.url) if guild.icon else None
                }
                for guild in bot.guilds
            ]
        }

    @app.get("/guilds/{guild_id}")
    async def get_guild(guild_id: int):
        """Get guild details"""
        guild = bot.get_guild(guild_id)
        if not guild:
            raise HTTPException(status_code=404, detail="Guild not found")

        guild_config = await bot.db.get_guild(guild_id)

        return {
            "id": guild.id,
            "name": guild.name,
            "member_count": guild.member_count,
            "icon_url": str(guild.icon.url) if guild.icon else None,
            "owner_id": guild.owner_id,
            "created_at": guild.created_at.isoformat(),
            "config": guild_config
        }

    @app.get("/guilds/{guild_id}/leaderboard")
    async def get_guild_leaderboard(guild_id: int, limit: int = 10):
        """Get guild XP leaderboard"""
        guild = bot.get_guild(guild_id)
        if not guild:
            raise HTTPException(status_code=404, detail="Guild not found")

        leaderboard = await bot.db.get_leaderboard(guild_id, limit=limit)

        return {
            "guild_id": guild_id,
            "leaderboard": [
                {
                    "rank": i + 1,
                    "user_id": entry['user_id'],
                    "xp": entry.get('xp', 0),
                    "level": entry.get('level', 0)
                }
                for i, entry in enumerate(leaderboard)
            ]
        }

    @app.get("/guilds/{guild_id}/analytics")
    async def get_guild_analytics(guild_id: int, days: int = 7):
        """Get guild analytics"""
        guild = bot.get_guild(guild_id)
        if not guild:
            raise HTTPException(status_code=404, detail="Guild not found")

        end_time = datetime.utcnow().timestamp()
        start_time = (datetime.utcnow() - timedelta(days=days)).timestamp()

        # Get analytics data
        messages = await bot.db.get_analytics(
            guild_id,
            event_type='message',
            start_time=start_time,
            end_time=end_time
        )

        joins = await bot.db.get_analytics(
            guild_id,
            event_type='member_join',
            start_time=start_time,
            end_time=end_time
        )

        leaves = await bot.db.get_analytics(
            guild_id,
            event_type='member_leave',
            start_time=start_time,
            end_time=end_time
        )

        return {
            "guild_id": guild_id,
            "period_days": days,
            "total_messages": len(messages),
            "member_joins": len(joins),
            "member_leaves": len(leaves),
            "net_growth": len(joins) - len(leaves)
        }

    @app.get("/health")
    async def health_check():
        """Health check endpoint"""
        db_connected = bot.db.is_connected if hasattr(bot.db, 'is_connected') else False

        return {
            "status": "healthy" if bot.is_ready() and db_connected else "unhealthy",
            "bot_ready": bot.is_ready(),
            "database_connected": db_connected,
            "timestamp": datetime.utcnow().isoformat()
        }

    @app.get("/modules")
    async def get_modules():
        """Get module status"""
        modules = bot.config.get('modules', {})
        return {
            "modules": {
                name: config.get('enabled', True)
                for name, config in modules.items()
            }
        }

    @app.post(TWITCH_EVENTSUB_PATH)
    async def twitch_eventsub_webhook(request: Request):
        """Receive Twitch EventSub webhook callbacks."""
        try:
            social_alerts = bot.get_cog("SocialAlerts")
            if social_alerts is None:
                raise HTTPException(status_code=503, detail="Social alerts cog is not loaded")

            body = await request.body()
            message_id = request.headers.get("Twitch-Eventsub-Message-Id", "")
            timestamp = request.headers.get("Twitch-Eventsub-Message-Timestamp", "")
            signature = request.headers.get("Twitch-Eventsub-Message-Signature", "")
            message_type = request.headers.get("Twitch-Eventsub-Message-Type", "")
            logger.info(
                "Incoming Twitch EventSub request: type=%s id=%s body_bytes=%s",
                message_type or "missing",
                message_id or "missing",
                len(body),
            )

            if not message_id or not timestamp or not signature or not message_type:
                logger.warning("Rejected Twitch EventSub request due to missing headers")
                raise HTTPException(status_code=400, detail="Missing Twitch EventSub headers")

            sent_at = parse_rfc3339_timestamp(timestamp)
            if sent_at is None:
                logger.warning("Rejected Twitch EventSub request due to invalid timestamp: %s", timestamp)
                raise HTTPException(status_code=400, detail="Invalid Twitch EventSub timestamp")

            # Keep replay protection, but don't let strict freshness checks break the one-time webhook verification.
            if message_type != "webhook_callback_verification":
                if abs((datetime.now(timezone.utc) - sent_at).total_seconds()) > 600:
                    logger.warning("Rejected Twitch EventSub request because timestamp was too old: %s", timestamp)
                    raise HTTPException(status_code=400, detail="Twitch EventSub message is too old")

            if not social_alerts.verify_eventsub_signature(body, message_id, timestamp, signature):
                logger.warning("Rejected Twitch EventSub request because signature verification failed")
                raise HTTPException(status_code=403, detail="Invalid Twitch EventSub signature")

            if message_type != "webhook_callback_verification" and message_id in social_alerts._recent_eventsub_messages:
                logger.info("Ignoring duplicate Twitch EventSub request id=%s", message_id)
                return Response(status_code=200)

            if message_type != "webhook_callback_verification":
                social_alerts._recent_eventsub_messages[message_id] = datetime.now(timezone.utc)
                cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
                social_alerts._recent_eventsub_messages = {
                    key: value
                    for key, value in list(social_alerts._recent_eventsub_messages.items())
                    if value >= cutoff
                }

            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError as error:
                logger.warning("Rejected Twitch EventSub request due to invalid JSON: %s", error)
                raise HTTPException(status_code=400, detail="Invalid Twitch EventSub payload")

            if message_type == "webhook_callback_verification":
                if not isinstance(payload, dict):
                    logger.warning("Rejected Twitch EventSub verification because payload was not an object")
                    raise HTTPException(status_code=400, detail="Invalid Twitch EventSub verification payload")

                challenge = payload.get("challenge")
                subscription = payload.get("subscription") or {}
                broadcaster_id = None
                subscription_type = None
                if isinstance(subscription, dict):
                    subscription_type = subscription.get("type")
                    condition = subscription.get("condition") or {}
                    if isinstance(condition, dict):
                        broadcaster_id = condition.get("broadcaster_user_id")

                if not isinstance(challenge, str) or not challenge:
                    logger.warning("Rejected Twitch EventSub verification because challenge was missing")
                    raise HTTPException(status_code=400, detail="Missing Twitch EventSub challenge")

                logger.info(
                    "Responding to Twitch EventSub challenge request for %s (%s)",
                    subscription_type or "unknown",
                    broadcaster_id or "unknown",
                )
                return PlainTextResponse(content=challenge, media_type="text/plain")

            challenge = await social_alerts.handle_twitch_eventsub_request(message_type, payload)
            if challenge is not None:
                logger.info("Responding to Twitch EventSub challenge request")
                return PlainTextResponse(content=challenge)

            return Response(status_code=204)
        except HTTPException:
            raise
        except Exception as error:
            logger.exception("Unhandled error while processing Twitch EventSub webhook: %s", error)
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal Twitch EventSub webhook error"}
            )

    @app.post(KICK_EVENTS_PATH)
    async def kick_events_webhook(request: Request):
        """Receive Kick webhook callbacks."""
        try:
            social_alerts = bot.get_cog("SocialAlerts")
            if social_alerts is None:
                raise HTTPException(status_code=503, detail="Social alerts cog is not loaded")

            body = await request.body()
            message_id = request.headers.get("Kick-Event-Message-Id", "")
            timestamp = request.headers.get("Kick-Event-Message-Timestamp", "")
            signature = request.headers.get("Kick-Event-Signature", "")
            event_type = request.headers.get("Kick-Event-Type", "")
            logger.info(
                "Incoming Kick webhook request: type=%s id=%s body_bytes=%s",
                event_type or "missing",
                message_id or "missing",
                len(body),
            )

            if not message_id or not timestamp or not signature or not event_type:
                logger.warning("Rejected Kick webhook request due to missing headers")
                raise HTTPException(status_code=400, detail="Missing Kick webhook headers")

            sent_at = parse_rfc3339_timestamp(timestamp)
            if sent_at is None:
                logger.warning("Rejected Kick webhook request due to invalid timestamp: %s", timestamp)
                raise HTTPException(status_code=400, detail="Invalid Kick webhook timestamp")

            if abs((datetime.now(timezone.utc) - sent_at).total_seconds()) > 600:
                logger.warning("Rejected Kick webhook request because timestamp was too old: %s", timestamp)
                raise HTTPException(status_code=400, detail="Kick webhook message is too old")

            if not await social_alerts.verify_kick_event_signature(body, message_id, timestamp, signature):
                logger.warning("Rejected Kick webhook request because signature verification failed")
                raise HTTPException(status_code=403, detail="Invalid Kick webhook signature")

            if message_id in social_alerts._recent_kick_event_messages:
                logger.info("Ignoring duplicate Kick webhook request id=%s", message_id)
                return Response(status_code=200)

            social_alerts._recent_kick_event_messages[message_id] = datetime.now(timezone.utc)
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
            social_alerts._recent_kick_event_messages = {
                key: value
                for key, value in list(social_alerts._recent_kick_event_messages.items())
                if value >= cutoff
            }

            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError as error:
                logger.warning("Rejected Kick webhook request due to invalid JSON: %s", error)
                raise HTTPException(status_code=400, detail="Invalid Kick webhook payload")

            await social_alerts.handle_kick_event_request(event_type, payload)
            return Response(status_code=204)
        except HTTPException:
            raise
        except Exception as error:
            logger.exception("Unhandled error while processing Kick webhook: %s", error)
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal Kick webhook error"}
            )

    @app.get(YOUTUBE_OAUTH_CALLBACK_PATH, response_class=HTMLResponse)
    async def youtube_oauth_callback(
        state: Optional[str] = None,
        code: Optional[str] = None,
        error: Optional[str] = None,
    ):
        """Complete the optional YouTube owner OAuth flow for social alerts."""
        social_alerts = bot.get_cog("SocialAlerts")
        if social_alerts is None:
            raise HTTPException(status_code=503, detail="Social alerts cog is not loaded")

        title, message = await social_alerts.handle_youtube_oauth_callback(
            state=state or "",
            code=code,
            error=error
        )
        return f"""
        <html>
            <head><title>{title}</title></head>
            <body style="font-family: sans-serif; max-width: 720px; margin: 40px auto; line-height: 1.5;">
                <h1>{title}</h1>
                <p>{message}</p>
                <p>You can close this page and return to Discord.</p>
            </body>
        </html>
        """

    @app.get(TIKTOK_OAUTH_CALLBACK_PATH, response_class=HTMLResponse)
    async def tiktok_oauth_callback(
        state: Optional[str] = None,
        code: Optional[str] = None,
        error: Optional[str] = None,
    ):
        """Complete the TikTok OAuth flow for social alerts."""
        social_alerts = bot.get_cog("SocialAlerts")
        if social_alerts is None:
            raise HTTPException(status_code=503, detail="Social alerts cog is not loaded")

        title, message = await social_alerts.handle_tiktok_oauth_callback(
            state=state or "",
            code=code,
            error=error
        )
        return f"""
        <html>
            <head><title>{title}</title></head>
            <body style="font-family: sans-serif; max-width: 720px; margin: 40px auto; line-height: 1.5;">
                <h1>{title}</h1>
                <p>{message}</p>
                <p>You can close this page and return to Discord.</p>
            </body>
        </html>
        """

    return app
