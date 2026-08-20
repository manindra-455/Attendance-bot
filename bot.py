from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import discord
from flask import Flask, jsonify
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()
FIREBASE_SERVICE_ACCOUNT = os.getenv(
    "FIREBASE_SERVICE_ACCOUNT", "firebase-service-account.json"
).strip()
ATTENDANCE_COLLECTION = os.getenv("ATTENDANCE_COLLECTION", "attendance").strip()
ACTIVE_SESSIONS_COLLECTION = os.getenv(
    "ACTIVE_SESSIONS_COLLECTION", "active_voice_sessions"
).strip()
TIMEZONE_NAME = os.getenv("TIMEZONE", "Asia/Kolkata").strip()

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Add it to your .env file.")

try:
    LOCAL_TZ = ZoneInfo(TIMEZONE_NAME)
except Exception as exc:  # pragma: no cover - startup guard
    raise RuntimeError(f"Invalid TIMEZONE={TIMEZONE_NAME!r}") from exc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("voice-attendance-bot")

# -----------------------------------------------------------------------------
# Firebase / Firestore
# -----------------------------------------------------------------------------

if not os.path.exists(FIREBASE_SERVICE_ACCOUNT):
    raise RuntimeError(
        f"Firebase service account file not found: {FIREBASE_SERVICE_ACCOUNT}\n"
        "Download it from Firebase Console > Project settings > Service accounts, "
        "then put the JSON file here or update FIREBASE_SERVICE_ACCOUNT in .env."
    )

cred = credentials.Certificate(FIREBASE_SERVICE_ACCOUNT)
firebase_admin.initialize_app(cred)
db = firestore.client()

# -----------------------------------------------------------------------------
# Time helpers
# -----------------------------------------------------------------------------


def now_local() -> datetime:
    """Current local time according to TIMEZONE in .env."""
    return datetime.now(LOCAL_TZ)



def to_iso(dt: datetime) -> str:
    """ISO timestamp with timezone offset, seconds precision."""
    return dt.isoformat(timespec="seconds")



def get_date_key(dt: datetime) -> str:
    """Date key used under attendance.<user_id>.dates."""
    return dt.strftime("%Y-%m-%d")



def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=LOCAL_TZ)
        return parsed.astimezone(LOCAL_TZ)
    except ValueError:
        return None


# -----------------------------------------------------------------------------
# Formatting
# -----------------------------------------------------------------------------


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"



def format_hhmmss(seconds: int) -> str:
    """Fixed width `HH:MM:SS` string, handy for sheets/exports."""
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


# -----------------------------------------------------------------------------
# Attendance shape helpers (date-wise totals)
# -----------------------------------------------------------------------------
#
# attendance/{user_id}
# {
#   "discord_user_id": "...",
#   "total_seconds": 12345,
#   "total_duration": "3h 25m 45s",
#   "dates": {
#       "2026-08-20": {
#           "sessions": [ {...}, {...} ],
#           "session_count": 2,
#           "total_seconds": 5400,
#           "total_duration": "1h 30m 0s",
#           "total_hhmmss": "01:30:00",
#           "total_minutes": 90.0,
#           "total_hours": 1.5,
#           "first_join_time": "2026-08-20T10:00:00+05:30",
#           "last_leave_time": "2026-08-20T11:30:00+05:30",
#           "updated_at": "2026-08-20T11:30:00+05:30"
#       }
#   }
# }
#
# Older documents stored `dates.<date>` as a plain list of sessions.
# Everything below reads BOTH shapes and writes the new one.


def sessions_from_day(raw: Any) -> list[dict[str, Any]]:
    """Return the session list for a day bucket in either the old or new shape."""
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, dict)]
    if isinstance(raw, dict):
        sessions = raw.get("sessions")
        if isinstance(sessions, list):
            return [s for s in sessions if isinstance(s, dict)]
    return []



def sum_durations(sessions: Iterable[dict[str, Any]]) -> int:
    total = 0
    for session in sessions:
        try:
            total += max(0, int(session.get("duration_seconds") or 0))
        except (TypeError, ValueError):
            continue
    return total



