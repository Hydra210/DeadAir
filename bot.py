# DeadAir — bot.py
# Discord bot for checking Roblox group audio: availability, moderation, info and loudness.
# Credits: @Nexesmere / EXE Development
#
# Commands are user-installable and work anywhere (servers, DMs, group DMs).
# Requires discord.py >= 2.4

import asyncio
import io
import logging
import os
import re
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import audio
from roblox import (AuthError, ChallengeRequired, GroupInfo, RobloxClient,
                    RobloxError, status_to_working)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("deadair")


# ================================================================
# CONFIG
# ================================================================

CFG = {
    "Token": os.getenv("DISCORD_TOKEN", ""),
    "Cookie": os.getenv("ROBLOX_COOKIE", ""),
    "AutoJoin": os.getenv("AUTO_JOIN", "true").lower() in ("1", "true", "yes", "on"),
    # Optional. User-installable commands must be synced globally, so this is no longer used
    # for syncing. If set, the bot clears any old guild-only copies of the commands from that
    # server on startup so you don't see duplicates.
    "DevGuildId": os.getenv("GUILD_ID", ""),
    "MaxIds": 500,
    "PerPage": 12,
    "Footer": "DeadAir  |  EXE Development",
}

COLORS = {"base": 0xF2F2F2, "bad": 0xE5484D}
LABELS = {"working": "Working", "broken": "Moderated", "unknown": "Unknown"}
AUDIO_TYPE_ID = 3


# ================================================================
# SMALL HELPERS
# ================================================================

def group_url(gid: int) -> str:
    return f"https://www.roblox.com/communities/{gid}"


def asset_url(aid) -> str:
    return f"https://create.roblox.com/store/asset/{aid}"


def link_text(text: Optional[str], limit: int = 48) -> str:
    t = (text or "(unknown name)").replace("[", "(").replace("]", ")").replace("`", "'")
    t = " ".join(t.split())
    return t if len(t) <= limit else t[: limit - 3] + "..."


def fmt_duration(seconds: float) -> str:
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_ts(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        clean = re.sub(r"\.\d+", "", value).replace("Z", "+00:00")
        return int(datetime.fromisoformat(clean).astimezone(timezone.utc).timestamp())
    except ValueError:
        return None


def make_embed(title: Optional[str] = None, description: Optional[str] = None,
               color: int = COLORS["base"]) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color)
    e.set_footer(text=CFG["Footer"])
    return e


def error_embed(message: str, title: str = "Request failed") -> discord.Embed:
    return make_embed(title, message, COLORS["bad"])


async def show(interaction: discord.Interaction, embed: discord.Embed, *,
               view: Optional[discord.ui.View] = None, file: Optional[discord.File] = None):
    """Edits the single deferred response, so one message morphs: working -> joining -> result."""
    kwargs = {"embed": embed, "attachments": [file] if file else []}
    if view is not None:
        kwargs["view"] = view
    return await interaction.edit_original_response(**kwargs)


# ================================================================
# BOT SETUP
# ================================================================

# Status messages the bot rotates through (each one shows for STATUS_SECONDS, then it loops).
STATUS_MESSAGES = [
    "All your wishes in one bot | DeadAir 2026 \U0001F499",
    "/help | see it for yourself",
]
STATUS_SECONDS = 10


class DeadAirTree(app_commands.CommandTree):
    """Commands are open to anyone, so cap how fast one person can burn the shared Roblox account."""
    LIMIT, WINDOW = 8, 30.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._hits: dict[int, deque] = {}

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.type is not discord.InteractionType.application_command:
            return True  # autocomplete etc. are not counted
        now = time.monotonic()
        q = self._hits.setdefault(interaction.user.id, deque())
        while q and now - q[0] > self.WINDOW:
            q.popleft()
        if len(q) >= self.LIMIT:
            wait = int(self.WINDOW - (now - q[0])) + 1
            await interaction.response.send_message(
                embed=error_embed(f"You're sending commands too fast. Try again in {wait}s.", "Slow down"),
                ephemeral=True)
            return False
        q.append(now)
        return True


