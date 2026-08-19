# Discord Voice Attendance Bot

Python Discord bot that tracks **voice join** and **voice leave** events and stores completed attendance sessions in **Firebase Firestore**.

It intentionally **does not record voice channel switches** as separate attendance. A session starts when a user joins any voice/stage channel from no voice channel, and ends when the user leaves voice completely.

## Firestore structure

Collection: `attendance`

Document ID: Discord user ID, for example `123456789012345678`

Example document:

```json
{
  "discord_user_id": "123456789012345678",
  "discord_name": "username",
  "discord_tag": "username#0",
  "global_name": "Display Name",
  "display_name": "Server Display Name",
  "nick_name": "Server Nickname",
  "last_seen_guild_id": "111111111111111111",
  "last_seen_guild_name": "My Discord Server",
  "total_seconds": 9000,
  "dates": {
    "2026-07-25": {
      "total_seconds": 5400,
      "sessions": [
        {
          "guild_id": "111111111111111111",
          "guild_name": "My Discord Server",
          "channel_id": "222222222222222222",
          "channel_name": "General Voice",
          "join_time": "2026-07-25T18:00:00+05:30",
          "leave_time": "2026-07-25T19:30:00+05:30",
          "duration_seconds": 5400
        },
        {
          "guild_id": "111111111111111111",
          "guild_name": "My Discord Server",
          "channel_id": "222222222222222222",
          "channel_name": "General Voice",
          "join_time": "2026-07-25T20:00:00+05:30",
          "leave_time": "2026-07-25T21:00:00+05:30",
          "duration_seconds": 3600
        }
      ]
    }
  }
}
```

Collection: `active_voice_sessions`

This collection stores currently open voice sessions so the bot can close them later when the user leaves.

## Discord slash commands

| Command | Use |
|---|---|
| `/ping` | Check if bot is online. |
| `/attendance_status` | Show your active voice session. |
| `/attendance_status member:@user` | Admin: show another member's active session. |
| `/attendance_today` | Show your completed sessions for today. |
| `/attendance_today member:@user` | Admin: show another member's completed sessions for today. |
| `/attendance_force_close member:@user` | Admin: force-close an active session and store it. |
| `/attendance_sync_active` | Admin: create active records for users already in voice after a bot restart. |

## Setup

### 1. Create a Discord bot

1. Go to <https://discord.com/developers/applications>.
2. Create an application.
3. Open **Bot** tab and create/reset the token.
4. Enable these privileged gateway intents:
   - **Server Members Intent**
   - Voice state events are used by the bot; `discord.py` enables this with `Intents.voice_states`.
5. Invite the bot to your server with scopes:
   - `bot`
   - `applications.commands`
6. Bot permissions can be simple. It does not need admin. Recommended:
   - View Channels
   - Use Slash Commands

### 2. Create Firebase service account

1. Go to Firebase Console.
2. Open your project.
3. Go to **Project settings > Service accounts**.
4. Click **Generate new private key**.
5. Download the JSON file.
6. Put it in this project folder as:

```bash
firebase-service-account.json
```

Do **not** upload or share this JSON file.

### 3. Install Python packages

Python 3.10+ is recommended.

```bash
cd discord_voice_attendance_bot
python -m venv .venv

# Windows PowerShell:
.venv\Scripts\Activate.ps1

# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### 4. Create `.env`

```bash
cp .env.example .env
```

Edit `.env`:

```env
DISCORD_TOKEN=your_discord_bot_token
DISCORD_GUILD_ID=your_server_id_optional_for_fast_command_sync
FIREBASE_SERVICE_ACCOUNT=firebase-service-account.json
ATTENDANCE_COLLECTION=attendance
ACTIVE_SESSIONS_COLLECTION=active_voice_sessions
TIMEZONE=Asia/Kolkata
```

`DISCORD_GUILD_ID` is optional but recommended while testing because slash commands appear instantly in that server. Global slash commands can take up to about 1 hour to show.

### 5. Run the bot

```bash
python bot.py
```

## Health API (for uptime monitors / Render)

The bot runs a lightweight HTTP server alongside the Discord connection.

| Endpoint | Response | Use |
|---|---|---|
| `GET /health` | `{"status": "ok", "bot": "ready"}` | Uptime monitor health check |
| `GET /` | `{"status": "ok"}` | Simple ping |

Configure your uptime monitor to check `http://your-host:8080/health`.

- `WEB_PORT` defaults to `8080`. Set it in `.env` to match your deployment port.
- If the bot is still starting up, `/health` returns `{"status": "ok", "bot": "not_ready"}`.
- When Discord is connected and slash commands are synced, `/health` returns `{"status": "ok", "bot": "ready"}`.

### Render deployment

In your Render dashboard:

1. **Start Command**: `python bot.py`
2. **Environment Variables** (add all from `.env`):
   - `DISCORD_TOKEN`
   - `FIREBASE_SERVICE_ACCOUNT` (upload the JSON as a file, reference it by filename)
   - `WEB_PORT=10000` (Render assigns port via this env var; the bot reads it)
3. **Health Check Path**: `/health`

## Important behavior notes

- Joining voice from no channel creates an active session.
- Leaving voice completely closes the active session and writes it to `attendance`.
- Switching from one voice channel to another is ignored and does not create a new join/leave record.
- The stored `channel_id` is the first channel the user joined for that session.
- If the bot restarts while users are already in voice, Discord will not send old join events. Use `/attendance_sync_active`; those users will get `join_time` equal to the sync command time.
- If a user leaves while the bot is offline, the bot cannot know that leave time. Use `/attendance_force_close` for stuck active sessions.

## Firestore security note

This bot uses a Firebase Admin service account. Admin SDK bypasses Firestore rules, so keep the service account JSON private and run the bot only on a trusted server.
