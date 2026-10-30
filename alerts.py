# DeadAir — alerts.py
# /remind (reminders) and /watch (alerts when a Roblox audio or group changes).
# Credits: @Nexesmere / EXE Development
#
# Everything is stored in a small SQLite file (DATA_DIR, default ./data). Alerts are sent as a DM, with the
# channel the command was used in as a fallback. NOTE: hosts with a wiped disk (Render's free plan) lose the
# file on every redeploy or restart, so set DATA_DIR to a persistent disk if you want these to survive.

import asyncio
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks

import toolkit
from roblox import RobloxError, status_to_working

log = logging.getLogger("deadair.alerts")

# ================================================================
# CONFIG
# ================================================================

CFG = {
    "DbPath": os.path.join(os.getenv("DATA_DIR", "data"), "deadair.db"),
    "ReminderTick": 20,         # seconds between checks for due reminders
    "WatchTick": 600,           # seconds between Roblox checks
    "MaxRemindersPerUser": 25,
    "MaxWatchesPerUser": 15,
    "MaxWatchesTotal": 300,
    "MaxRemindSeconds": 366 * 86400,
}

STATE_LABEL = {"working": "working", "broken": "moderated", "unknown": "unknown"}
_bot: Optional[discord.Client] = None


# ================================================================
# STORAGE
# ================================================================

def _run(sql: str, params: tuple = (), fetch: bool = False):
    os.makedirs(os.path.dirname(CFG["DbPath"]) or ".", exist_ok=True)
    conn = sqlite3.connect(CFG["DbPath"])
    try:
        conn.row_factory = sqlite3.Row
        if sql == "SCHEMA":
            conn.executescript(SCHEMA)
            return None
        cur = conn.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()] if fetch else cur.lastrowid
        conn.commit()
        return rows
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, channel_id INTEGER,
    due REAL NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS watches (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, channel_id INTEGER,
    kind TEXT NOT NULL, target INTEGER NOT NULL, label TEXT, state TEXT, min_change INTEGER DEFAULT 1);
"""


async def db(sql: str, params: tuple = (), fetch: bool = False):
    return await asyncio.to_thread(_run, sql, params, fetch)


async def notify(user_id: int, channel_id: Optional[int], e: discord.Embed) -> bool:
    """DM first. If Discord won't allow that, try the channel the command came from."""
    assert _bot is not None
    try:
        user = await _bot.fetch_user(user_id)
        await user.send(embed=e)
        return True
    except discord.HTTPException as err:
        log.info("DM to %s failed (%s)", user_id, err)
    if channel_id:
        try:
            ch = _bot.get_channel(channel_id) or await _bot.fetch_channel(channel_id)
            await ch.send(content=f"<@{user_id}>", embed=e)
            return True
        except discord.HTTPException as err:
            log.info("Channel send to %s failed (%s)", channel_id, err)
    return False


def channel_of(interaction: discord.Interaction) -> Optional[int]:
    return interaction.channel_id if interaction.guild_id else None


# ================================================================
# /remind
# ================================================================

def make_group(name: str, description: str) -> app_commands.Group:
    return app_commands.Group(
        name=name, description=description,
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
        allowed_contexts=app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True))


remind = make_group("remind", "Reminders sent to you by DM")


@remind.command(name="add", description="Set a reminder")
@app_commands.describe(when="like in 2h30m, 6:30pm, or 2026-12-25 09:00", message="What to remind you about",
                       timezone="Timezone for clock times, like America/New_York or UTC-5 (default UTC)")
