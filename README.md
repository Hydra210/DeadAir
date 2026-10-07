# DeadAir

Discord bot that checks Roblox group audio: is it working, is it moderated, who owns it, how long it is,
and what it sounds like (a black-and-white loudness waveform). No emojis, everything in embeds.

## Commands

| Command | What it does |
|---|---|
| `/check ids:<one ID>` | Full card: status, length, group (link + ID), dates, loudness stats, waveform image |
| `/check ids:<several IDs>` | Quick list with name and working/moderated status per ID |
| `/search group_id term [exact] [only_moderated]` | Search a group's audio by keyword(s) or exact name |
| `/join group_id` | Make the bot's Roblox account join a group |
| `/whoami` | Which Roblox account the bot runs on |

If the bot's account isn't in a group it needs, the message switches to "Joining group" and the bot joins
by itself. For `/check` it only joins when it can't get the audio otherwise, so it doesn't pile up groups.

## Setup (Windows)

1. `python -m venv venv` then `venv\Scripts\activate`
2. `pip install -r requirements.txt`
3. Create a bot at https://discord.com/developers/applications (no privileged intents).
   Invite with scopes `bot` + `applications.commands`.
4. Copy `.env.example` to `.env`, fill in the token, cookie, and your Discord user ID.
5. `python bot.py`

Cookie: logged into the Roblox alt in a browser, DevTools (F12) -> Application -> Cookies -> roblox.com ->
copy `.ROBLOSECURITY`. Don't log that account out afterwards, that kills the cookie.

## What auto-join can and can't do

- Public groups: joins straight away. Approval groups: sends a request, someone has to accept it.
- Roblox sometimes throws a captcha at joins. A bot can't solve it, so it tells you to join once by hand.
- Being in a group doesn't guarantee access to its audio, the role needs asset/creations permission.
- Joins are throttled to one per 30 seconds on purpose. Rapid joining gets accounts flagged.

## About the waveform

The bot downloads the audio file through Roblox's asset delivery and analyzes it locally. Roblox only serves
audio files to accounts that have permission, so audio the bot can't access still gets a card, just without
length-from-file, loudness stats, or the image. Loudness numbers are peak and average (RMS) in dBFS, not LUFS.

## Deploying on Render

Use a **Background Worker** (not a Web Service). Push this folder to a private GitHub repo, then
Render -> New -> Blueprint, which reads `render.yaml`. Fill in `DISCORD_TOKEN`, `ROBLOX_COOKIE`,
`ALLOWED_USER_IDS` in the dashboard.

## Cookie safety

The cookie is full access to that account. Keep `.env` out of git, use an alt, never paste the cookie
anywhere, and log the account out everywhere if it ever leaks.
