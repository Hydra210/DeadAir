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
from datetime import datetime, timezone
from typing import Optional

import discord
from aiohttp import web
from discord import app_commands
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

class DeadAir(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        # Every command can be installed to a user's account or a server, and run in
        # servers, bot DMs, and group DMs / DMs with other users.
        self.tree = app_commands.CommandTree(
            self,
            allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
            allowed_contexts=app_commands.AppCommandContext(
                guild=True, dm_channel=True, private_channel=True),
        )
        self.rbx: Optional[RobloxClient] = None
        self.health: Optional[web.AppRunner] = None

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

    async def close(self):
        if self.rbx:
            await self.rbx.close()
        if self.health:
            await self.health.cleanup()
        await super().close()


bot = DeadAir()


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
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
        "challenge": (f"Roblox asked for a captcha when joining {link}, and a bot can't solve that. "
                      "Join the group once by hand on the bot's account, then run this again."),
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
    group_id="Optional: a group to make sure the bot's account is in first",
    only_moderated="For several IDs: only show the moderated ones",
)
async def check(interaction: discord.Interaction, ids: str, group_id: Optional[str] = None,
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

    if group_id:
        if not await ensure_group_access(interaction, parse_id(group_id, "group ID")):
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


@bot.tree.command(name="search", description="Search a group's audio library by name or keyword")
@app_commands.describe(
    group_id="The group ID (a group URL works too)",
    term="Keyword(s) or an exact audio name",
    exact="Match the name exactly instead of by keywords",
    only_moderated="Only show the moderated ones",
)
async def search(interaction: discord.Interaction, group_id: str, term: str, exact: bool = False,
                 only_moderated: bool = False):
    await interaction.response.defer(thinking=True)
    term = term.strip()
    if not term:
        await show(interaction, error_embed("Give me something to search for.", "Empty search"))
        return

    gid = parse_id(group_id, "group ID")
    info = await ensure_group_access(interaction, gid)
    if not info:
        return

    try:
        everything = await bot.rbx.list_group_audio(gid)
    except RobloxError as e:
        if str(e).startswith("403"):
            await show(interaction, error_embed(
                f"The account is in [{link_text(info.name)}]({group_url(gid)}), but its role can't view the "
                "group's audio. It needs a role with asset or creations permissions.", "Missing permissions"))
            return
        raise

    low = term.lower()
    words = low.split()
    matched = [it for it in everything
               if (name := (it.get("name") or "").lower()) == low
               or (not exact and all(w in name for w in words))]

    link = f"[{link_text(info.name)}]({group_url(gid)})"
    if not matched:
        extra = ""
        try:
            role = await bot.rbx.group_role(gid)
        except RobloxError:
            role = None
        if role:
            extra += f"\nThe bot's account is in this group as **{link_text(role['name'], 40)}** (rank {role['rank']})."
        if not everything:
            extra += ("\nRoblox returned **0 audios** for this account. Either the group has no audio, or this role "
                      "can't see the group's creations. Roblox sometimes returns an empty list instead of an error, "
                      "so check the role's permissions in the group settings.")
        if everything:
            sample = ", ".join(f"`{link_text(it.get('name'), 30)}`" for it in everything[:5])
            extra += f"\nFirst names the bot saw: {sample}"
        skipped = getattr(bot.rbx, "last_skipped", 0)
        if skipped:
            extra += (f"\n{skipped} entries were skipped because Roblox hasn't given them an asset ID yet "
                      "(usually still processing).")
        await show(interaction, make_embed(
            "No matches", f"Nothing matching `{link_text(term, 60)}` in {link} "
                          f"({len(everything)} audios searched).{extra}"))
        return

    ids = [int(m["assetId"]) for m in matched]
    details = await bot.rbx.asset_details(ids)
    rows = [{"id": int(m["assetId"]), "name": m.get("name"),
             "status": status_to_working(details.get(str(m["assetId"])))} for m in matched]
    icon = await bot.rbx.group_icon(gid)

    header = (f"{len(matched)} of {len(everything)} audios in {link} (`{gid}`) match "
              f"`{link_text(term, 60)}`.")
    await send_results(interaction, "Audio search", header, rows, only_moderated, icon)


@bot.tree.command(name="join", description="Make the bot's Roblox account join a group")
@app_commands.describe(group_id="The group ID (a group URL works too)")
async def join(interaction: discord.Interaction, group_id: str):
    await interaction.response.defer(thinking=True)
    gid = parse_id(group_id, "group ID")
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


@bot.tree.command(name="group", description="Look up a Roblox group by ID or link")
@app_commands.describe(group="Group ID or a roblox.com group link")
async def group_cmd(interaction: discord.Interaction, group: str):
    await interaction.response.defer(thinking=True)
    gid = parse_id(group, "group ID")
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


@bot.tree.command(name="game", description="Look up a Roblox game by place ID, universe ID, or link")
@app_commands.describe(game="Place ID, universe ID, or a roblox.com/games link")
async def game_cmd(interaction: discord.Interaction, game: str):
    await interaction.response.defer(thinking=True)
    n = parse_id(game, "game ID")
    universe = await bot.rbx.universe_from_place(n) or n  # a place ID resolves to its universe; otherwise try it as one
    d = await bot.rbx.game_details(universe)
    if not d:
        await show(interaction, not_found("Game", f"No game with the place or universe ID `{n}` was found."))
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


# ---------- help ----------

@bot.tree.command(name="help", description="List everything DeadAir can do")
async def help_cmd(interaction: discord.Interaction):
    e = make_embed("DeadAir", "Roblox lookups and audio checking. Everything works in servers and DMs.")
    e.add_field(name="Audio", value=(
        "`/check` working or moderated, plus info, loudness and a waveform\n"
        "`/search` search a group's audio library by name or keyword"), inline=False)
    e.add_field(name="Lookups", value=(
        "`/user` profile by username, ID or link\n"
        "`/avatar` a user's current avatar\n"
        "`/usergroups` the groups a user is in\n"
        "`/group` group info by ID or link\n"
        "`/asset` any asset type by ID\n"
        "`/game` game info by place or universe ID\n"
        "`/badge` badge info by ID"), inline=False)
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