async def remind_add(interaction: discord.Interaction, when: str, message: app_commands.Range[str, 1, 500],
                     timezone: str = "UTC"):
    try:
        due = toolkit.parse_when(when, toolkit.parse_tz(timezone)).timestamp()
    except toolkit.UserError as err:
        await interaction.response.send_message(embed=toolkit.error_embed(str(err)), ephemeral=True)
        return
    now = time.time()
    problem = None
    if due <= now + 5:
        problem = "That time has already passed. Try something like `in 30m`."
    elif due - now > CFG["MaxRemindSeconds"]:
        problem = "Reminders can be at most a year away."
    else:
        mine = await db("SELECT COUNT(*) AS n FROM reminders WHERE user_id=?", (interaction.user.id,), True)
        if mine[0]["n"] >= CFG["MaxRemindersPerUser"]:
            problem = f"You already have {CFG['MaxRemindersPerUser']} reminders. Cancel one with `/remind cancel`."
    if problem:
        await interaction.response.send_message(embed=toolkit.error_embed(problem), ephemeral=True)
        return
    rid = await db("INSERT INTO reminders (user_id, channel_id, due, text) VALUES (?,?,?,?)",
                   (interaction.user.id, channel_of(interaction), due, message))
    e = toolkit.embed("Reminder set", f"<t:{int(due)}:F> (<t:{int(due)}:R>)\n{toolkit.clip(message, 300)}")
    e.set_footer(text=f"{toolkit.CFG['Footer']}  |  ID {rid}  |  Sent as a DM, so keep your DMs open")
    await interaction.response.send_message(embed=e, ephemeral=True)


@remind.command(name="list", description="Show your pending reminders")
async def remind_list(interaction: discord.Interaction):
    rows = await db("SELECT id, due, text FROM reminders WHERE user_id=? ORDER BY due", (interaction.user.id,), True)
    if not rows:
        await interaction.response.send_message(embed=toolkit.embed("Reminders", "You have no reminders."), ephemeral=True)
        return
    lines = [f"`{r['id']}`  <t:{int(r['due'])}:R>  {toolkit.clip(r['text'], 80)}" for r in rows]
    await interaction.response.send_message(embed=toolkit.embed("Reminders", "\n".join(lines)), ephemeral=True)


@remind.command(name="cancel", description="Cancel a reminder by its ID")
@app_commands.describe(reminder_id="The ID shown in /remind list")
async def remind_cancel(interaction: discord.Interaction, reminder_id: int):
    mine = await db("SELECT id FROM reminders WHERE id=? AND user_id=?", (reminder_id, interaction.user.id), True)
    if not mine:
        await interaction.response.send_message(
            embed=toolkit.error_embed("You have no reminder with that ID."), ephemeral=True)
        return
    await db("DELETE FROM reminders WHERE id=?", (reminder_id,))
    await interaction.response.send_message(embed=toolkit.embed("Reminder cancelled"), ephemeral=True)


@tasks.loop(seconds=CFG["ReminderTick"])
async def reminder_loop():
    due = await db("SELECT * FROM reminders WHERE due<=? ORDER BY due LIMIT 50", (time.time(),), True)
    for r in due:
        e = toolkit.embed("Reminder", toolkit.clip(r["text"], 1500))
        sent = await notify(r["user_id"], r["channel_id"], e)
        late = time.time() - r["due"]
        if sent or late > 86400:    # one that can't be delivered for a day is dropped instead of retried forever
            await db("DELETE FROM reminders WHERE id=?", (r["id"],))


# ================================================================
# /watch
# ================================================================

watch = make_group("watch", "Get a DM when a Roblox audio or group changes")


def asset_id_from(text: str) -> Optional[int]:
    m = re.search(r"(?:asset|store/asset|library|catalog)/(\d+)", text) or re.search(r"\d+", text)
    return int(m.group(1) if m and m.groups() else m.group()) if m else None


async def watch_quota(interaction: discord.Interaction) -> Optional[str]:
    mine = await db("SELECT COUNT(*) AS n FROM watches WHERE user_id=?", (interaction.user.id,), True)
    total = await db("SELECT COUNT(*) AS n FROM watches", (), True)
    if mine[0]["n"] >= CFG["MaxWatchesPerUser"]:
        return f"You already watch {CFG['MaxWatchesPerUser']} things. Remove one with `/watch stop`."
    if total[0]["n"] >= CFG["MaxWatchesTotal"]:
        return "The bot is watching as many things as it can right now. Try again later."
    return None


