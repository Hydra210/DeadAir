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
| `/search group term [exact] [only_moderated] [amount] [refresh] [export]` | Search a group's audio by part of a name, or exact name. `amount` is `all` (default) or a number like `5000` or `5k`. `refresh` forces a rescan. `export` true also attaches every result as a CSV (id, name, status, link), zipped automatically if it's big |
| `/audiograb asset_id` | Downloads the audio file itself, if the bot's account is allowed to get it |

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


**More Roblox**

| Command | What it does |
|---|---|
| `/bulkids users [export]` | Paste many user IDs and/or usernames (spaces, commas or new lines, profile links work). Returns username, display name and ID for each, up to 500, paged. `export` true adds a CSV |
| `/limited item` | RAP, value, demand, trend and flags from Rolimons plus Roblox's own resale data. Name, acronym, link or ID |
| `/usernamecheck` | Free, taken, or not allowed |
| `/compare kind first second` | Two users or two groups side by side |
| `/servers game` | Players online and how full the servers are (samples the 100 fullest) |
| `/thumbnail kind target` | Full-size image of an asset, game, group, avatar or headshot |
| `/robux amount` | Sale cut, DevEx payout and buy cost. Rates are in `ROBUX` at the top of that section of `bot.py`, or `DEVEX_RATE` / `ROBUX_BUY_RATE` env vars |

**Alerts** (sent by DM, with the channel as a fallback)

| Command | What it does |
|---|---|
| `/watch audio` / `/watch group` | DM when an audio turns moderated or works again, or when a group's member count moves by `min_change`. Checked every 10 minutes |
| `/watch list` / `/watch stop` | See and remove what you watch. 15 per person |
| `/remind add when message [timezone]` | `in 2h30m`, `6:30pm`, `2026-12-25 09:00`. Also `/remind list` and `/remind cancel` |

Alerts and reminders live in a SQLite file (`DATA_DIR`, default `./data`). Render's free plan wipes the disk on
every restart or redeploy, so on Render they are lost until you attach a persistent disk and point `DATA_DIR` at it.
Alerts arrive as a DM from the bot, so test with `/remind add when:in 1m message:test` first.

**Convert** (attach a file to the command, up to 25 MB)

| Command | What it does |
|---|---|
| `/convert file format` | Image to png, jpg, webp, gif, bmp, ico or tiff. Animated gif/webp keep their frames when going to gif or webp |
| `/resize file [width] [height] [percent]` | Resize an image, keeps the aspect ratio if you give only one side |
| `/compress file [target_mb]` | Shrinks an image until it fits the size you want |
| `/gifmaker image1 ... image6 [delay] [loop]` | Turns 2 to 6 images into an animated GIF |
| `/filter file effect [strength]` | grayscale, invert, sepia, blur, sharpen, pixelate, mirror, flip, rotate. GIFs keep animating |
| `/caption file text` | Meme text. `top \| bottom` puts text on both |
| `/gifframes file [mode] [columns]` | Splits a GIF into one sprite sheet or a zip of frames. `/spritesheet` packs 2 to 6 images into a sheet |
| `/ocr file [language]` | Reads text from an image (needs `OCR_SPACE_KEY`) |
| `/audioconvert file format` | Audio between mp3, wav, flac and ogg |
| `/trim file [start] [end]` | Cuts audio, like `0:12.5` to `1:03` |
| `/waveform file` | Length, peak, average loudness and the same black-and-white waveform `/check` makes, for any audio file |

**Socials and lookups**

| Command | What it does |
|---|---|
| `/youtube video` | Title, channel and thumbnail from a link. With `YOUTUBE_API_KEY` set it also shows views, likes, length, date, and accepts search words |
| `/tiktok link` | Title, creator and thumbnail |
| `/tweet link` | Post text, author, likes, reposts, views and the first image |
| `/embedfix link` | Rewrites TikTok, Instagram, X and Reddit links to a host that makes Discord show the real embed |
| `/spotify link` / `/soundcloud link` | Track, album, artist or playlist info. Spotify gets richer with `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` |
| `/unshorten link` | Follows a short link and shows every redirect. Refuses private and local addresses |
| `/robloxstatus` | Whether Roblox is up, with any open incidents |
| `/github target` | A user or `owner/repo`. `GITHUB_TOKEN` (optional) lifts the 60 per hour limit |
| `/weather` `/define` `/qr` `/color` | Weather for a city, dictionary definition, QR code, color swatch |
| `/timestamp when [timezone]` | Discord `<t:...>` codes in every style, ready to copy |
| `/currency` / `/crypto` | Money conversion (ECB rates) and coin prices (CoinGecko) |
| `/hash` / `/base64` | Hashes of text or a file, Base64 encode and decode |