class DeadAir(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        # Every command can be installed to a user's account or a server, and run in
        # servers, bot DMs, and group DMs / DMs with other users.
        self.tree = DeadAirTree(
            self,
            allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
            allowed_contexts=app_commands.AppCommandContext(
                guild=True, dm_channel=True, private_channel=True),
        )
        self.rbx: Optional[RobloxClient] = None
        self.health: Optional[web.AppRunner] = None
        self._status_i = 0

    async def start_health_server(self):
        """Tiny HTTP endpoint so Render's free Web Service has a port to check and an uptime pinger can hit it."""
        port = os.getenv("PORT")
        if not port:
            return
        app = web.Application()

        async def ok(_request):
            return web.Response(text="ok")

        app.router.add_get("/", ok)
        app.router.add_get("/health", ok)
        self.health = web.AppRunner(app)
        await self.health.setup()
        await web.TCPSite(self.health, "0.0.0.0", int(port)).start()
        log.info("Health server listening on port %s", port)

    @tasks.loop(seconds=STATUS_SECONDS)
    async def status_loop(self):
        text = STATUS_MESSAGES[self._status_i % len(STATUS_MESSAGES)]
        self._status_i += 1
        try:
            await self.change_presence(status=discord.Status.online,
                                       activity=discord.CustomActivity(name=text))
        except Exception:
            log.exception("Couldn't update the status")  # never let this stop the loop

    @status_loop.before_loop
    async def _before_status_loop(self):
        await self.wait_until_ready()

    async def setup_hook(self):
        await self.start_health_server()
        self.rbx = RobloxClient(CFG["Cookie"])
        await self.rbx.start()  # raises if the cookie is dead
        log.info("Roblox account: %s (%s)", self.rbx.user["name"], self.rbx.user["id"])

        # Clear any leftover guild-only commands from the old dev-guild sync (avoids duplicates).
        if CFG["DevGuildId"]:
            guild = discord.Object(id=int(CFG["DevGuildId"]))
            self.tree.clear_commands(guild=guild)
            await self.tree.sync(guild=guild)

        # Global sync: required for user-installable commands.
        await self.tree.sync()
        log.info("Commands synced globally")

        if not self.status_loop.is_running():
            self.status_loop.start()

    async def close(self):
        self.status_loop.cancel()
        if self.rbx:
            await self.rbx.close()
        if self.health:
            await self.health.cleanup()
        await super().close()


bot = DeadAir()


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
    if isinstance(error, app_commands.CheckFailure):
        return  # the rate limiter already replied
    if isinstance(original, AuthError):
        embed = error_embed("The bot's Roblox cookie is dead. Replace `ROBLOX_COOKIE` and restart.", "Roblox login failed")
    elif isinstance(original, RobloxError):
        embed = error_embed(str(original), "Roblox rejected the request")
    else:
        log.exception("Unhandled command error", exc_info=error)
        embed = error_embed(f"`{original}`", "Unexpected error")

    if interaction.response.is_done():
        try:
            await interaction.edit_original_response(embed=embed, attachments=[], view=None)
        except discord.HTTPException:
            await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ================================================================
# GROUP ACCESS (auto-join)
# ================================================================

def parse_id(raw: str, what: str = "ID") -> int:
    m = re.search(r"\d+", raw or "")
    if not m:
        raise RobloxError(f"That doesn't look like a valid {what}.")
    return int(m.group())


def access_problem(info: GroupInfo, state: str) -> Optional[str]:
    link = f"[{link_text(info.name)}]({group_url(info.id)})"
    return {
        "disabled": f"The account isn't in {link} and auto-join is turned off.",
        "locked": f"{link} is locked, so the account can't join it.",
        "challenge": (f"Roblox asked for a captcha when joining {link}, and the bot can't solve that. "
                      "Some information will not be viewable."),
        "pending": (f"{link} needs approval. A join request was sent. "
                    "Have someone accept it, then run this again."),
    }.get(state)


async def try_access(interaction: discord.Interaction, gid: int) -> tuple[GroupInfo, str]:
    """state: member | joined | pending | disabled | locked | challenge. Announces the join in the message."""
    rbx = bot.rbx
    info = await rbx.group_info(gid)
    if await rbx.in_group(gid):
        return info, "member"
    if not CFG["AutoJoin"]:
        return info, "disabled"
    if info.locked:
        return info, "locked"

    await show(interaction, make_embed(
        "Joining group",
        f"The account isn't in [{link_text(info.name)}]({group_url(gid)}) (`{gid}`), "
        "so it's joining automatically.\nJoins are throttled, so this can take a moment.",
    ))
    try:
        outcome = await rbx.join_group(gid)
    except ChallengeRequired:
        return info, "challenge"
    return info, ("joined" if outcome in ("joined", "already") else "pending")


async def ensure_group_access(interaction: discord.Interaction, gid: int) -> Optional[GroupInfo]:
    """Returns GroupInfo if the account can use the group, otherwise shows why and returns None."""
    info, state = await try_access(interaction, gid)
    if state in ("member", "joined"):
        return info
    await show(interaction, error_embed(access_problem(info, state), "No access to group"))
    return None


# ================================================================
# PAGINATED RESULTS
# ================================================================

class Pager(discord.ui.View):
    def __init__(self, owner_id: int, title: str, header: str, lines: list[str],
                 footer: str, color: int, thumbnail: Optional[str] = None):
        super().__init__(timeout=300)
        per = CFG["PerPage"]
        self.pages = [lines[i:i + per] for i in range(0, len(lines), per)] or [[]]
        self.owner_id, self.title, self.header = owner_id, title, header
        self.footer, self.color, self.thumbnail = footer, color, thumbnail
        self.index = 0
        self.message: Optional[discord.Message] = None
        self._sync()

    def embed(self) -> discord.Embed:
        body = (self.header + "\n\n" if self.header else "") + "\n".join(self.pages[self.index])
        e = discord.Embed(title=self.title, description=body, color=self.color)
        e.set_footer(text=f"Page {self.index + 1} of {len(self.pages)}  |  {self.footer}  |  DeadAir")
        if self.thumbnail:
            e.set_thumbnail(url=self.thumbnail)
        return e

    def _sync(self):
        self.prev_btn.disabled = self.index == 0
        self.next_btn.disabled = self.index >= len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                embed=error_embed("These results belong to someone else.", "Not yours"), ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = max(0, self.index - 1)
        self._sync()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = min(len(self.pages) - 1, self.index + 1)
        self._sync()
        await interaction.response.edit_message(embed=self.embed(), view=self)


def fmt_row(row: dict) -> str:
    return (f"**{LABELS[row['status']]}**  [{link_text(row['name'])}]({asset_url(row['id'])})  "
            f"`{row['id']}`")


async def send_results(interaction: discord.Interaction, title: str, header: str, rows: list[dict],
                       only_moderated: bool, thumbnail: Optional[str] = None):
    counts = {k: sum(1 for r in rows if r["status"] == k) for k in LABELS}
    summary = f"{counts['working']} working, {counts['broken']} moderated, {counts['unknown']} unknown"
    shown = [r for r in rows if r["status"] == "broken"] if only_moderated else rows

    if not shown:
        await show(interaction, make_embed(
            title, (header + "\n\n" if header else "") +
            ("Nothing moderated in these results." if only_moderated else "No results.")))
        return

    color = COLORS["bad"] if counts["broken"] else COLORS["base"]
    pager = Pager(interaction.user.id, title, header + "\n" + summary if header else summary,
                  [fmt_row(r) for r in shown], f"{len(shown)} shown", color, thumbnail)
    pager.message = await show(interaction, pager.embed(), view=pager)


# ================================================================
# SINGLE AUDIO CARD
# ================================================================

def build_card(asset_id: int, econ: dict, status: str, stats: Optional[dict], toolbox: dict,
               group: Optional[GroupInfo], icon: Optional[str], note: Optional[str],
               has_wave: bool) -> discord.Embed:
    name = econ.get("Name") or f"Audio {asset_id}"
    desc = " ".join((econ.get("Description") or "").split())
    if len(desc) > 220:
        desc = desc[:217] + "..."

    color = COLORS["bad"] if status == "broken" else COLORS["base"]
    e = discord.Embed(title=link_text(name, 200), url=asset_url(asset_id),
                      description=desc or None, color=color)

    duration = stats["duration"] if stats else None
    if duration is None and toolbox.get("duration"):
        try:
            duration = float(toolbox["duration"])
        except (TypeError, ValueError):
            duration = None

    e.add_field(name="Status", value=LABELS[status], inline=True)
    e.add_field(name="Length", value=fmt_duration(duration) if duration is not None else "Unknown", inline=True)
    e.add_field(name="Asset ID", value=f"`{asset_id}`", inline=True)

    creator = econ.get("Creator") or {}
    if group:
        e.add_field(name="Group", value=f"[{link_text(group.name, 40)}]({group_url(group.id)})", inline=True)
        e.add_field(name="Group ID", value=f"`{group.id}`", inline=True)
        e.add_field(name="Members", value=f"{group.member_count:,}" if group.member_count else "Unknown", inline=True)
    elif creator:
        uid = creator.get("Id") or creator.get("CreatorTargetId")
        who = link_text(creator.get("Name"), 40)
        e.add_field(name="Creator", value=f"[{who}](https://www.roblox.com/users/{uid}/profile)" if uid else who, inline=True)
        e.add_field(name="Creator type", value=str(creator.get("CreatorType") or "User"), inline=True)
        e.add_field(name="Creator ID", value=f"`{uid}`" if uid else "Unknown", inline=True)

    created, updated = parse_ts(econ.get("Created")), parse_ts(econ.get("Updated"))
    e.add_field(name="Created", value=f"<t:{created}:D>" if created else "Unknown", inline=True)
    e.add_field(name="Updated", value=f"<t:{updated}:R>" if updated else "Unknown", inline=True)
    e.add_field(name="Free to use", value="Yes" if econ.get("IsPublicDomain") else "No", inline=True)

    extras = [(k.title(), str(toolbox[k])) for k in ("artist", "album", "genre") if toolbox.get(k)]
    for label, value in extras[:3]:
        e.add_field(name=label, value=link_text(value, 40), inline=True)

    if stats:
        e.add_field(
            name="Loudness",
            value=(f"Peak `{stats['peak_db']:.1f} dBFS`   Average `{stats['rms_db']:.1f} dBFS`   "
                   f"Range `{stats['range_db']:.1f} dB`"),
            inline=False,
        )
        channels = {1: "Mono", 2: "Stereo"}.get(stats["channels"], f"{stats['channels']} channels")
        e.add_field(
            name="File",
            value=(f"{stats['format']}  |  {stats['sample_rate'] / 1000:g} kHz  |  {channels}  |  "
                   f"{stats['size_bytes'] / 1_000_000:.2f} MB  |  about {stats['bitrate_kbps']:.0f} kbps"),
            inline=False,
        )

    if note:
        e.add_field(name="Note", value=note, inline=False)
    if has_wave:
        e.set_image(url="attachment://waveform.png")
    if icon:
        e.set_thumbnail(url=icon)
    e.set_footer(text=CFG["Footer"])
    return e


async def probe_asset(asset_id: int):
    """Moderation info + the audio file. Either can be missing if the account lacks access."""
    rbx = bot.rbx
    details = await rbx.asset_details([asset_id])
    dev = details.get(str(asset_id))
    data, err = None, None
    if not (dev and dev.get("isModerated")):
        try:
            data = await rbx.download_audio(asset_id)
        except (AuthError, ChallengeRequired):
            raise
        except RobloxError as e:
            err = str(e)
    return dev, data, err


async def check_single(interaction: discord.Interaction, asset_id: int):
    rbx = bot.rbx
    econ = await rbx.economy_details(asset_id)
    if not econ:
        await show(interaction, error_embed(f"No asset with the ID `{asset_id}` was found.", "Asset not found"))
        return
    if econ.get("AssetTypeId") != AUDIO_TYPE_ID:
        await show(interaction, error_embed(
            f"`{asset_id}` is [{link_text(econ.get('Name'))}]({asset_url(asset_id)}), and it isn't an audio asset.",
            "Not an audio asset"))
        return

    creator = econ.get("Creator") or {}
    gid = None
    if creator.get("CreatorType") == "Group":
        gid = int(creator.get("CreatorTargetId") or creator.get("Id") or 0) or None

    dev, data, dl_err = await probe_asset(asset_id)
    note = None
    group: Optional[GroupInfo] = None

    moderated = bool(dev and dev.get("isModerated"))
    if gid:
        needs_access = (dev is None or data is None) and not moderated
        if needs_access:
            group, state = await try_access(interaction, gid)
            if state in ("member", "joined"):
                if state == "joined":
                    note = f"The account joined [{link_text(group.name)}]({group_url(gid)}) automatically for this check."
                dev, data, dl_err = await probe_asset(asset_id)
            else:
                note = access_problem(group, state)
        else:
            group = await rbx.group_info(gid)

    status = status_to_working(dev) if dev else ("working" if data else "unknown")

    stats, wave = None, None
    if data:
        try:
            stats = await asyncio.to_thread(audio.analyze, data)
            png = await asyncio.to_thread(audio.render_waveform, stats)
            wave = discord.File(io.BytesIO(png), filename="waveform.png")
        except Exception:
            log.exception("Audio analysis failed for %s", asset_id)
            note = (note + "\n" if note else "") + "The file downloaded, but it couldn't be decoded for a waveform."
    elif status != "broken":
        why = "Roblox wouldn't serve the file to this account. It may be moderated, deleted, or private."
        if dl_err:
            why += f"\nReason from Roblox: `{dl_err[:200]}`"
        note = (note + "\n" if note else "") + why
    if status == "broken":
        note = (note + "\n" if note else "") + "Roblox flagged this audio as moderated."

    toolbox = await rbx.toolbox_audio_info(asset_id)
    icon = await rbx.group_icon(gid) if gid else None
    embed = build_card(asset_id, econ, status, stats, toolbox, group, icon, note, wave is not None)
    await show(interaction, embed, file=wave)


# ================================================================
# COMMANDS
# ================================================================

@bot.tree.command(name="check", description="Check if a Roblox audio works, plus its info and loudness")
@app_commands.describe(
    ids="One audio ID for the full card, or several IDs for a quick list",
    group="Optional: a group (name or ID) to make sure the bot's account is in first",
    only_moderated="For several IDs: only show the moderated ones",
)
async def check(interaction: discord.Interaction, ids: str, group: Optional[str] = None,
                only_moderated: bool = False):
    await interaction.response.defer(thinking=True)

    seen, clean = set(), []
    for raw in re.findall(r"\d+", ids):
        n = int(raw)
        if n > 0 and n not in seen:
            seen.add(n)
            clean.append(n)
    if not clean:
        await show(interaction, error_embed("No audio IDs found in that.", "Nothing to check"))
        return
    if len(clean) > CFG["MaxIds"]:
        await show(interaction, error_embed(f"The limit is {CFG['MaxIds']} IDs at once, and you sent {len(clean)}.", "Too many IDs"))
        return

    if group:
        gid = await pick_group(interaction, group, strict=True)
        if not gid or not await ensure_group_access(interaction, gid):
            return

    if len(clean) == 1:
        await check_single(interaction, clean[0])
        return

    details = await bot.rbx.asset_details(clean)
    sem = asyncio.Semaphore(5)

    async def name_for(aid: int):
        async with sem:
            return await bot.rbx.asset_name_fallback(aid)

    missing = [i for i in clean if str(i) not in details][:50]
    fallback = dict(zip(missing, await asyncio.gather(*(name_for(i) for i in missing))))

    rows = []
    for aid in clean:
        d = details.get(str(aid))
        rows.append({"id": aid, "name": d.get("name") if d else fallback.get(aid),
                     "status": status_to_working(d) if d else "unknown"})

    await send_results(interaction, "Audio check", f"{len(clean)} audio IDs checked. "
                       "Run `/check` with a single ID for the full card.", rows, only_moderated)


AMOUNT_SUGGESTIONS = [("All audios", "all"), ("First 500", "500"), ("First 1,000", "1000"),
                      ("First 2,500", "2500"), ("First 5,000", "5000"), ("First 10,000", "10000"),
                      ("First 25,000", "25000"), ("First 50,000", "50000")]


def parse_amount(raw: Optional[str]) -> Optional[int]:
    """None means all. Accepts all, 5000, 5,000, 5k, 2.5k."""
    t = (raw or "all").strip().lower().replace(",", "").replace(" ", "")
    if t in ("all", "everything", "max", "every", "*", ""):
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(k?)", t)
    if not m:
        raise RobloxError("Amount must be `all` or a number like `5000` or `5k`.")
    n = int(float(m.group(1)) * (1000 if m.group(2) else 1))
    if n < 1:
        raise RobloxError("Amount must be at least 1.")
    return n


def ago(seconds: float) -> str:
    s = int(seconds)
    return f"{s}s ago" if s < 90 else f"{s // 60} min ago"


def amount_label(limit: Optional[int]) -> str:
    return "all audios" if limit is None else f"the first {num(limit)} audios"


async def amount_autocomplete(interaction: discord.Interaction, current: str):
    cur = (current or "").strip().lower()
    out = [app_commands.Choice(name=n, value=v) for n, v in AMOUNT_SUGGESTIONS
           if not cur or cur in n.lower() or cur in v]
    if cur and not any(c.value == cur for c in out):
        try:
            parse_amount(cur)
            out.insert(0, app_commands.Choice(name=f"Use {cur}", value=cur))
        except RobloxError:
            pass
    return out[:25]


async def explain_no_match(gid: int, everything: list) -> str:
    """Says WHY a search came back empty, using what Roblox actually returned to the bot."""
    rbx = bot.rbx
    try:
        role = await rbx.group_role(gid)
    except RobloxError:
        role = None
    meta = getattr(rbx, "last_meta", {})
    out = ""
    if role:
        out += f"\nThe bot's account is in this group as **{link_text(role['name'], 40)}** (rank {role['rank']})."
    if not everything:
        out += (f"\n\n**Roblox answered the audio request with 0 entries** ({meta.get('pages', 0)} page, "
                f"{meta.get('raw', 0)} raw). That is what it does when the account's role can't see the group's "
                "creations: an empty list, not an error.\n"
                "The extension finds audios because *your* account has a role with item permissions. "
                "The bot only sees what its own account is allowed to see, so a plain Member role gets nothing.\n"
                "**Fix:** give the bot's account a role that can manage group items, or run the bot with the "
                "cookie of an account that already has one.")
    else:
        sample = ", ".join(f"`{link_text(it.get('name'), 30)}`" for it in everything[:5])
        out += f"\nFirst names the bot saw: {sample}"
    if meta.get("incomplete"):
        out += (f"\n\n**Roblox errored out partway through the scan** ({meta.get('error')}), so only the first "
                f"{num(len(everything))} audios were searched. Run it again within 5 minutes and it picks up "
                "where it stopped instead of starting over.")
    elif meta.get("truncated"):
        if meta.get("hit_ceiling"):
            out += (f"\n\n**The scan stopped at the {num(meta.get('limit'))} audio ceiling** and the group has more. "
                    "That's the most the bot will scan in one search.")
        else:
            out += (f"\n\n**The scan stopped at your limit of {num(meta.get('limit'))} audios and the group has more.** "
                    "Run it again with `amount` set to a bigger number, or `all`.")
    skipped = getattr(rbx, "last_skipped", 0)
    if skipped:
        out += f"\n{skipped} entries were skipped because Roblox hasn't given them an asset ID yet (still processing)."
    return out


@bot.tree.command(name="search", description="Search a group's audio library by name or keyword")
@app_commands.describe(
    group="Group name or ID (a group URL works too)",
    term="Part of an audio name, or the exact name with exact on",
    exact="Match the whole name exactly instead of a part of it",
    only_moderated="Only show the moderated ones",
    amount="How many audios to scan: all (default), or a number like 5000 or 5k",
    refresh="Rescan from scratch instead of reusing a scan from the last 5 minutes",
)
async def search(interaction: discord.Interaction, group: str, term: str, exact: bool = False,
                 only_moderated: bool = False, amount: Optional[str] = None, refresh: bool = False):
    await interaction.response.defer(thinking=True)
    term = term.strip()
    if not term:
        await show(interaction, error_embed("Give me something to search for.", "Empty search"))
        return
    limit = parse_amount(amount)  # None = all

    gid = await pick_group(interaction, group, strict=True)
    if not gid:
        return
    info = await ensure_group_access(interaction, gid)
    if not info:
        return

    link = f"[{link_text(info.name)}]({group_url(gid)})"
    last_edit = [0.0]

    async def progress(count: int):
        now = time.monotonic()
        if now - last_edit[0] < 2.5:
            return
        last_edit[0] = now
        await show(interaction, make_embed(
            "Scanning group audio",
            f"Scanning {amount_label(limit)} in {link}.\n**{num(count)}** audios loaded so far..."))

    try:
        everything = await bot.rbx.list_group_audio(gid, max_items=limit, progress=progress, refresh=refresh)
    except RobloxError as e:
        if str(e).startswith("403"):
            await show(interaction, error_embed(
                f"The account is in [{link_text(info.name)}]({group_url(gid)}), but its role can't view the "
                "group's audio. It needs a role with asset or creations permissions.", "Missing permissions"))
            return
        raise

    # Same rule as the extension: exact -> name equals the term, otherwise -> name contains the term.
    low = term.lower()
    matched = [it for it in everything
               if ((it.get("name") or "").lower() == low if exact else low in (it.get("name") or "").lower())]

    if not matched:
        await show(interaction, make_embed(
            "No matches", f"Nothing matching `{link_text(term, 60)}` in {link} "
                          f"({len(everything)} audios searched)." + await explain_no_match(gid, everything)))
        return

    ids = [int(m["assetId"]) for m in matched]
    status_note = ""
    try:
        details = await bot.rbx.asset_details(ids)
    except (AuthError, ChallengeRequired):
        raise
    except RobloxError as e:
        details = {}
        status_note = f"\nThe matches were found, but Roblox wouldn't give their moderation status ({e})."
    rows = [{"id": int(m["assetId"]), "name": m.get("name"),
             "status": status_to_working(details.get(str(m["assetId"])))} for m in matched]
    icon = await bot.rbx.group_icon(gid)

    meta = getattr(bot.rbx, "last_meta", {})
    scanned = f"Scanned {num(len(everything))} audios"
    if meta.get("from_cache"):
        scanned += f" (reused a scan from {ago(meta.get('cached_age', 0))}, use `refresh` to rescan)"
    elif meta.get("resumed"):
        scanned += " (continued a recent scan)"
    if meta.get("incomplete"):
        scanned += (f". **Roblox errored out partway ({meta.get('error')}), so this only covers what was loaded.** "
                    "Run it again within 5 minutes and it picks up where it stopped")
    elif meta.get("truncated"):
        scanned += (f", then stopped at {'the ceiling' if meta.get('hit_ceiling') else 'your limit'}. "
                    "The group has more, so raise `amount` or use `all` to search the rest")
    header = (f"{len(matched)} of {len(everything)} audios in {link} (`{gid}`) match "
              f"`{link_text(term, 60)}`.\n{scanned}." + status_note)
    await send_results(interaction, "Audio search", header, rows, only_moderated, icon)


@bot.tree.command(name="join", description="Make the bot's Roblox account join a group")
@app_commands.describe(group="Group name or ID (a group URL works too)")
async def join(interaction: discord.Interaction, group: str):
    await interaction.response.defer(thinking=True)
    gid = await pick_group(interaction, group, strict=True)
    if not gid:
        return
    info, state = await try_access(interaction, gid)
    link = f"[{link_text(info.name)}]({group_url(gid)})"

    if state == "member":
        await show(interaction, make_embed("Already in group", f"The account is already in {link} (`{gid}`)."))
    elif state == "joined":
        await show(interaction, make_embed("Joined group", f"The account is now in {link} (`{gid}`)."))
    else:
        await show(interaction, error_embed(access_problem(info, state), "Couldn't join"))


@bot.tree.command(name="whoami", description="Show which Roblox account the bot is running on")
async def whoami(interaction: discord.Interaction):
    u = bot.rbx.user
    e = make_embed("Bot account", f"[{u['name']}](https://www.roblox.com/users/{u['id']}/profile)")
    e.add_field(name="User ID", value=f"`{u['id']}`", inline=True)
    e.add_field(name="Auto-join", value="On" if CFG["AutoJoin"] else "Off", inline=True)
    await interaction.response.send_message(embed=e, ephemeral=True)


# ================================================================
# LOOKUPS: users, groups, assets, games, badges
# ================================================================

ASSET_TYPES = {
    1: "Image", 2: "T-Shirt", 3: "Audio", 4: "Mesh", 5: "Lua script", 8: "Hat", 9: "Place", 10: "Model",
    11: "Shirt", 12: "Pants", 13: "Decal", 17: "Head", 18: "Face", 19: "Gear", 21: "Badge",
    24: "Animation", 27: "Torso", 28: "Right arm", 29: "Left arm", 30: "Left leg", 31: "Right leg",
    32: "Package", 34: "Game pass", 38: "Plugin", 40: "Mesh part", 41: "Hair accessory",
    42: "Face accessory", 43: "Neck accessory", 44: "Shoulder accessory", 45: "Front accessory",
    46: "Back accessory", 47: "Waist accessory", 48: "Climb animation", 49: "Death animation",
    50: "Fall animation", 51: "Idle animation", 52: "Jump animation", 53: "Run animation",
    54: "Swim animation", 55: "Walk animation", 56: "Pose animation", 59: "Ear accessory",
    60: "Eye accessory", 61: "Emote animation", 62: "Video", 63: "T-Shirt accessory",
    64: "Shirt accessory", 65: "Pants accessory", 66: "Jacket accessory", 67: "Sweater accessory",
    68: "Shorts accessory", 69: "Left shoe accessory", 70: "Right shoe accessory",
    71: "Dress/skirt accessory", 72: "Font family", 73: "Font face", 75: "Eyebrow accessory",
    76: "Eyelash accessory", 77: "Mood animation", 78: "Dynamic head",
}


def clip(text: Optional[str], limit: int) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[: limit - 3] + "..."


def num(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "Unknown"


def when(value: Optional[str], style: str = "D") -> str:
    ts = parse_ts(value)
    return f"<t:{ts}:{style}>" if ts else "Unknown"


def profile_url(uid) -> str:
    return f"https://www.roblox.com/users/{uid}/profile"


def creator_link(name: Optional[str], kind: Optional[str], cid) -> str:
    label = link_text(name, 40)
    if not cid:
        return label
    return f"[{label}]({group_url(cid) if kind == 'Group' else profile_url(cid)})"


def not_found(what: str, detail: str) -> discord.Embed:
    return error_embed(detail, f"{what} not found")


# ---------- users ----------

def build_user_card(u: dict, counts: dict, history: list, group_count: int,
                    headshot: Optional[str]) -> discord.Embed:
    uid = u.get("id")
    banned = bool(u.get("isBanned"))
    e = discord.Embed(
        title=link_text(u.get("displayName") or u.get("name"), 200), url=profile_url(uid),
        description=clip(u.get("description"), 300) or None,
        color=COLORS["bad"] if banned else COLORS["base"])
    status = "Banned" if banned else "Active"
    if u.get("hasVerifiedBadge"):
        status += ", verified"
    e.add_field(name="Username", value=f"@{link_text(u.get('name'), 30)}", inline=True)
    e.add_field(name="User ID", value=f"`{uid}`", inline=True)
    e.add_field(name="Status", value=status, inline=True)
    e.add_field(name="Joined", value=when(u.get("created")), inline=True)
    e.add_field(name="Friends", value=num(counts.get("friends")), inline=True)
    e.add_field(name="Followers", value=num(counts.get("followers")), inline=True)
    e.add_field(name="Following", value=num(counts.get("followings")), inline=True)
    e.add_field(name="Groups", value=num(group_count), inline=True)
    if history:
        e.add_field(name="Past usernames",
                    value=clip(", ".join(link_text(n, 24) for n in history[:5]), 200), inline=False)
    if headshot:
        e.set_thumbnail(url=headshot)
    e.set_footer(text=CFG["Footer"])
    return e


async def find_user(interaction: discord.Interaction, query: str) -> Optional[dict]:
    u = await bot.rbx.resolve_user(query)
    if not u:
        await show(interaction, not_found("User", f"I couldn't find a Roblox user for `{link_text(query, 60)}`."))
    return u


@bot.tree.command(name="user", description="Look up a Roblox user by username, ID, or profile link")
@app_commands.describe(user="Username, user ID, or a roblox.com profile link")
async def user_cmd(interaction: discord.Interaction, user: str):
    await interaction.response.defer(thinking=True)
    u = await find_user(interaction, user)
    if not u:
        return
    uid = int(u["id"])
    rbx = bot.rbx
    friends, followers, following, history, groups, head = await asyncio.gather(
        rbx.user_count(uid, "friends"), rbx.user_count(uid, "followers"), rbx.user_count(uid, "followings"),
        rbx.username_history(uid), rbx.user_groups(uid), rbx.user_headshot(uid))
    counts = {"friends": friends, "followers": followers, "followings": following}
    await show(interaction, build_user_card(u, counts, history, len(groups), head))


@bot.tree.command(name="avatar", description="Show a Roblox user's current avatar")
@app_commands.describe(user="Username, user ID, or a roblox.com profile link")
async def avatar_cmd(interaction: discord.Interaction, user: str):
    await interaction.response.defer(thinking=True)
    u = await find_user(interaction, user)
    if not u:
        return
    url = await bot.rbx.user_avatar(int(u["id"]))
    if not url:
        await show(interaction, error_embed(
            "Roblox hasn't finished rendering that avatar yet. Try again in a few seconds.", "Avatar not ready"))
        return
    e = make_embed(f"{link_text(u.get('displayName') or u.get('name'), 100)} (@{link_text(u.get('name'), 30)})")
    e.url = profile_url(u["id"])
    e.set_image(url=url)
    await show(interaction, e)


@bot.tree.command(name="usergroups", description="List the groups a Roblox user is in")
@app_commands.describe(user="Username, user ID, or a roblox.com profile link")
async def usergroups_cmd(interaction: discord.Interaction, user: str):
    await interaction.response.defer(thinking=True)
    u = await find_user(interaction, user)
    if not u:
        return
    uid = int(u["id"])
    groups, head = await asyncio.gather(bot.rbx.user_groups(uid), bot.rbx.user_headshot(uid))
    who = f"[{link_text(u.get('name'), 30)}]({profile_url(uid)})"
    if not groups:
        await show(interaction, make_embed("No groups", f"{who} isn't in any groups, or Roblox didn't return them."))
        return
    groups.sort(key=lambda g: ((g.get("role") or {}).get("rank") or 0, g["group"].get("memberCount") or 0),
                reverse=True)
    lines = [f"[{link_text(g['group'].get('name'))}]({group_url(g['group']['id'])})  `{g['group']['id']}`  "
             f"{link_text((g.get('role') or {}).get('name'), 24)}  |  {num(g['group'].get('memberCount'))} members"
             for g in groups]
    pager = Pager(interaction.user.id, "Groups", f"{who} is in {len(groups)} groups.", lines,
                  f"{len(lines)} groups", COLORS["base"], head)
    pager.message = await show(interaction, pager.embed(), view=pager)


# ---------- groups ----------

def build_group_card(d: dict, roles: list, icon: Optional[str]) -> discord.Embed:
    gid = d.get("id")
    e = discord.Embed(title=link_text(d.get("name"), 200), url=group_url(gid),
                      description=clip(d.get("description"), 300) or None, color=COLORS["base"])
    owner = d.get("owner") or {}
    owner_text = (f"[{link_text(owner.get('username'), 30)}]({profile_url(owner['userId'])})"
                  if owner.get("userId") else "No owner")
    entry = "Locked" if d.get("isLocked") else ("Open to join" if d.get("publicEntryAllowed") else "Approval required")
    e.add_field(name="Group ID", value=f"`{gid}`", inline=True)
    e.add_field(name="Members", value=num(d.get("memberCount")), inline=True)
    e.add_field(name="Owner", value=owner_text, inline=True)
    e.add_field(name="Joining", value=entry, inline=True)
    e.add_field(name="Verified", value="Yes" if d.get("hasVerifiedBadge") else "No", inline=True)

    shout = d.get("shout") or {}
    if shout.get("body"):
        e.add_field(name="Shout", value=clip(shout["body"], 200) + f"\n{when(shout.get('updated'), 'R')}", inline=False)

    if roles:
        ordered = sorted(roles, key=lambda r: r.get("rank") or 0, reverse=True)
        lines = [f"{link_text(r.get('name'), 30)}  (rank {r.get('rank')}, {num(r.get('memberCount'))} members)"
                 for r in ordered[:8]]
        if len(ordered) > 8:
            lines.append(f"...and {len(ordered) - 8} more roles")
        e.add_field(name=f"Roles ({len(ordered)})", value="\n".join(lines), inline=False)
    if icon:
        e.set_thumbnail(url=icon)
    e.set_footer(text=CFG["Footer"])
    return e


@bot.tree.command(name="group", description="Look up a Roblox group by name, ID, or link")
@app_commands.describe(group="Group name, ID, or a roblox.com group link")
async def group_cmd(interaction: discord.Interaction, group: str):
    await interaction.response.defer(thinking=True)
    gid = await pick_group(interaction, group, strict=False)
    if not gid:
        return
    d, roles, icon = await asyncio.gather(
        bot.rbx.group_details(gid), bot.rbx.group_roles(gid), bot.rbx.group_icon(gid))
    if not d:
        await show(interaction, not_found("Group", f"There's no group with the ID `{gid}`."))
        return
    await show(interaction, build_group_card(d, roles, icon))


# ---------- assets ----------

def build_asset_card(aid: int, econ: dict, dev: Optional[dict], thumb: Optional[str]) -> discord.Embed:
    type_id = econ.get("AssetTypeId")
    type_name = ASSET_TYPES.get(type_id, f"Type {type_id}")
    url = asset_url(aid) if type_id == 3 else f"https://www.roblox.com/catalog/{aid}"
    e = discord.Embed(title=link_text(econ.get("Name") or f"Asset {aid}", 200), url=url,
                      description=clip(econ.get("Description"), 300) or None, color=COLORS["base"])

    status = status_to_working(dev) if dev else None
    if status == "broken":
        e.color = COLORS["bad"]

    creator = econ.get("Creator") or {}
    cid = creator.get("CreatorTargetId") or creator.get("Id")
    if econ.get("IsForSale"):
        price = econ.get("PriceInRobux")
        price_text = "On sale" if price is None else ("Free" if price == 0 else f"R$ {num(price)}")
    else:
        price_text = "Not for sale"

    e.add_field(name="Type", value=type_name, inline=True)
    e.add_field(name="Asset ID", value=f"`{aid}`", inline=True)
    e.add_field(name="Creator", value=creator_link(creator.get("Name"), creator.get("CreatorType"), cid), inline=True)
    e.add_field(name="Created", value=when(econ.get("Created")), inline=True)
    e.add_field(name="Updated", value=when(econ.get("Updated"), "R"), inline=True)
    e.add_field(name="Price", value=price_text, inline=True)
    if econ.get("Sales"):
        e.add_field(name="Sales", value=num(econ["Sales"]), inline=True)
    if econ.get("IsLimitedUnique") or econ.get("IsLimited"):
        e.add_field(name="Limited", value="Limited U" if econ.get("IsLimitedUnique") else "Limited", inline=True)
    e.add_field(name="Free to use", value="Yes" if econ.get("IsPublicDomain") else "No", inline=True)
    if status:
        e.add_field(name="Moderation", value=LABELS[status], inline=True)
    if type_id == 3:
        e.add_field(name="Audio", value="Run `/check` with this ID for length, loudness and a waveform.", inline=False)
    if thumb:
        e.set_thumbnail(url=thumb)
    e.set_footer(text=CFG["Footer"])
    return e


@bot.tree.command(name="asset", description="Look up any Roblox asset (audio, model, shirt, decal, place, ...)")
@app_commands.describe(asset_id="Asset ID or a roblox.com / create.roblox.com link")
async def asset_cmd(interaction: discord.Interaction, asset_id: str):
    await interaction.response.defer(thinking=True)
    aid = parse_id(asset_id, "asset ID")
    econ = await bot.rbx.economy_details(aid)
    if not econ:
        await show(interaction, not_found("Asset", f"No asset with the ID `{aid}` was found."))
        return
    details, thumb = await asyncio.gather(bot.rbx.asset_details([aid]), bot.rbx.asset_thumbnail(aid))
    await show(interaction, build_asset_card(aid, econ, details.get(str(aid)), thumb))


# ---------- games ----------

def build_game_card(d: dict, votes: Optional[dict], icon: Optional[str]) -> discord.Embed:
    place = d.get("rootPlaceId")
    e = discord.Embed(title=link_text(d.get("name"), 200), url=f"https://www.roblox.com/games/{place}",
                      description=clip(d.get("description"), 300) or None, color=COLORS["base"])
    c = d.get("creator") or {}
    e.add_field(name="Playing now", value=num(d.get("playing")), inline=True)
    e.add_field(name="Visits", value=num(d.get("visits")), inline=True)
    e.add_field(name="Favorites", value=num(d.get("favoritedCount")), inline=True)
    if votes:
        up, down = votes.get("upVotes") or 0, votes.get("downVotes") or 0
        pct = f" ({up / (up + down) * 100:.0f}%)" if up + down else ""
        e.add_field(name="Likes", value=f"{num(up)}{pct}", inline=True)
        e.add_field(name="Dislikes", value=num(down), inline=True)
    e.add_field(name="Max players", value=num(d.get("maxPlayers")), inline=True)
    e.add_field(name="Creator", value=creator_link(c.get("name"), c.get("type"), c.get("id")), inline=True)
    if d.get("genre"):
        e.add_field(name="Genre", value=link_text(d["genre"], 30), inline=True)
    e.add_field(name="Created", value=when(d.get("created")), inline=True)
    e.add_field(name="Updated", value=when(d.get("updated"), "R"), inline=True)
    e.add_field(name="Place ID", value=f"`{place}`", inline=True)
    e.add_field(name="Universe ID", value=f"`{d.get('id')}`", inline=True)
    if icon:
        e.set_thumbnail(url=icon)
    e.set_footer(text=CFG["Footer"])
    return e


@bot.tree.command(name="game", description="Look up a Roblox game by name, place ID, or link")
@app_commands.describe(game="Game name, place ID, or a roblox.com/games link")
async def game_cmd(interaction: discord.Interaction, game: str):
    await interaction.response.defer(thinking=True)
    universe = await pick_game(interaction, game)
    if not universe:
        return
    d = await bot.rbx.game_details(universe)
    if not d:
        await show(interaction, not_found("Game", f"No game was found for `{link_text(game, 60)}`."))
        return
    votes, icon = await asyncio.gather(bot.rbx.game_votes(universe), bot.rbx.game_icon(universe))
    await show(interaction, build_game_card(d, votes, icon))


# ---------- badges ----------

def build_badge_card(d: dict, icon: Optional[str]) -> discord.Embed:
    e = discord.Embed(title=link_text(d.get("name"), 200), url=f"https://www.roblox.com/badges/{d.get('id')}",
                      description=clip(d.get("description"), 300) or None, color=COLORS["base"])
    stats = d.get("statistics") or {}
    win = stats.get("winRatePercentage")
    e.add_field(name="Badge ID", value=f"`{d.get('id')}`", inline=True)
    e.add_field(name="Status", value="Enabled" if d.get("enabled") else "Disabled", inline=True)
    e.add_field(name="Awarded", value=num(stats.get("awardedCount")), inline=True)
    e.add_field(name="Past day", value=num(stats.get("pastDayAwardedCount")), inline=True)
    if isinstance(win, (int, float)):
        e.add_field(name="Win rate", value=f"{win * 100:.2f}%", inline=True)
    e.add_field(name="Created", value=when(d.get("created")), inline=True)
    game = d.get("awardingUniverse") or {}
    if game.get("name"):
        place = game.get("rootPlaceId")
        label = link_text(game["name"], 40)
        e.add_field(name="Game", value=f"[{label}](https://www.roblox.com/games/{place})" if place else label,
                    inline=False)
    if icon:
        e.set_thumbnail(url=icon)
    e.set_footer(text=CFG["Footer"])
    return e


@bot.tree.command(name="badge", description="Look up a Roblox badge by ID")
@app_commands.describe(badge_id="Badge ID or a roblox.com badge link")
async def badge_cmd(interaction: discord.Interaction, badge_id: str):
    await interaction.response.defer(thinking=True)
    bid = parse_id(badge_id, "badge ID")
    d = await bot.rbx.badge_details(bid)
    if not d:
        await show(interaction, not_found("Badge", f"There's no badge with the ID `{bid}`."))
        return
    await show(interaction, build_badge_card(d, await bot.rbx.badge_icon(bid)))


# ================================================================
# NAME OR ID: groups and games
# ================================================================

SEARCH_TTL = 90.0
_search_cache: dict = {}


def looks_like_id(q: str) -> bool:
    """A bare number or a roblox.com link counts as an ID. Anything else is treated as a name."""
    q = (q or "").strip()
    return bool(re.fullmatch(r"\d+", q) or re.search(r"roblox\.com|rbxcdn\.com", q, re.I))


def id_from_text(q: str) -> Optional[int]:
    m = (re.search(r"(?:communities|groups|games|users|badges|catalog|library|asset|store/asset)/(\d+)", q, re.I)
         or re.search(r"\d+", q or ""))
    return int(m.group(1) if m and m.groups() else m.group()) if m else None


async def cached_search(kind: str, query: str) -> list[dict]:
    key = (kind, query.strip().lower())
    hit = _search_cache.get(key)
    if hit and time.monotonic() - hit[0] < SEARCH_TTL:
        return hit[1]
    results = await (bot.rbx.search_groups(query) if kind == "group" else bot.rbx.search_games(query))
    if len(_search_cache) > 300:
        _search_cache.clear()
    _search_cache[key] = (time.monotonic(), results)
    return results


async def pick_group(interaction: discord.Interaction, query: str, *, strict: bool) -> Optional[int]:
    """Group ID from an ID, a link, or a name. strict=True never guesses (used before any join)."""
    q = (query or "").strip()
    if looks_like_id(q):
        gid = id_from_text(q)
        if not gid:
            raise RobloxError("That doesn't look like a valid group ID.")
        return gid

    results = await cached_search("group", q)
    exact = [g for g in results if (g.get("name") or "").strip().lower() == q.lower()]
    if exact:
        return int(max(exact, key=lambda g: g.get("memberCount") or 0)["id"])
    if not results:
        await show(interaction, not_found("Group", f"No group matched `{link_text(q, 60)}`. Try the group ID."))
        return None
    if strict:
        lines = [f"[{esc(link_text(g.get('name'), 40))}]({group_url(g['id'])})  `{g['id']}`  "
                 f"{num(g.get('memberCount'))} members" for g in results[:6]]
        await show(interaction, make_embed(
            "Which group?",
            f"No group is named exactly `{link_text(q, 60)}`, and this action won't guess. "
            "Pick one from the suggestions while typing, or run it again with the ID:\n\n" + "\n".join(lines)))
        return None
    return int(results[0]["id"])


async def pick_game(interaction: discord.Interaction, query: str) -> Optional[int]:
    """Universe ID from a name, place ID, link, or the 'universe:ID' value autocomplete produces."""
    q = (query or "").strip()
    m = re.fullmatch(r"universe:(\d+)", q, re.I)
    if m:
        return int(m.group(1))
    if looks_like_id(q):
        n = id_from_text(q)
        if not n:
            raise RobloxError("That doesn't look like a valid game ID.")
        return await bot.rbx.universe_from_place(n) or n  # a place ID resolves to its universe

    results = await cached_search("game", q)
    exact = [g for g in results if (g.get("name") or "").strip().lower() == q.lower()]
    chosen = exact[0] if exact else (results[0] if results else None)
    if not chosen:
        await show(interaction, not_found(
            "Game", f"No game matched `{link_text(q, 60)}`. Try the place ID or the game's link."))
        return None
    return int(chosen["universeId"])


async def _autocomplete(kind: str, current: str) -> list[app_commands.Choice]:
    cur = (current or "").strip()
    if len(cur) < 2 or bot.rbx is None:
        return []
    if looks_like_id(cur):
        n = id_from_text(cur)
        return [app_commands.Choice(name=f"Use ID {n}", value=str(n))] if n else []
    try:
        results = await asyncio.wait_for(cached_search(kind, cur), timeout=2.5)
    except Exception:
        return []
    out = []
    for r in results[:25]:
        if kind == "group":
            name, value = f"{clip(r.get('name'), 62)}  ({num(r.get('memberCount'))} members)", str(r.get("id"))
        else:
            playing = r.get("playerCount")
            name = clip(r.get("name"), 70) + (f"  ({num(playing)} playing)" if playing is not None else "")
            value = f"universe:{r.get('universeId')}"
        if name.strip() and value not in ("None", "universe:None"):
            out.append(app_commands.Choice(name=name[:100], value=value))
    return out


async def group_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete("group", current)


async def game_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete("game", current)


for _cmd, _param in ((check, "group"), (search, "group"), (join, "group"), (group_cmd, "group")):
    _cmd.autocomplete(_param)(group_autocomplete)
game_cmd.autocomplete("game")(game_autocomplete)
search.autocomplete("amount")(amount_autocomplete)


# ================================================================
# BIG LISTS: lazy-loading pager
# ================================================================

def esc(text: str) -> str:
    """Escape Discord markdown so names like cool_user_1 don't turn italic."""
    return re.sub(r"([_*~|>\\])", r"\\\1", text)


class CursorPager(discord.ui.View):
    """Shows rows a page at a time and fetches the next batch from Roblox only when you reach the end."""
    MAX_ROWS = 5000

    def __init__(self, owner_id: int, title: str, header: str, rows: list[str], cursor: Optional[str],
                 fetch, color: int = COLORS["base"], thumbnail: Optional[str] = None):
        super().__init__(timeout=300)
        self.owner_id, self.title, self.header = owner_id, title, header
        self.rows, self.cursor, self.fetch = rows, cursor, fetch
        self.color, self.thumbnail = color, thumbnail
        self.index = 0
        self.lock = asyncio.Lock()
        self.message: Optional[discord.Message] = None
        self._sync()

    def _has_next(self) -> bool:
        return (self.index + 1) * CFG["PerPage"] < len(self.rows) or self.cursor is not None

    def embed(self) -> discord.Embed:
        per = CFG["PerPage"]
        start = self.index * per
        chunk = self.rows[start:start + per]
        total = f"{len(self.rows)}+" if self.cursor is not None else str(len(self.rows))
        body = (self.header + "\n\n" if self.header else "") + ("\n".join(chunk) or "Nothing here.")
        e = discord.Embed(title=self.title, description=body, color=self.color)
        e.set_footer(text=f"{start + 1} to {start + len(chunk)} of {total}  |  {CFG['Footer']}")
        if self.thumbnail:
            e.set_thumbnail(url=self.thumbnail)
        return e

    def _sync(self):
        self.prev_btn.disabled = self.index == 0
        self.next_btn.disabled = not self._has_next()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                embed=error_embed("These results belong to someone else.", "Not yours"), ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.lock:
            self.index = max(0, self.index - 1)
            self._sync()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        async with self.lock:
            per = CFG["PerPage"]
            tries = 0
            while (self.index + 1) * per >= len(self.rows) and self.cursor is not None and tries < 3:
                tries += 1
                try:
                    new_rows, self.cursor = await self.fetch(self.cursor)
                except RobloxError:
                    self.cursor = None
                    break
                self.rows.extend(new_rows)
                if len(self.rows) >= self.MAX_ROWS:
                    self.cursor = None
            if (self.index + 1) * per < len(self.rows):
                self.index += 1
            self._sync()
        await interaction.edit_original_response(embed=self.embed(), view=self)


async def list_command(interaction: discord.Interaction, *, title: str, header: str, fetch, fmt,
                       thumbnail: Optional[str] = None, empty: str = "Nothing to show."):
    """fetch(cursor) -> (items, next_cursor). The first page loads now, the rest as you page."""
    try:
        items, cursor = await fetch("")
    except RobloxError as e:
        if isinstance(e, (AuthError, ChallengeRequired)):
            raise
        await show(interaction, error_embed(
            f"Roblox wouldn't return this list. It may be private. ({e})", "List unavailable"))
        return
    rows = [fmt(i) for i in items]
    if not rows:
        e = make_embed(title, (header + "\n\n" if header else "") + empty)
        if thumbnail:
            e.set_thumbnail(url=thumbnail)
        await show(interaction, e)
        return

    async def fetch_rows(cur: str):
        its, nxt = await fetch(cur)
        return [fmt(i) for i in its], nxt

    pager = CursorPager(interaction.user.id, title, header, rows, cursor, fetch_rows, thumbnail=thumbnail)
    pager.message = await show(interaction, pager.embed(), view=pager)


# ---------- row formatters ----------

def user_row(u: dict) -> str:
    uid = u.get("id") or u.get("userId")
    name, disp = u.get("name") or u.get("username"), u.get("displayName")
    label = esc(link_text(disp or name, 32))
    handle = f" (@{esc(link_text(name, 24))})" if disp and name and disp != name else ""
    tail = "  Banned" if u.get("isBanned") else ""
    return f"[{label}]({profile_url(uid)}){handle}  `{uid}`{tail}"


def member_row(m: dict) -> str:
    u = m.get("user") or {}
    row = user_row({"id": u.get("userId"), "name": u.get("username"), "displayName": u.get("displayName")})
    role = (m.get("role") or {}).get("name")
    return row + (f"  {esc(link_text(role, 24))}" if role else "")


def game_row(d: dict) -> str:
    place = (d.get("rootPlace") or {}).get("id")
    name = esc(link_text(d.get("name"), 44))
    label = f"[{name}](https://www.roblox.com/games/{place})" if place else name
    visits = d.get("placeVisits")
    return f"{label}  `{place or d.get('id')}`" + (f"  {num(visits)} visits" if visits else "")


def badge_row(d: dict) -> str:
    awarded = (d.get("statistics") or {}).get("awardedCount")
    return (f"[{esc(link_text(d.get('name'), 44))}](https://www.roblox.com/badges/{d.get('id')})  `{d.get('id')}`"
            + (f"  {num(awarded)} awarded" if awarded is not None else ""))


def pass_row(d: dict) -> str:
    price = d.get("price")
    cost = "Off sale" if price is None else ("Free" if price == 0 else f"R$ {num(price)}")
    return (f"[{esc(link_text(d.get('name'), 44))}](https://www.roblox.com/game-pass/{d.get('id')})  "
            f"`{d.get('id')}`  {cost}")


def wear_row(d: dict) -> str:
    econ = d["econ"]
    type_name = ASSET_TYPES.get(econ.get("AssetTypeId"), "Item")
    if econ.get("IsForSale"):
        price = econ.get("PriceInRobux")
        cost = "Free" if price == 0 else (f"R$ {num(price)}" if price is not None else "On sale")
    else:
        cost = "Not for sale"
    return (f"[{esc(link_text(econ.get('Name'), 40))}](https://www.roblox.com/catalog/{d['id']})  "
            f"`{d['id']}`  {type_name}  |  {cost}")


def user_ref(u: dict) -> str:
    return f"[{esc(link_text(u.get('name'), 30))}]({profile_url(u['id'])})"


# ================================================================
# USER LISTS: followers, following, friends, badges, games, wearing
# ================================================================

ORDER = [app_commands.Choice(name="Newest first", value="Desc"),
         app_commands.Choice(name="Oldest first", value="Asc")]
USER_ARG = "Username, user ID, or a roblox.com profile link"


async def follow_list(interaction: discord.Interaction, user: str, kind: str, order: Optional[str]):
    u = await find_user(interaction, user)
    if not u:
        return
    uid, rbx, order = int(u["id"]), bot.rbx, order or "Desc"
    count, head = await asyncio.gather(rbx.user_count(uid, kind), rbx.user_headshot(uid))
    if kind == "followers":
        title, line = "Followers", f"{user_ref(u)} has {num(count)} followers."
    else:
        title, line = "Following", f"{user_ref(u)} follows {num(count)} accounts."
    header = f"{line} {'Newest' if order == 'Desc' else 'Oldest'} first."

    async def fetch(cur: str):
        return await rbx.follow_page(uid, kind, cur, order)

    await list_command(interaction, title=title, header=header, fetch=fetch, fmt=user_row,
                       thumbnail=head, empty="Nobody here.")


@bot.tree.command(name="followers", description="List who follows a Roblox user")
@app_commands.describe(user=USER_ARG, order="Which end of the list to start from")
@app_commands.choices(order=ORDER)
async def followers_cmd(interaction: discord.Interaction, user: str,
                        order: Optional[app_commands.Choice[str]] = None):
    await interaction.response.defer(thinking=True)
    await follow_list(interaction, user, "followers", order.value if order else None)


@bot.tree.command(name="following", description="List the accounts a Roblox user follows")
@app_commands.describe(user=USER_ARG, order="Which end of the list to start from")
@app_commands.choices(order=ORDER)
async def following_cmd(interaction: discord.Interaction, user: str,
                        order: Optional[app_commands.Choice[str]] = None):
    await interaction.response.defer(thinking=True)
    await follow_list(interaction, user, "followings", order.value if order else None)


@bot.tree.command(name="friends", description="List a Roblox user's friends")
@app_commands.describe(user=USER_ARG)
async def friends_cmd(interaction: discord.Interaction, user: str):
    await interaction.response.defer(thinking=True)
    u = await find_user(interaction, user)
    if not u:
        return
    uid, rbx = int(u["id"]), bot.rbx
    count, head = await asyncio.gather(rbx.user_count(uid, "friends"), rbx.user_headshot(uid))

    async def fetch(cur: str):
        items = await rbx.friends_list(uid)
        items.sort(key=lambda f: (f.get("displayName") or f.get("name") or "").lower())
        return items, None

    await list_command(interaction, title="Friends", header=f"{user_ref(u)} has {num(count)} friends.",
                       fetch=fetch, fmt=user_row, thumbnail=head, empty="No friends to show.")


@bot.tree.command(name="mutual", description="Mutual friends and groups between two Roblox users")
@app_commands.describe(user="First user (name, ID, or link)", other="Second user (name, ID, or link)")
async def mutual_cmd(interaction: discord.Interaction, user: str, other: str):
    await interaction.response.defer(thinking=True)
    u1 = await find_user(interaction, user)
    if not u1:
        return
    u2 = await find_user(interaction, other)
    if not u2:
        return
    id1, id2, rbx = int(u1["id"]), int(u2["id"]), bot.rbx
    f1, f2, g1, g2 = await asyncio.gather(
        rbx.friends_list(id1), rbx.friends_list(id2), rbx.user_groups(id1), rbx.user_groups(id2),
        return_exceptions=True)
    for r in (f1, f2, g1, g2):
        if isinstance(r, (AuthError, ChallengeRequired)):
            raise r
    f1, f2 = (f1 if isinstance(f1, list) else None), (f2 if isinstance(f2, list) else None)
    g1, g2 = (g1 if isinstance(g1, list) else []), (g2 if isinstance(g2, list) else [])

    e = make_embed("Mutual connections", f"{user_ref(u1)} and {user_ref(u2)}")
    if f1 is None or f2 is None:
        e.add_field(name="Mutual friends", value="One of the friend lists couldn't be loaded.", inline=False)
    else:
        ids2 = {x["id"]: x for x in f2}
        shared = [x for x in f1 if x["id"] in ids2]
        shared.sort(key=lambda f: (f.get("displayName") or f.get("name") or "").lower())
        text = "\n".join(user_row(x) for x in shared[:12]) or "None"
        if len(shared) > 12:
            text += f"\n...and {len(shared) - 12} more"
        e.add_field(name=f"Mutual friends ({len(shared)})", value=clip_field(text), inline=False)
        e.add_field(name="Friends with each other", value="Yes" if id2 in {x["id"] for x in f1} else "No", inline=True)

    gids2 = {g["group"]["id"] for g in g2}
    shared_groups = [g for g in g1 if g["group"]["id"] in gids2]
    shared_groups.sort(key=lambda g: g["group"].get("memberCount") or 0, reverse=True)
    text = "\n".join(f"[{esc(link_text(g['group'].get('name'), 40))}]({group_url(g['group']['id'])})  "
                     f"`{g['group']['id']}`" for g in shared_groups[:10]) or "None"
    if len(shared_groups) > 10:
        text += f"\n...and {len(shared_groups) - 10} more"
    e.add_field(name=f"Mutual groups ({len(shared_groups)})", value=clip_field(text), inline=False)
    await show(interaction, e)


def clip_field(text: str, limit: int = 1000) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


@bot.tree.command(name="userbadges", description="List the badges a Roblox user has earned")
@app_commands.describe(user=USER_ARG)
async def userbadges_cmd(interaction: discord.Interaction, user: str):
    await interaction.response.defer(thinking=True)
    u = await find_user(interaction, user)
    if not u:
        return
    uid, rbx = int(u["id"]), bot.rbx
    head = await rbx.user_headshot(uid)

    async def fetch(cur: str):
        return await rbx.user_badges_page(uid, cur)

    await list_command(interaction, title="Badges", header=f"Badges earned by {user_ref(u)}, newest first.",
                       fetch=fetch, fmt=badge_row, thumbnail=head, empty="No badges to show.")


@bot.tree.command(name="usergames", description="List the games a Roblox user has created")
@app_commands.describe(user=USER_ARG)
async def usergames_cmd(interaction: discord.Interaction, user: str):
    await interaction.response.defer(thinking=True)
    u = await find_user(interaction, user)
    if not u:
        return
    uid, rbx = int(u["id"]), bot.rbx
    head = await rbx.user_headshot(uid)

    async def fetch(cur: str):
        return await rbx.user_games_page(uid, cur)

    await list_command(interaction, title="Created games", header=f"Games made by {user_ref(u)}. Codes are place IDs.",
                       fetch=fetch, fmt=game_row, thumbnail=head, empty="No public games.")


@bot.tree.command(name="favorites", description="List a Roblox user's favorite games")
@app_commands.describe(user=USER_ARG)
async def favorites_cmd(interaction: discord.Interaction, user: str):
    await interaction.response.defer(thinking=True)
    u = await find_user(interaction, user)
    if not u:
        return
    uid, rbx = int(u["id"]), bot.rbx
    head = await rbx.user_headshot(uid)

    async def fetch(cur: str):
        return await rbx.user_favorites_page(uid, cur)

    await list_command(interaction, title="Favorite games", header=f"Favorites of {user_ref(u)}. Codes are place IDs.",
                       fetch=fetch, fmt=game_row, thumbnail=head, empty="No favorites to show.")


@bot.tree.command(name="wearing", description="List the items a Roblox user is wearing right now")
@app_commands.describe(user=USER_ARG)
async def wearing_cmd(interaction: discord.Interaction, user: str):
    await interaction.response.defer(thinking=True)
    u = await find_user(interaction, user)
    if not u:
        return
    uid, rbx = int(u["id"]), bot.rbx
    try:
        ids = await rbx.currently_wearing(uid)
    except RobloxError as e:
        if isinstance(e, (AuthError, ChallengeRequired)):
            raise
        await show(interaction, error_embed(f"Roblox wouldn't return the outfit. ({e})", "Outfit unavailable"))
        return
    sem = asyncio.Semaphore(6)

    async def one(aid: int):
        async with sem:
            return {"id": aid, "econ": await rbx.economy_details(aid) or {"Name": f"Asset {aid}"}}

    items = list(await asyncio.gather(*(one(i) for i in ids[:40])))
    head = await rbx.user_headshot(uid)

    async def fetch(cur: str):
        return items, None

    await list_command(interaction, title="Currently wearing",
                       header=f"{user_ref(u)} is wearing {len(ids)} items.", fetch=fetch, fmt=wear_row,
                       thumbnail=head, empty="Not wearing anything.")


@bot.tree.command(name="names", description="Show a Roblox user's past usernames")
@app_commands.describe(user=USER_ARG)
async def names_cmd(interaction: discord.Interaction, user: str):
    await interaction.response.defer(thinking=True)
    u = await find_user(interaction, user)
    if not u:
        return
    uid = int(u["id"])
    history, head = await asyncio.gather(bot.rbx.username_history(uid, 50), bot.rbx.user_headshot(uid))
    e = make_embed("Past usernames", f"{user_ref(u)}, currently @{esc(link_text(u.get('name'), 30))}")
    e.add_field(name=f"Previous ({len(history)})",
                value=clip_field(", ".join(f"`{link_text(n, 24)}`" for n in history)) if history
                else "No past usernames.", inline=False)
    if head:
        e.set_thumbnail(url=head)
    await show(interaction, e)


@bot.tree.command(name="membership", description="Check whether a Roblox user is in a group, and their role")
@app_commands.describe(user=USER_ARG, group="Group name or ID")
async def membership_cmd(interaction: discord.Interaction, user: str, group: str):
    await interaction.response.defer(thinking=True)
    u = await find_user(interaction, user)
    if not u:
        return
    gid = await pick_group(interaction, group, strict=False)
    if not gid:
        return
    rbx = bot.rbx
    d, groups, head = await asyncio.gather(rbx.group_details(gid), rbx.user_groups(int(u["id"])), rbx.user_headshot(int(u["id"])))
    if not d:
        await show(interaction, not_found("Group", f"There's no group with the ID `{gid}`."))
        return
    link = f"[{esc(link_text(d.get('name'), 40))}]({group_url(gid)})"
    match = next((g for g in groups if int(g["group"]["id"]) == gid), None)
    if not match:
        e = make_embed("Not in group", f"{user_ref(u)} isn't in {link} (`{gid}`).")
    else:
        role = match.get("role") or {}
        e = make_embed("Group membership", f"{user_ref(u)} is in {link} (`{gid}`).")
        e.add_field(name="Role", value=esc(link_text(role.get("name"), 40)), inline=True)
        e.add_field(name="Rank", value=str(role.get("rank", "Unknown")), inline=True)
        e.add_field(name="Members", value=num(d.get("memberCount")), inline=True)
    if head:
        e.set_thumbnail(url=head)
    await show(interaction, e)


# ================================================================
# GROUP LISTS: members, games
# ================================================================

@bot.tree.command(name="members", description="List the members of a Roblox group")
@app_commands.describe(group="Group name or ID", order="Which end of the list to start from")
@app_commands.choices(order=ORDER)
async def members_cmd(interaction: discord.Interaction, group: str,
                      order: Optional[app_commands.Choice[str]] = None):
    await interaction.response.defer(thinking=True)
    gid = await pick_group(interaction, group, strict=False)
    if not gid:
        return
    rbx, ordv = bot.rbx, (order.value if order else "Desc")
    d, icon = await asyncio.gather(rbx.group_details(gid), rbx.group_icon(gid))
    if not d:
        await show(interaction, not_found("Group", f"There's no group with the ID `{gid}`."))
        return
    header = (f"[{esc(link_text(d.get('name'), 40))}]({group_url(gid)}) has {num(d.get('memberCount'))} members. "
              f"{'Newest' if ordv == 'Desc' else 'Oldest'} first.")

    async def fetch(cur: str):
        return await rbx.group_members_page(gid, cur, ordv)

    await list_command(interaction, title="Group members", header=header, fetch=fetch, fmt=member_row,
                       thumbnail=icon, empty="No members to show.")


@bot.tree.command(name="groupgames", description="List the games a Roblox group has made")
@app_commands.describe(group="Group name or ID")
async def groupgames_cmd(interaction: discord.Interaction, group: str):
    await interaction.response.defer(thinking=True)
    gid = await pick_group(interaction, group, strict=False)
    if not gid:
        return
    rbx = bot.rbx
    d, icon = await asyncio.gather(rbx.group_details(gid), rbx.group_icon(gid))
    if not d:
        await show(interaction, not_found("Group", f"There's no group with the ID `{gid}`."))
        return

    async def fetch(cur: str):
        return await rbx.group_games_page(gid, cur)

    await list_command(interaction, title="Group games",
                       header=f"Games made by [{esc(link_text(d.get('name'), 40))}]({group_url(gid)}). Codes are place IDs.",
                       fetch=fetch, fmt=game_row, thumbnail=icon, empty="No public games.")


# ================================================================
# GAME LISTS: game passes, badges
# ================================================================

async def game_list(interaction: discord.Interaction, game: str, *, title: str, noun: str, page_fn, fmt, empty: str):
    universe = await pick_game(interaction, game)
    if not universe:
        return
    rbx = bot.rbx
    d, icon = await asyncio.gather(rbx.game_details(universe), rbx.game_icon(universe))
    if not d:
        await show(interaction, not_found("Game", f"No game was found for `{link_text(game, 60)}`."))
        return
    place = d.get("rootPlaceId")
    name = f"[{esc(link_text(d.get('name'), 40))}](https://www.roblox.com/games/{place})"

    async def fetch(cur: str):
        return await getattr(rbx, page_fn)(universe, cur)

    await list_command(interaction, title=title, header=f"{noun} in {name}.", fetch=fetch, fmt=fmt,
                       thumbnail=icon, empty=empty)


@bot.tree.command(name="gamepasses", description="List a Roblox game's game passes and prices")
@app_commands.describe(game="Game name, place ID, or a roblox.com/games link")
async def gamepasses_cmd(interaction: discord.Interaction, game: str):
    await interaction.response.defer(thinking=True)
    await game_list(interaction, game, title="Game passes", noun="Game passes", page_fn="game_passes_page",
                    fmt=pass_row, empty="This game has no game passes.")


@bot.tree.command(name="gamebadges", description="List a Roblox game's badges and how many were awarded")
@app_commands.describe(game="Game name, place ID, or a roblox.com/games link")
async def gamebadges_cmd(interaction: discord.Interaction, game: str):
    await interaction.response.defer(thinking=True)
    await game_list(interaction, game, title="Game badges", noun="Badges", page_fn="game_badges_page",
                    fmt=badge_row, empty="This game has no badges.")


for _cmd in (gamepasses_cmd, gamebadges_cmd):
    _cmd.autocomplete("game")(game_autocomplete)
for _cmd in (members_cmd, groupgames_cmd, membership_cmd):
    _cmd.autocomplete("group")(group_autocomplete)


# ---------- help ----------

@bot.tree.command(name="help", description="List everything DeadAir can do")
async def help_cmd(interaction: discord.Interaction):
    e = make_embed("DeadAir", "Roblox lookups and audio checking. Works in servers and DMs.\n"
                              "Groups and games accept a name (pick from the suggestions), an ID, or a link.")
    e.add_field(name="Audio", value=(
        "`/check` working or moderated, plus info, loudness and a waveform\n"
        "`/search` search a group's audio library by name or keyword"), inline=False)
    e.add_field(name="Users", value=(
        "`/user` profile card\n`/avatar` current avatar\n`/wearing` items they have on\n"
        "`/followers` `/following` `/friends` full lists, paged\n"
        "`/mutual` shared friends and groups between two users\n"
        "`/usergroups` `/membership` groups and roles\n"
        "`/userbadges` `/usergames` `/favorites` `/names`"), inline=False)
    e.add_field(name="Groups", value=(
        "`/group` group card\n`/members` member list\n`/groupgames` games the group made"), inline=False)
    e.add_field(name="Games and items", value=(
        "`/game` game card\n`/gamepasses` `/gamebadges` passes and badges for a game\n"
        "`/asset` any asset by ID\n`/badge` badge by ID"), inline=False)
    e.add_field(name="Bot", value="`/join` make the bot's account join a group\n`/whoami` which account the bot uses",
                inline=False)
    await interaction.response.send_message(embed=e, ephemeral=True)


# ================================================================
# RUN
# ================================================================

if __name__ == "__main__":
    if not CFG["Token"] or not CFG["Cookie"]:
        raise SystemExit("Missing DISCORD_TOKEN or ROBLOX_COOKIE. Copy .env.example to .env and fill it in.")
    bot.run(CFG["Token"])
