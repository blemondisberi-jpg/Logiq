# 🤖 Logiq - Open Source Discord Bot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Discord.py](https://img.shields.io/badge/discord.py-2.3+-blue.svg)](https://github.com/Rapptz/discord.py)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](http://makeapullrequest.com)

**The Open-Source Alternative to MEE6**

Logiq is a feature-rich, fully open-source Discord bot packed with the systems most servers actually need: verification, role menus, alerts, moderation, welcome cards, server utility, temporary voice, tickets, birthdays, audit logging, embeds, and more. Built by **Programmify** and the open-source community.

🌟 **Star this repo** if you find it useful!

---

## 🚀 Quick Start

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Create Your `.env`
```env
DISCORD_BOT_TOKEN=your_discord_bot_token
MONGODB_URI=mongodb://localhost:27017
ENVIRONMENT=development
```

### 3. Run The Bot
```bash
python main.py
```

### 4. Sync Commands
Use:
```text
/sync
```

If Discord still looks stale, restart the client and give global commands a little time to refresh.

---

## ✨ What Logiq Does Right Now

### 🔐 Verification & Onboarding
- Rules panels with a built-in **Accept** flow
- Optional **captcha in DMs**
- Optional **platform-link verification** after rules acceptance
- Platform-role assignment for **Twitch**, **YouTube**, and **Kick**
- Optional **signpost message** to direct new members to another channel or panel
- Custom welcome DMs
- Join **welcome cards** with avatar, background image, title, subtitle, and font controls

### 🎭 Roles & Access
- Modal-based self-role panels
- **Exclusive** one-role dropdowns and regular multi-select menus
- Repair tools for role panels after role renames or message drift
- Prefab **25-colour role panels**
- Bulk role tools for entire servers or filtered groups

### 📢 Social Alerts
- **Twitch live alerts** with EventSub + polling fallback
- **Kick live alerts** with webhook sync + fallback checking
- **YouTube live alerts**
- **YouTube upload alerts** for new videos
- **TikTok post alerts**
- **Instagram post alerts**
- Per-alert custom message templates
- Test, debug, run-now, and subscription sync tools

### 🖼️ Embed Tools
- Build embeds directly from slash commands
- Build rules panels with the verification button attached underneath
- Edit previously sent embeds and rules panels without recreating them

### 🎫 Server Systems
- Ticket panel + ticket logging
- Temporary voice channels with ownership controls
- Birthday storage + automatic birthday announcements
- Audit logging for joins, leaves, moderation actions, role/channel changes, and more
- Server stats channels, including member counters and time channels
- World time lookup by country

### 🛡️ Core Utility
- Moderation suite
- Leveling
- Economy
- Analytics
- Giveaways
- Games
- Music
- AI chat
- Admin controls
- Built-in `/help`

---

## 🧩 Cog Overview

Logiq currently ships with these cogs:

- `admin.py`
- `ai_chat.py`
- `analytics.py`
- `audit_log.py`
- `birthdays.py`
- `economy.py`
- `embed_builder.py`
- `games.py`
- `giveaways.py`
- `leveling.py`
- `moderation.py`
- `music.py`
- `roles.py`
- `server_stats_channels.py`
- `social_alerts.py`
- `temp_voice.py`
- `tickets.py`
- `utility.py`
- `verification.py`
- `world_time.py`

---

## ⚙️ Essential Setup

### Core Environment Variables
```env
DISCORD_BOT_TOKEN=your_token
MONGODB_URI=your_mongodb_uri
ENVIRONMENT=production
```

### Optional But Commonly Needed
```env
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
```

### Social Alert Variables

#### Twitch Live Alerts
```env
TWITCH_CLIENT_ID=
TWITCH_CLIENT_SECRET=
TWITCH_EVENTSUB_SECRET=
PUBLIC_BASE_URL=https://your-public-domain.com
```

#### Kick Live Alerts
```env
KICK_CLIENT_ID=
KICK_CLIENT_SECRET=
PUBLIC_BASE_URL=https://your-public-domain.com
```

#### YouTube Live + Upload Alerts
```env
YOUTUBE_API_KEY=
YOUTUBE_OAUTH_CLIENT_ID=
YOUTUBE_OAUTH_CLIENT_SECRET=
PUBLIC_BASE_URL=https://your-public-domain.com
```