def build_day_bucket(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the day document (sessions + date-wise total duration)."""
    ordered = sorted(sessions, key=lambda s: str(s.get("join_time") or ""))
    total_seconds = sum_durations(ordered)

    join_times = [s.get("join_time") for s in ordered if s.get("join_time")]
    leave_times = [s.get("leave_time") for s in ordered if s.get("leave_time")]

    return {
        "sessions": ordered,
        "session_count": len(ordered),
        "total_seconds": total_seconds,
        "total_duration": format_duration(total_seconds),
        "total_hhmmss": format_hhmmss(total_seconds),
        "total_minutes": round(total_seconds / 60, 2),
        "total_hours": round(total_seconds / 3600, 2),
        "first_join_time": min(join_times) if join_times else None,
        "last_leave_time": max(leave_times) if leave_times else None,
        "updated_at": to_iso(now_local()),
    }



def day_total_seconds(raw: Any) -> int:
    """Date-wise total, trusting the stored value when present."""
    if isinstance(raw, dict) and raw.get("total_seconds") is not None:
        try:
            return max(0, int(raw["total_seconds"]))
        except (TypeError, ValueError):
            pass
    return sum_durations(sessions_from_day(raw))


# -----------------------------------------------------------------------------
# Discord/Firebase data helpers
# -----------------------------------------------------------------------------


def active_doc_id(guild_id: int | str, user_id: int | str) -> str:
    return f"{guild_id}_{user_id}"



def member_snapshot(member: discord.Member, channel: Optional[discord.abc.GuildChannel]) -> dict[str, Any]:
    """Make a serializable snapshot of Discord member/channel data."""
    guild = member.guild
    data: dict[str, Any] = {
        "guild_id": str(guild.id),
        "guild_name": guild.name,
        "discord_user_id": str(member.id),
        "discord_name": member.name,
        "discord_tag": str(member),
        "global_name": getattr(member, "global_name", None),
        "display_name": member.display_name,
        "nick_name": member.nick,
    }

    if channel is not None:
        data.update(
            {
                "channel_id": str(channel.id),
                "channel_name": channel.name,
            }
        )

    return data



def _write_join_sync(snapshot: dict[str, Any], join_dt_iso: str, join_date: str) -> dict[str, Any]:
    """Create an active voice session in Firestore.

    If a session already exists, we keep the original join_time because this bot
    intentionally does not count duplicate joins/switches as new attendance.
    """
    doc_id = active_doc_id(snapshot["guild_id"], snapshot["discord_user_id"])
    active_ref = db.collection(ACTIVE_SESSIONS_COLLECTION).document(doc_id)
    current = active_ref.get()

    if current.exists:
        # Keep original join_time, but refresh names in case username/nickname changed.
        active_ref.set(
            {
                "discord_name": snapshot.get("discord_name"),
                "discord_tag": snapshot.get("discord_tag"),
                "global_name": snapshot.get("global_name"),
                "display_name": snapshot.get("display_name"),
                "nick_name": snapshot.get("nick_name"),
                "updated_at": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        return {"status": "already_active", "doc_id": doc_id}

    active_ref.set(
        {
            **snapshot,
            "join_time": join_dt_iso,
            "join_date": join_date,
            "created_at": firestore.SERVER_TIMESTAMP,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
    )
    return {"status": "created", "doc_id": doc_id}



def _close_active_session_sync(
    guild_id: str,
    user_id: str,
    leave_snapshot: Optional[dict[str, Any]] = None,
    forced_by: Optional[str] = None,
) -> dict[str, Any]:
    """Close an active session, append it to attendance and refresh totals.

    Totals maintained:
      * attendance.dates.<date>.total_seconds / total_duration  (date-wise)
      * attendance.total_seconds / total_duration               (all-time)
    """
    leave_dt = now_local()
    leave_dt_iso = to_iso(leave_dt)

    doc_id = active_doc_id(guild_id, user_id)
    active_ref = db.collection(ACTIVE_SESSIONS_COLLECTION).document(doc_id)
    attendance_ref = db.collection(ATTENDANCE_COLLECTION).document(user_id)

    transaction = db.transaction()

    @firestore.transactional
    def _txn(tx: firestore.Transaction) -> dict[str, Any]:
        # --- reads first (Firestore requires all reads before writes) ---------
        active_snap = active_ref.get(transaction=tx)
        if not active_snap.exists:
            return {"status": "missing_active_session", "doc_id": doc_id}

        attendance_snap = attendance_ref.get(transaction=tx)
        attendance_data = (attendance_snap.to_dict() or {}) if attendance_snap.exists else {}

        active = active_snap.to_dict() or {}
        join_dt = parse_iso(active.get("join_time"))
        duration_seconds = 0
        if join_dt is not None:
            duration_seconds = max(0, int((leave_dt - join_dt).total_seconds()))

        # Store under the date on which the user originally joined.
        date_key = active.get("join_date")
        if not date_key:
            date_key = get_date_key(join_dt if join_dt else leave_dt)

        fresh = leave_snapshot or {}
        guild_name = fresh.get("guild_name") or active.get("guild_name")
        discord_name = fresh.get("discord_name") or active.get("discord_name")
        discord_tag = fresh.get("discord_tag") or active.get("discord_tag")
        global_name = fresh.get("global_name") or active.get("global_name")
        display_name = fresh.get("display_name") or active.get("display_name")
        nick_name = fresh.get("nick_name") if "nick_name" in fresh else active.get("nick_name")

        session: dict[str, Any] = {
            "guild_id": str(guild_id),
            "guild_name": guild_name,
            # Switches are ignored, so this remains the first channel joined.
            "channel_id": active.get("channel_id"),
            "channel_name": active.get("channel_name"),
            "date": date_key,
            "join_time": active.get("join_time"),
            "leave_time": leave_dt_iso,
            "duration_seconds": duration_seconds,
            "duration_text": format_duration(duration_seconds),
        }

        if forced_by:
            session["forced_close"] = True
            session["forced_by"] = forced_by

        # --- date-wise rollup -------------------------------------------------
        all_dates = attendance_data.get("dates") or {}
        existing_day = all_dates.get(date_key) if isinstance(all_dates, dict) else None
        day_sessions = sessions_from_day(existing_day) + [session]
        day_bucket = build_day_bucket(day_sessions)

        # --- all-time rollup --------------------------------------------------
        if isinstance(all_dates, dict) and all_dates:
            grand_total = sum(
                day_total_seconds(value) if key != date_key else day_bucket["total_seconds"]
                for key, value in all_dates.items()
            )
            if date_key not in all_dates:
                grand_total += day_bucket["total_seconds"]
        else:
            grand_total = day_bucket["total_seconds"]

        tx.set(
            attendance_ref,
            {
                "discord_user_id": str(user_id),
                "discord_name": discord_name,
                "discord_tag": discord_tag,
                "global_name": global_name,
                "display_name": display_name,
                "nick_name": nick_name,
                "last_seen_guild_id": str(guild_id),
                "last_seen_guild_name": guild_name,
                "updated_at": firestore.SERVER_TIMESTAMP,
                "total_seconds": grand_total,
                "total_duration": format_duration(grand_total),
                "total_hours": round(grand_total / 3600, 2),
                "last_attendance_date": date_key,
                "dates": {
                    date_key: day_bucket,
                },
            },
            merge=True,
        )
        tx.delete(active_ref)

        return {
            "status": "closed",
            "doc_id": doc_id,
            "date_key": date_key,
            "duration_seconds": duration_seconds,
            "date_total_seconds": day_bucket["total_seconds"],
            "date_total_duration": day_bucket["total_duration"],
            "total_seconds": grand_total,
            "session": session,
        }

    return _txn(transaction)



def _get_active_session_sync(guild_id: str, user_id: str) -> Optional[dict[str, Any]]:
    doc_id = active_doc_id(guild_id, user_id)
    snap = db.collection(ACTIVE_SESSIONS_COLLECTION).document(doc_id).get()
    if not snap.exists:
        return None
    return snap.to_dict() or {}



def _get_attendance_day_sync(user_id: str, date_key: str) -> dict[str, Any]:
    """Return sessions + stored date-wise total for one day."""
    snap = db.collection(ATTENDANCE_COLLECTION).document(user_id).get()
    if not snap.exists:
        return {"sessions": [], "total_seconds": 0, "total_duration": format_duration(0)}

    data = snap.to_dict() or {}
    dates = data.get("dates") or {}
    raw_day = dates.get(date_key) if isinstance(dates, dict) else None

    sessions = sessions_from_day(raw_day)
    return {
        "sessions": sessions,
        "total_seconds": day_total_seconds(raw_day),
        "total_duration": format_duration(day_total_seconds(raw_day)),
    }



def _get_attendance_totals_sync(user_id: str, days: int) -> dict[str, Any]:
    """Date-wise totals for the last `days` calendar days (most recent first)."""
    snap = db.collection(ATTENDANCE_COLLECTION).document(user_id).get()
    if not snap.exists:
        return {"rows": [], "grand_total_seconds": 0}

    data = snap.to_dict() or {}
    dates = data.get("dates") or {}
    if not isinstance(dates, dict):
        return {"rows": [], "grand_total_seconds": 0}

    today = now_local().date()
    cutoff = today - timedelta(days=max(1, days) - 1)

    rows: list[dict[str, Any]] = []
    for date_key, raw_day in dates.items():
        try:
            day_date = datetime.strptime(str(date_key), "%Y-%m-%d").date()
        except ValueError:
            continue
        if day_date < cutoff or day_date > today:
            continue
        total = day_total_seconds(raw_day)
        rows.append(
            {
                "date": str(date_key),
                "total_seconds": total,
                "total_duration": format_duration(total),
                "session_count": len(sessions_from_day(raw_day)),
            }
        )

    rows.sort(key=lambda r: r["date"], reverse=True)
    return {
        "rows": rows,
        "grand_total_seconds": sum(r["total_seconds"] for r in rows),
        "all_time_seconds": int(data.get("total_seconds") or 0),
    }



def _recalculate_totals_sync(user_id: str) -> dict[str, Any]:
    """Backfill/repair date-wise totals for one attendance document.

    Also migrates legacy `dates.<date> = [sessions]` arrays into the new
    `{sessions, total_seconds, total_duration, ...}` map shape.
    """
    ref = db.collection(ATTENDANCE_COLLECTION).document(user_id)
    snap = ref.get()
    if not snap.exists:
        return {"status": "missing", "user_id": user_id}

    data = snap.to_dict() or {}
    dates = data.get("dates") or {}
    if not isinstance(dates, dict) or not dates:
        return {"status": "no_dates", "user_id": user_id}

    rebuilt: dict[str, Any] = {}
    grand_total = 0
    migrated = 0

    for date_key, raw_day in dates.items():
        sessions = sessions_from_day(raw_day)
        bucket = build_day_bucket(sessions)
        rebuilt[str(date_key)] = bucket
        grand_total += bucket["total_seconds"]
        if isinstance(raw_day, list):
            migrated += 1

    ref.set(
        {
            "dates": rebuilt,
            "total_seconds": grand_total,
            "total_duration": format_duration(grand_total),
            "total_hours": round(grand_total / 3600, 2),
            "updated_at": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )

    return {
        "status": "updated",
        "user_id": user_id,
        "days": len(rebuilt),
        "migrated_days": migrated,
        "total_seconds": grand_total,
    }



def _recalculate_all_totals_sync() -> dict[str, Any]:
    updated = 0
    migrated_days = 0
    failed = 0

    for doc in db.collection(ATTENDANCE_COLLECTION).stream():
        try:
            result = _recalculate_totals_sync(doc.id)
            if result["status"] == "updated":
                updated += 1
                migrated_days += result.get("migrated_days", 0)
        except Exception:
            failed += 1
            logger.exception("Failed to recalculate totals for %s", doc.id)

    return {"updated": updated, "migrated_days": migrated_days, "failed": failed}


# -----------------------------------------------------------------------------
# Discord bot setup
# -----------------------------------------------------------------------------

intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True
intents.members = True  # needed for nick_name/member details


class VoiceAttendanceBot(commands.Bot):
    async def setup_hook(self) -> None:
        if DISCORD_GUILD_ID:
            guild_obj = discord.Object(id=int(DISCORD_GUILD_ID))
            self.tree.copy_global_to(guild=guild_obj)
            synced = await self.tree.sync(guild=guild_obj)
            logger.info("Synced %s slash command(s) to guild %s", len(synced), DISCORD_GUILD_ID)
        else:
            synced = await self.tree.sync()
            logger.info("Synced %s global slash command(s)", len(synced))


bot = VoiceAttendanceBot(command_prefix="!", intents=intents)

# Flask health API
app = Flask(__name__)


@app.route("/health")
def health() -> tuple:
    bot_status = "ready" if (bot.is_ready() and bot.user) else "not_ready"
    return {"status": "ok", "bot": bot_status}, 200


@app.route("/")
def index() -> tuple:
    return {"status": "ok"}, 200


@app.route("/attendance/<user_id>/totals")
def attendance_totals_api(user_id: str) -> tuple:
    """Date-wise totals as JSON (last 30 days)."""
    result = _get_attendance_totals_sync(str(user_id), 30)
    return jsonify(
        {
            "status": "ok",
            "user_id": str(user_id),
            "dates": result["rows"],
            "range_total_seconds": result["grand_total_seconds"],
            "range_total_duration": format_duration(result["grand_total_seconds"]),
            "all_time_seconds": result.get("all_time_seconds", 0),
            "all_time_duration": format_duration(result.get("all_time_seconds", 0)),
        }
    ), 200


async def record_join(member: discord.Member, channel: discord.abc.GuildChannel) -> dict[str, Any]:
    join_dt = now_local()
    snapshot = member_snapshot(member, channel)
    return await asyncio.to_thread(
        _write_join_sync,
        snapshot,
        to_iso(join_dt),
        get_date_key(join_dt),
    )


async def record_leave(member: discord.Member, channel: discord.abc.GuildChannel) -> dict[str, Any]:
    snapshot = member_snapshot(member, channel)
    return await asyncio.to_thread(
        _close_active_session_sync,
        str(member.guild.id),
        str(member.id),
        snapshot,
        None,
    )


@bot.event
async def on_ready() -> None:
    logger.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="voice attendance")
    )


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    # Usually you do not want bot accounts in attendance.
    if member.bot:
        return

    # Real join: user was not in any voice/stage channel and now is in one.
    if before.channel is None and after.channel is not None:
        result = await record_join(member, after.channel)
        logger.info(
            "JOIN | user=%s guild=%s channel=%s result=%s",
            member.id,
            member.guild.id,
            after.channel.id,
            result["status"],
        )
        return

    # Real leave: user was in a voice/stage channel and now is in none.
    if before.channel is not None and after.channel is None:
        result = await record_leave(member, before.channel)
        logger.info(
            "LEAVE | user=%s guild=%s channel=%s result=%s duration=%ss day_total=%ss",
            member.id,
            member.guild.id,
            before.channel.id,
            result["status"],
            result.get("duration_seconds", 0),
            result.get("date_total_seconds", 0),
        )
        return

    # Switches and mute/deafen/status-only voice updates are intentionally ignored.
    # Example switch: before.channel != None and after.channel != None.


# -----------------------------------------------------------------------------
# Slash commands
# -----------------------------------------------------------------------------


@bot.tree.command(name="ping", description="Check if the attendance bot is online.")
async def ping(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("✅ Pong. Voice attendance bot is online.", ephemeral=True)


@bot.tree.command(
    name="attendance_status",
    description="Show whether you, or a selected member, currently have an active voice session.",
)
@app_commands.describe(member="Optional member to check. Defaults to yourself.")
async def attendance_status(
    interaction: discord.Interaction,
    member: Optional[discord.Member] = None,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
        return

    target = member or interaction.user
    if not isinstance(target, discord.Member):
        await interaction.response.send_message("Could not read that member.", ephemeral=True)
        return

    if member is not None and not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You need Manage Server permission to check another member.",
            ephemeral=True,
        )
        return

    data = await asyncio.to_thread(
        _get_active_session_sync,
        str(interaction.guild.id),
        str(target.id),
    )

    if not data:
        await interaction.response.send_message(
            f"ℹ️ {target.mention} has no active tracked voice session.",
            ephemeral=True,
        )
        return

    join_dt = parse_iso(data.get("join_time"))
    running = max(0, int((now_local() - join_dt).total_seconds())) if join_dt else 0

    await interaction.response.send_message(
        "🟢 Active voice session\n"
        f"Member: {target.mention}\n"
        f"Channel: `{data.get('channel_name')}` (`{data.get('channel_id')}`)\n"
        f"Join time: `{data.get('join_time')}`\n"
        f"Running for: `{format_duration(running)}` (not counted until leave)",
        ephemeral=True,
    )


@bot.tree.command(
    name="attendance_today",
    description="Show completed voice sessions and the total duration for a date.",
)
@app_commands.describe(
    member="Optional member to check. Defaults to yourself.",
    date="Optional date as YYYY-MM-DD. Defaults to today.",
)
async def attendance_today(
    interaction: discord.Interaction,
    member: Optional[discord.Member] = None,
    date: Optional[str] = None,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
        return

    target = member or interaction.user
    if not isinstance(target, discord.Member):
        await interaction.response.send_message("Could not read that member.", ephemeral=True)
        return

    if member is not None and not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You need Manage Server permission to check another member.",
            ephemeral=True,
        )
        return

    if date:
        try:
            date_key = get_date_key(datetime.strptime(date.strip(), "%Y-%m-%d"))
        except ValueError:
            await interaction.response.send_message(
                "Invalid date. Use the `YYYY-MM-DD` format, e.g. `2026-08-20`.",
                ephemeral=True,
            )
            return
    else:
        date_key = get_date_key(now_local())

    day = await asyncio.to_thread(_get_attendance_day_sync, str(target.id), date_key)

    # Stored total covers every guild; the listing below is guild-scoped.
    all_sessions = day["sessions"]
    sessions = [s for s in all_sessions if str(s.get("guild_id")) == str(interaction.guild.id)]

    if not sessions:
        await interaction.response.send_message(
            f"No completed sessions for {target.mention} on `{date_key}` yet.",
            ephemeral=True,
        )
        return

    guild_total = sum_durations(sessions)
    stored_total = int(day["total_seconds"])

    lines = [
        f"📅 Completed sessions for {target.mention} on `{date_key}`",
        f"🕒 Total duration (this server): `{format_duration(guild_total)}` "
        f"across `{len(sessions)}` session(s)",
    ]
    if stored_total != guild_total:
        lines.append(f"🌐 Total duration (all servers): `{format_duration(stored_total)}`")
    lines.append("")

    for index, session in enumerate(sessions[-10:], start=max(1, len(sessions) - 9)):
        lines.append(
            f"`{index}.` `{session.get('channel_name')}` "
            f"join `{session.get('join_time')}` → leave `{session.get('leave_time')}` "
            f"(`{format_duration(int(session.get('duration_seconds') or 0))}`)"
        )

    if len(sessions) > 10:
        lines.append(f"...showing last 10 of {len(sessions)} sessions.")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(
    name="attendance_totals",
    description="Show date-wise total voice duration for the last N days.",
)
@app_commands.describe(
    member="Optional member to check. Defaults to yourself.",
    days="How many days back to include (1-31). Defaults to 7.",
)
async def attendance_totals(
    interaction: discord.Interaction,
    member: Optional[discord.Member] = None,
    days: Optional[int] = 7,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
        return

    target = member or interaction.user
    if not isinstance(target, discord.Member):
        await interaction.response.send_message("Could not read that member.", ephemeral=True)
        return

    if member is not None and not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You need Manage Server permission to check another member.",
            ephemeral=True,
        )
        return

    window = min(31, max(1, int(days or 7)))
    result = await asyncio.to_thread(_get_attendance_totals_sync, str(target.id), window)
    rows = result["rows"]

    if not rows:
        await interaction.response.send_message(
            f"No attendance recorded for {target.mention} in the last `{window}` day(s).",
            ephemeral=True,
        )
        return

    lines = [
        f"📊 Date-wise total duration for {target.mention} (last `{window}` day(s))",
        "",
    ]
    for row in rows:
        lines.append(
            f"`{row['date']}` → `{row['total_duration']}` "
            f"({row['session_count']} session(s))"
        )

    lines.append("")
    lines.append(f"Σ Range total: `{format_duration(result['grand_total_seconds'])}`")
    lines.append(f"Σ All-time total: `{format_duration(result.get('all_time_seconds', 0))}`")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(
    name="attendance_force_close",
    description="Admin: force-close a member's active session and store it in attendance.",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(member="Member whose active session should be closed.")
async def attendance_force_close(
    interaction: discord.Interaction,
    member: discord.Member,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
        return

    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You need Manage Server permission to force-close attendance.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    forced_by = f"{interaction.user} ({interaction.user.id})"
    result = await asyncio.to_thread(
        _close_active_session_sync,
        str(interaction.guild.id),
        str(member.id),
        None,
        forced_by,
    )

    if result["status"] != "closed":
        await interaction.followup.send(
            f"No active session found for {member.mention}.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        f"✅ Closed active session for {member.mention}. "
        f"Duration: `{format_duration(result['duration_seconds'])}`. "
        f"Stored under date `{result['date_key']}`.\n"
        f"🕒 Total for `{result['date_key']}`: `{result['date_total_duration']}`.",
        ephemeral=True,
    )


@bot.tree.command(
    name="attendance_recalculate",
    description="Admin: rebuild date-wise totals (also migrates old records).",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(member="Optional member. Leave empty to rebuild every member.")
async def attendance_recalculate(
    interaction: discord.Interaction,
    member: Optional[discord.Member] = None,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
        return

    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You need Manage Server permission to recalculate attendance.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    if member is not None:
        result = await asyncio.to_thread(_recalculate_totals_sync, str(member.id))
        if result["status"] != "updated":
            await interaction.followup.send(
                f"Nothing to recalculate for {member.mention} (`{result['status']}`).",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"✅ Rebuilt totals for {member.mention}.\n"
            f"Days: `{result['days']}` (migrated `{result['migrated_days']}` legacy day(s))\n"
            f"All-time total: `{format_duration(result['total_seconds'])}`",
            ephemeral=True,
        )
        return

    summary = await asyncio.to_thread(_recalculate_all_totals_sync)
    await interaction.followup.send(
        "✅ Rebuilt date-wise totals for all attendance documents.\n"
        f"Updated: `{summary['updated']}`\n"
        f"Legacy days migrated: `{summary['migrated_days']}`\n"
        f"Failed: `{summary['failed']}`",
        ephemeral=True,
    )


@bot.tree.command(
    name="attendance_sync_active",
    description="Admin: create active records for users already in voice now.",
)
@app_commands.default_permissions(manage_guild=True)
async def attendance_sync_active(interaction: discord.Interaction) -> None:
    """Useful after restarting the bot while people are already in voice.

    Discord does not tell a newly-started bot when already-connected users joined,
    so their join_time is set to the moment this command runs.
    """
    if interaction.guild is None:
        await interaction.response.send_message("Use this command inside a server.", ephemeral=True)
        return

    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You need Manage Server permission to sync active attendance.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    created = 0
    skipped = 0
    failed = 0

    channels: list[discord.abc.GuildChannel] = []
    channels.extend(interaction.guild.voice_channels)
    channels.extend(interaction.guild.stage_channels)

    for channel in channels:
        members = getattr(channel, "members", [])
        for member in members:
            if member.bot:
                continue
            try:
                result = await record_join(member, channel)
                if result["status"] == "created":
                    created += 1
                else:
                    skipped += 1
            except Exception:
                failed += 1
                logger.exception("Failed to sync active member %s", member.id)

    await interaction.followup.send(
        "✅ Active voice sync finished.\n"
        f"Created: `{created}`\n"
        f"Already active/skipped: `{skipped}`\n"
        f"Failed: `{failed}`\n\n"
        "Note: synced users get `join_time` equal to the time this command ran.",
        ephemeral=True,
    )


if __name__ == "__main__":
    import threading

    port = int(os.environ.get("PORT", 8000))

    # Run Flask health API in a background thread
    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False),
        daemon=True,
    )
    flask_thread.start()
    logger.info("Health API running on 0.0.0.0:%s", port)

    # Run Discord bot in the main thread
    bot.run(DISCORD_TOKEN)