@watch.command(name="audio", description="DM me when an audio becomes moderated or starts working again")
@app_commands.describe(asset_id="Audio asset ID or link")
async def watch_audio(interaction: discord.Interaction, asset_id: str):
    await interaction.response.defer(thinking=True, ephemeral=True)
    aid = asset_id_from(asset_id)
    if not aid:
        await interaction.edit_original_response(embed=toolkit.error_embed("That isn't an asset ID."))
        return
    problem = await watch_quota(interaction)
    if problem:
        await interaction.edit_original_response(embed=toolkit.error_embed(problem))
        return
    dup = await db("SELECT id FROM watches WHERE user_id=? AND kind='audio' AND target=?", (interaction.user.id, aid), True)
    if dup:
        await interaction.edit_original_response(embed=toolkit.error_embed(f"You already watch that audio (ID {dup[0]['id']})."))
        return
    details = await _bot.rbx.asset_details([aid])
    dev = details.get(str(aid))
    if not dev:
        await interaction.edit_original_response(embed=toolkit.error_embed("Roblox didn't return that asset."))
        return
    state = status_to_working(dev)
    wid = await db("INSERT INTO watches (user_id, channel_id, kind, target, label, state) VALUES (?,?,?,?,?,?)",
                   (interaction.user.id, channel_of(interaction), "audio", aid, dev.get("name") or f"Audio {aid}", state))
    e = toolkit.embed("Watching audio", f"{toolkit.clip(dev.get('name'), 100)} (`{aid}`) is **{STATE_LABEL[state]}** now. "
                                        "You'll get a DM when that changes.")
    e.set_footer(text=f"{toolkit.CFG['Footer']}  |  ID {wid}  |  Checked every {CFG['WatchTick'] // 60} minutes")
    await interaction.edit_original_response(embed=e)


@watch.command(name="group", description="DM me when a group's member count changes")
@app_commands.describe(group="Group ID or link", min_change="Only alert when it moves by at least this many (default 1)")
async def watch_group(interaction: discord.Interaction, group: str, min_change: app_commands.Range[int, 1, 100000] = 1):
    await interaction.response.defer(thinking=True, ephemeral=True)
    m = re.search(r"(?:communities|groups)/(\d+)", group) or re.search(r"\d+", group)
    if not m:
        await interaction.edit_original_response(embed=toolkit.error_embed("Give a group ID or link."))
        return
    gid = int(m.group(1) if m.groups() else m.group())
    problem = await watch_quota(interaction)
    if problem:
        await interaction.edit_original_response(embed=toolkit.error_embed(problem))
        return
    d = await _bot.rbx.group_details(gid)
    if not d:
        await interaction.edit_original_response(embed=toolkit.error_embed("That group wasn't found."))
        return
    count = int(d.get("memberCount") or 0)
    wid = await db("INSERT INTO watches (user_id, channel_id, kind, target, label, state, min_change) VALUES (?,?,?,?,?,?,?)",
                   (interaction.user.id, channel_of(interaction), "group", gid, d.get("name"), str(count), min_change))
    e = toolkit.embed("Watching group", f"{toolkit.clip(d.get('name'), 100)} (`{gid}`) has **{count:,}** members. "
                                        f"You'll get a DM when that moves by {min_change:,} or more.")
    e.set_footer(text=f"{toolkit.CFG['Footer']}  |  ID {wid}  |  Checked every {CFG['WatchTick'] // 60} minutes")
    await interaction.edit_original_response(embed=e)


@watch.command(name="list", description="Show what you're watching")
async def watch_list(interaction: discord.Interaction):
    rows = await db("SELECT * FROM watches WHERE user_id=? ORDER BY id", (interaction.user.id,), True)
    if not rows:
        await interaction.response.send_message(embed=toolkit.embed("Watching", "You aren't watching anything."), ephemeral=True)
        return
    lines = []
    for r in rows:
        now = STATE_LABEL.get(r["state"], r["state"]) if r["kind"] == "audio" else f"{int(r['state']):,} members"
        lines.append(f"`{r['id']}`  {r['kind']}  {toolkit.clip(r['label'], 40)} (`{r['target']}`)  {now}")
    await interaction.response.send_message(embed=toolkit.embed("Watching", "\n".join(lines)), ephemeral=True)