#### TikTok Post Alerts
```env
TIKTOK_CLIENT_KEY=
TIKTOK_CLIENT_SECRET=
PUBLIC_BASE_URL=https://your-public-domain.com
```

#### Instagram Post Alerts
Instagram is currently connected per alert with:
- an Instagram **professional account ID**
- a valid **Instagram access token**

This is stored through the Discord admin command rather than a global app-wide key pair.

---

## 🚂 Deploy To Railway

1. Push the repo to GitHub.
2. Create a Railway project from the repo.
3. Add MongoDB or supply your external MongoDB URI.
4. Set your environment variables in Railway.
5. Deploy.

If you want Twitch, Kick, TikTok, or YouTube OAuth callback flows to work, your Railway app must have a working public HTTPS domain and that domain must be used as `PUBLIC_BASE_URL`.

---

## 🔐 Verification Flow

The old `/setup-verification` flow has been replaced.

### The Current Setup Path

1. Set the role members should receive after accepting the rules:
```text
/verification-role
```

2. Create or send your rules panel:
```text
/embed_rules
```

3. If needed, customise follow-up behaviour:
- `/verification-mode`
- `/verification-platform-toggle`
- `/verification-platform-role`
- `/verification-signpost`
- `/set-welcome-message`
- `/welcome-card-config`

### Supported Flows

#### Standard Flow
Member reads rules → clicks **Accept** → receives verification role

#### Captcha Flow
Member reads rules → clicks **Accept** → receives captcha in DMs → completes captcha → receives verification role

#### Platform-Link Flow
Member reads rules → clicks **Accept** → links Twitch/YouTube/Kick profile → receives platform role + verified role

#### Combined Flow
Member reads rules → clicks **Accept** → completes captcha in DMs → links platform → receives full access

---

## 📢 Social Alerts Setup

Logiq now supports different alert types:

- `live`
- `post`

### Supported Platform/Type Combinations

- Twitch: `live`
- Kick: `live`
- YouTube: `live`, `post`
- TikTok: `post`
- Instagram: `post`

### Basic Alert Commands

```text
/alert add
/alert edit
/alert remove
/alert list
/alert test
/alert debug
/alert run
```

### Twitch
Use a `live` alert and make sure EventSub is configured.

### Kick
Use a `live` alert and make sure your webhook sync is healthy.

### YouTube
- `live` alerts monitor streams
- `post` alerts monitor newly uploaded videos
- Optional owner OAuth is available for the YouTube live path:
  - `/alert youtube-oauth-connect`
  - `/alert youtube-oauth-disconnect`

### TikTok
Create a `post` alert, then connect the owning account:
```text
/alert tiktok-connect
```

Disconnect with:
```text
/alert tiktok-disconnect
```

### Instagram
Create a `post` alert, then connect manual credentials:
```text
/alert instagram-connect-manual
```

Disconnect with:
```text
/alert instagram-disconnect
```

### Subscription Repair Commands
```text
/alert eventsub-sync
/alert kick-sync
```

---

## 🖼️ Embed Tools

### Available Commands
- `/embed_create`
- `/embed_rules`
- `/embed_edit`
- `/embed_rules_edit`

### What You Can Do
- Send standard embeds to any text channel
- Send rules embeds with the verification button directly attached underneath
- Edit bot-made embeds in place
- Keep rich formatting in rules text
- Add embed images, thumbnails, footers, and colours

---

## 🎭 Role Menus & Colour Panels

### Role Commands
- `/create-role-menu`
- `/repair-role-menu`
- `/create-color-panel`
- `/addrole`
- `/removerole`
- `/massrole-add`
- `/massrole-remove`
- `/massrole-add-filter`
- `/massrole-remove-filter`

### Role Menu Notes
- Role menus support up to **25 roles**
- Exclusive menus allow **one active choice at a time**
- Exclusive users can switch to another allowed option without breaking the one-role rule
- If a role is renamed later, use `/repair-role-menu` to refresh the displayed labels on older panels

---

## 🎂 Birthdays

### Commands
- `/birthday-set`
- `/birthday-remove`
- `/birthday-config`
- `/birthday-list`
- `/birthday-test`

Members can store their birthday and the bot will announce it automatically in the configured channel.

---

