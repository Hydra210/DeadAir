# DeadAir

Discord bot that checks Roblox group audio: is it working, is it moderated, who owns it, how long it is,
and what it sounds like (a black-and-white loudness waveform). No emojis, everything in embeds.

## Commands

Groups and games accept a **name, an ID, or a link**. Start typing a name and pick from the suggestions.
Anything that can make the bot's account join a group (`/join`, `/search`, `/check group`) never guesses: it
needs an exact name or an ID, and shows suggestions otherwise.

**Audio**

| Command | What it does |
|---|---|
| `/check ids:<one ID>` | Full card: status, length, group (link + ID), dates, loudness stats, waveform image |
| `/check ids:<several IDs>` | Quick list with name and working/moderated status per ID |
| `/search group term [exact] [only_moderated]` | Search a group's audio by part of a name, or exact name |

**Users** (username, ID, or profile link)

| Command | What it does |
|---|---|
| `/user` | Profile card: join date, friends/followers/following, groups, past usernames |
| `/avatar` | Current avatar |
| `/wearing` | Every item they have on right now, with prices and catalog links |
| `/followers [order]` | Everyone who follows them, newest or oldest first. Loads more as you page |
| `/following [order]` | Everyone they follow |
| `/friends` | Friend list |
| `/mutual user other` | Mutual friends and mutual groups between two users |
| `/usergroups` / `/membership user group` | All their groups, or their role in one group |
| `/userbadges` / `/usergames` / `/favorites` / `/names` | Badges earned, games created, favorite games, past usernames |

**Groups**

| Command | What it does |
|---|---|
| `/group` | Group card: owner, members, joining, shout, roles |
| `/members group [order]` | Member list with roles. Loads more as you page |
| `/groupgames group` | Games the group has made |

**Games and items**

| Command | What it does |
|---|---|
| `/game` | Playing now, visits, favorites, likes, creator, dates |
| `/gamepasses` / `/gamebadges` | A game's passes with prices, or its badges with award counts |
| `/asset` / `/badge` | Any asset or badge by ID |

**Bot:** `/join group`, `/whoami`, `/help`.

Lists of people (`/followers`, `/following`, `/members`) can be huge, so the first batch loads right away and the
bot fetches more from Roblox only when you page forward. A list that is private on Roblox shows a clear message
instead of an error.

Each person is limited to 8 commands per 30 seconds, since everyone shares one Roblox account and one rate limit.

If the bot's account isn't in a group it needs, the message switches to "Joining group" and the bot joins
by itself. For `/check` it only joins when it can't get the audio otherwise, so it doesn't pile up groups.

## Setup (Windows)

1. `python -m venv venv` then `venv\Scripts\activate`
2. `pip install -r requirements.txt`
3. Create a bot at https://discord.com/developers/applications (no privileged intents).
   Invite with scopes `bot` + `applications.commands`.
4. Copy `.env.example` to `.env` and fill in the token and cookie.
5. `python bot.py`

Cookie: logged into the Roblox alt in a browser, DevTools (F12) -> Application -> Cookies -> roblox.com ->
copy `.ROBLOSECURITY`. Don't log that account out afterwards, that kills the cookie.

## Why `/search` can come back empty

`/search` does exactly what the extension does: list the group's audio through Creator Dashboard's
`creations` API, then match the name (exact: the name equals what you typed, otherwise: the name contains it,
both ignoring case). The difference is *whose account asks*. The extension runs as your own account, which has
a role with item permissions. The bot runs as its own account, and a plain Member role gets an empty list from
Roblox, not an error. When that happens the bot now says so, with the account's role and rank.

To fix it, give the bot's account a role that can manage group items in that group, or run the bot with the
cookie of an account that already has one. Quick test: put your own account's cookie in `.env` locally and run
the search again. If it finds the audio, it was the permissions.

## What auto-join can and can't do

- Public groups: joins straight away. Approval groups: sends a request, someone has to accept it.
- Roblox sometimes throws a captcha at joins. A bot can't solve it, so it tells you to join once by hand.
- Being in a group doesn't guarantee access to its audio, the role needs asset/creations permission.
- Joins are throttled to one per 30 seconds on purpose. Rapid joining gets accounts flagged.

## About the waveform

The bot downloads the audio file through Roblox's asset delivery and analyzes it locally. Roblox only serves
audio files to accounts that have permission, so audio the bot can't access still gets a card, just without
length-from-file, loudness stats, or the image. Loudness numbers are peak and average (RMS) in dBFS, not LUFS.

## Deploying on Render (free)

Background Workers cost money, so this runs as a free **Web Service**. `bot.py` serves `/health` on the
port Render provides. Push this folder to a private GitHub repo, then Render -> New -> Web Service
(or Blueprint, which reads `render.yaml`), and fill in `DISCORD_TOKEN` and `ROBLOX_COOKIE`.

Free web services spin down after 15 minutes without inbound traffic, which would take the bot offline.
Add a free monitor (UptimeRobot, 5 minute interval) that requests `https://<your-service>.onrender.com/health`.

## Cookie safety

The cookie is full access to that account. Keep `.env` out of git, use an alt, never paste the cookie
anywhere, and log the account out everywhere if it ever leaks.