Instagram has no public lookup that works without a login, so `/embedfix` is how Instagram links are handled.
Converters, lookups and utilities live in `toolkit.py` (add a command there and put it in `COMMANDS`). Reminders and
watches live in `alerts.py`. Roblox commands stay in `bot.py`.

Optional environment variables: `YOUTUBE_API_KEY`, `GITHUB_TOKEN`, `OCR_SPACE_KEY`, `SPOTIFY_CLIENT_ID`,
`SPOTIFY_CLIENT_SECRET`, `DATA_DIR`, `DEVEX_RATE`, `ROBUX_BUY_RATE`.

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

## How much `/search` scans

Roblox hands out a group's audio 100 at a time, so a group with 20,000 audios means 200 requests. By default
`/search` scans **all** of them and shows progress while it works. Use `amount` to cap it (`1000`, `5k`, ...)
when you only care about the first part of the list. The result always says how many audios were scanned, and if
it stopped before the end of the group it tells you so and suggests raising `amount`. The hard ceiling for one
search is 100,000 audios. An older version silently stopped at 2,500, which hid results in big groups.

## Status and profile

The bot rotates its status every 10 seconds between two messages. Edit `STATUS_MESSAGES` and `STATUS_SECONDS`
near the top of `bot.py` to change the text or timing.

For the profile, Discord's API lets a bot change its **username, avatar and banner**. I found no setting for a
gradient "profile theme" on bot accounts, so the black-and-white gradient is baked into the banner image. Two are
in `assets/`: `deadair_banner_gradient.png` (black to white fade) and `deadair_banner_glow.png` (dark with a white
glow). Upload one on the Bot page of the Developer Portal if it offers a banner field, or run:

    python set_profile.py --banner assets/deadair_banner_gradient.png
    python set_profile.py --avatar halo_pfp_big.png        (optional)
    python set_profile.py --banner assets/deadair_banner_glow.png --dry-run     (checks the file, sends nothing)

Profile changes are rate limited, so don't spam it.

## When Roblox times out (504)

Deep in a big group Roblox's servers sometimes time out. The bot retries those errors, and if a page keeps
failing it asks for smaller pages. If Roblox still won't answer, you get results for everything loaded so far with
a warning, instead of losing the whole scan. A scan is kept for 5 minutes, so searching again picks up where it
stopped, and searching a second term in the same group doesn't rescan. Use `refresh` on `/search` to force a new
scan (for example right after uploading new audio).

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

## Additional commands

- `/dmca asset_id owner work claim contact` creates a private copyright takedown **draft** as a text file. Review and sign it before submitting it yourself; the bot does not send legal notices.
- `/normalize file target_lufs` applies FFmpeg loudness normalization (default -16 LUFS).
- `/speed file multiplier` changes speed while approximately preserving pitch.
- `/pitch file semitones` shifts pitch while approximately preserving duration.
- `/silence file threshold_db` trims quiet starts/ends without removing interior pauses.
- `/palette file colors` returns dominant HEX colors and a swatch image.
- `/imageinfo file strip` displays dimensions, format and basic EXIF, or creates a metadata-free PNG when `strip=true`.
- `/emoji file` creates a transparent 128×128 PNG.

**Deployment:** Audio commands require the `ffmpeg` executable installed on the host and accessible on PATH. On Render, use a runtime/container with FFmpeg available. Input attachments are capped at 20 MB. Audio outputs are WAV; very long files may exceed Discord's upload limits. Commands sync globally on startup and may take time to appear.