## 🛰️ Server Stats & Time

### Commands
- `/serverstats`
- `/time-country`

Server stats channels and clock channels can mirror:
- total members
- humans
- bots
- country-based time labels

These channels update on an interval, so they are best treated as a **clean server display feature**, not as a second-perfect wall clock.

---

## 🎫 Tickets

### Commands
- `/ticket-setup`
- `/ticket-panel`
- `/tickets`
- `/close-ticket`

Creates a persistent ticket panel with private ticket channels and optional ticket logging.

---

## 🔊 Temporary Voice

### Commands
- `/setup-tempvoice`
- `/voice-lock`
- `/voice-unlock`
- `/voice-limit`
- `/voice-rename`
- `/voice-claim`

---

## 🛡️ Moderation

### Commands
- `/warn`
- `/warnings`
- `/timeout`
- `/kick`
- `/ban`
- `/unban`
- `/clear`
- `/slowmode`
- `/lock`
- `/unlock`
- `/nickname`
- `/purge`

---

## 📈 Leveling, Economy & Games

### Leveling
- `/setlevelchannel`
- `/setlevel`
- `/resetlevels`
- `/rank`
- `/leaderboard`

### Economy
- `/balance`
- `/daily`
- `/give`
- `/coinflip-bet`
- `/shop`
- `/addbalance`

### Games
- `/setup-game-panel`

### Giveaways
- `/giveaway`
- `/gend`
- `/greroll`

---

## 🎵 Music

### Commands
- `/play`
- `/join`
- `/leave`
- `/queue`
- `/skip`
- `/pause`
- `/resume`
- `/volume`
- `/nowplaying`

Audio playback still depends on the usual voice/music extras such as FFmpeg and compatible extraction tooling.

---

## 🤖 AI Chat

### Commands
- `/ask`
- `/clear-conversation`
- `/summarize`

If AI chat says it is disabled, set a valid provider key first and make sure the AI module is enabled in config.

---

## 🧰 Utility & Admin

### Utility
- `/help`
- `/poll`
- `/remind`
- `/userinfo`
- `/avatar`

### Admin
- `/reload`
- `/sync`
- `/modules`
- `/botinfo`
- `/setlogchannel`
- `/config`

### Analytics
- `/analytics`
- `/activity`

### Audit Logging
- `/auditlog-status`

---

## 📁 Project Structure

```text
logiq/
├── main.py
├── config.yaml
├── requirements.txt
├── railway.json
├── runtime.txt
├── cogs/
├── database/
├── utils/
└── web/
```

---

## 🐛 Troubleshooting

### Commands Not Showing
1. Run `/sync`
2. Restart your Discord client
3. Give Discord time to refresh global commands

### Alert Callbacks Not Working
- Check `PUBLIC_BASE_URL`
- Make sure the URL is publicly reachable over HTTPS
- Check Railway HTTP logs
- Use `/alert debug`
- Use `/alert eventsub-sync` or `/alert kick-sync` when relevant

### AI Chat Says It Is Disabled
- Set `OPENAI_API_KEY` or your chosen provider key
- Confirm the AI module is enabled

### Welcome Cards Or Web Features Acting Up
- Confirm MongoDB is reachable
- Confirm the bot can fetch the configured asset URLs
- Check Railway deploy logs and HTTP logs

---

## 🔒 Security

- Never commit `.env`
- Keep bot tokens and API keys private
- Use environment variables for secrets
- Rotate keys if they are exposed
- Prefer least-privilege credentials wherever possible

---

## 🤝 Contributing

Contributions are welcome.

1. Fork the repository
2. Clone your fork
3. Create a feature branch
4. Make your changes
5. Test thoroughly
6. Open a pull request with a clear description

Please keep changes focused, update documentation where needed, and match the existing style of the project.

---

## 📝 License

**MIT License**

Copyright (c) 2025 Programmify

See [LICENSE](LICENSE) for full details.

### Optional Enhancements (Not Required)
- Music audio playback: Install `yt-dlp` and `PyNaCl` + FFmpeg for YouTube playback
- Social alerts live monitoring: Add API keys to .env for real-time notifications

---

## 💬 Support

For licensing inquiries or support, contact Programmify.

---

**Built by Programmify with ❤️**

**Open Source MEE6 Alternative - Completely Free!**