@watch.command(name="stop", description="Stop watching something by its ID")
@app_commands.describe(watch_id="The ID shown in /watch list")
async def watch_stop(interaction: discord.Interaction, watch_id: int):
    mine = await db("SELECT id FROM watches WHERE id=? AND user_id=?", (watch_id, interaction.user.id), True)
    if not mine:
        await interaction.response.send_message(embed=toolkit.error_embed("You aren't watching anything with that ID."), ephemeral=True)
        return
    await db("DELETE FROM watches WHERE id=?", (watch_id,))
    await interaction.response.send_message(embed=toolkit.embed("Stopped watching"), ephemeral=True)


async def check_audio_watches(rows: list[dict]):
    ids = sorted({r["target"] for r in rows})
    details = await _bot.rbx.asset_details(ids)
    for r in rows:
        dev = details.get(str(r["target"]))
        if not dev:
            continue            # Roblox didn't answer for this one this round, try again next time
        new = status_to_working(dev)
        if new == r["state"]:
            continue
        name = toolkit.clip(dev.get("name") or r["label"], 100)
        bad = new == "broken"
        e = toolkit.embed("Audio changed", f"**{name}** (`{r['target']}`) is now **{STATE_LABEL[new]}** "
                          f"(was {STATE_LABEL.get(r['state'], r['state'])}).\n"
                          f"https://create.roblox.com/store/asset/{r['target']}",
                          toolkit.COLOR["bad"] if bad else toolkit.COLOR["base"])
        await notify(r["user_id"], r["channel_id"], e)
        await db("UPDATE watches SET state=? WHERE id=?", (new, r["id"]))


async def check_group_watches(rows: list[dict]):
    cache: dict[int, Optional[dict]] = {}
    for r in rows:
        gid = r["target"]
        if gid not in cache:
            cache[gid] = await _bot.rbx.group_details(gid)
            await asyncio.sleep(0.5)
        d = cache[gid]
        if not d:
            continue
        old, new = int(r["state"]), int(d.get("memberCount") or 0)
        if abs(new - old) < (r["min_change"] or 1):
            continue
        e = toolkit.embed("Group members changed",
                          f"**{toolkit.clip(d.get('name'), 100)}** (`{gid}`) went from {old:,} to {new:,} members "
                          f"({new - old:+,}).\nhttps://www.roblox.com/communities/{gid}")
        await notify(r["user_id"], r["channel_id"], e)
        await db("UPDATE watches SET state=? WHERE id=?", (str(new), r["id"]))


@reminder_loop.before_loop
async def _wait_reminders():
    await _bot.wait_until_ready()


@tasks.loop(seconds=CFG["WatchTick"])
async def watch_loop():
    rows = await db("SELECT * FROM watches", (), True)
    for fn, kind in ((check_audio_watches, "audio"), (check_group_watches, "group")):
        subset = [r for r in rows if r["kind"] == kind]
        if subset:
            try:
                await fn(subset)
            except RobloxError as err:
                log.warning("Watch check (%s) failed: %s", kind, err)


@watch_loop.before_loop
async def _wait_watch():
    await _bot.wait_until_ready()


# ================================================================
# REGISTER
# ================================================================

def register(bot: discord.Client) -> None:
    global _bot
    _bot = bot
    bot.tree.add_command(remind)
    bot.tree.add_command(watch)


async def start() -> None:
    """Called from setup_hook, after the Roblox client is ready."""
    await db("SCHEMA")
    for loop in (reminder_loop, watch_loop):
        if not loop.is_running():
            loop.start()


async def stop() -> None:
    for loop in (reminder_loop, watch_loop):
        loop.cancel()
