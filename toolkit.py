# DeadAir — toolkit.py
# File converters, social/link lookups and small utilities that sit next to the Roblox commands.
# Credits: @Nexesmere / EXE Development
#
# bot.py calls register(bot) once. Nothing in here needs a Roblox cookie.

import asyncio
import base64
import colorsys
import csv
import functools
import hashlib
import io
import ipaddress
import math
import os
import re
import socket
import time
import zipfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Awaitable, Callable, Literal, Optional
from urllib.parse import quote, urlparse

import aiohttp
import discord
import qrcode
import soundfile as sf
from discord import app_commands
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageSequence, UnidentifiedImageError

import audio

# ================================================================
# CONFIG
# ================================================================

CFG = {
    "Footer": "DeadAir  |  EXE Development",
    "YoutubeKey": os.getenv("YOUTUBE_API_KEY", ""),   # optional: adds views, likes, length, search
    "GithubToken": os.getenv("GITHUB_TOKEN", ""),     # optional: lifts the 60 requests/hour limit
    "MaxInputBytes": 25 * 1024 * 1024,
    "MaxOutputBytes": 9_500_000,        # stay under Discord's 10 MB upload cap
    "MaxPixels": 40_000_000,            # refuses decompression bombs before they eat the free tier's RAM
    "MaxFrames": 150,                   # animated inputs are capped so a huge gif can't stall the bot
    "MaxAudioSeconds": 20 * 60,
    "MaxSide": 4096,
    "GifMaxSide": 512,
    "HttpTimeout": 12,
    "UserAgent": "DeadAir/1.0 (Discord bot; EXE Development)",
    "Workers": 2,                       # how many heavy conversions may run at once
    "OcrKey": os.getenv("OCR_SPACE_KEY", ""),            # free key from ocr.space, needed for /ocr
    "SpotifyId": os.getenv("SPOTIFY_CLIENT_ID", ""),     # optional, richer /spotify
    "SpotifySecret": os.getenv("SPOTIFY_CLIENT_SECRET", ""),
    "UnshortenHops": 10,
}

COLOR = {"base": 0xF2F2F2, "bad": 0xE5484D}

IMAGE_FORMATS = {
    "png": ("PNG", "png"), "jpg": ("JPEG", "jpg"), "webp": ("WEBP", "webp"), "gif": ("GIF", "gif"),
    "bmp": ("BMP", "bmp"), "ico": ("ICO", "ico"), "tiff": ("TIFF", "tiff"),
}
ANIMATED_OK = {"gif", "webp"}                 # only these keep every frame
NO_ALPHA = {"jpg", "bmp"}                     # these need the transparency flattened first
INLINE_PREVIEW = {"png", "jpg", "webp", "gif"}

AUDIO_FORMATS = {
    "wav": ("WAV", "PCM_16"), "flac": ("FLAC", "PCM_16"), "ogg": ("OGG", "VORBIS"), "mp3": ("MP3", None),
}

# Link fixers: the replacement host makes Discord show a real embed (video, images, text).
EMBED_FIXERS = {
    "x.com": "fxtwitter.com", "twitter.com": "fxtwitter.com",
    "tiktok.com": "vxtiktok.com", "vm.tiktok.com": "vxtiktok.com",
    "instagram.com": "kkinstagram.com",
    "reddit.com": "rxddit.com", "old.reddit.com": "rxddit.com",
}

WEATHER_CODES = {
    0: "Clear sky", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Light showers", 81: "Showers",
    82: "Violent showers", 85: "Snow showers", 86: "Heavy snow showers", 95: "Thunderstorm",
    96: "Thunderstorm with hail", 99: "Severe thunderstorm with hail",
}

_cpu_gate = asyncio.Semaphore(CFG["Workers"])
_session: Optional[aiohttp.ClientSession] = None
Image.MAX_IMAGE_PIXELS = CFG["MaxPixels"]


class UserError(Exception):
    """Something the user can fix; shown as a red embed instead of a stack trace."""


# ================================================================
# SMALL HELPERS
# ================================================================

def embed(title: Optional[str] = None, desc: Optional[str] = None, color: int = COLOR["base"],
          url: Optional[str] = None) -> discord.Embed:
    e = discord.Embed(title=title, description=desc, color=color, url=url)
    e.set_footer(text=CFG["Footer"])
    return e


def error_embed(message: str, title: str = "Couldn't do that") -> discord.Embed:
    return embed(title, message, COLOR["bad"])


def num(n) -> str:
    return f"{int(n):,}" if isinstance(n, (int, float)) else "n/a"


def clip(text: Optional[str], limit: int) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[: limit - 3] + "..."


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} MB"


def fmt_duration(seconds: float) -> str:
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def need_url(raw: str) -> str:
    raw = raw.strip().strip("<>")
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    if not urlparse(raw).netloc or "." not in urlparse(raw).netloc:
        raise UserError("That doesn't look like a link.")
    return raw


def guarded(fn: Callable[..., Awaitable]):
    """Defers the interaction, then turns UserError into a clean embed. Other errors reach bot.py's handler."""
    @functools.wraps(fn)
    async def wrapper(interaction: discord.Interaction, *args, **kwargs):
        await interaction.response.defer(thinking=True)
        try:
            await fn(interaction, *args, **kwargs)
        except UserError as err:
            await interaction.edit_original_response(embed=error_embed(str(err)), attachments=[])
    return wrapper


async def reply(interaction: discord.Interaction, e: discord.Embed, file: Optional[discord.File] = None):
    await interaction.edit_original_response(embed=e, attachments=[file] if file else [])


# ================================================================
# HTTP
# ================================================================

async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=CFG["HttpTimeout"]),
            headers={"User-Agent": CFG["UserAgent"]})
    return _session


async def close() -> None:
    if _session and not _session.closed:
        await _session.close()


async def http_json(url: str, *, params: Optional[dict] = None, headers: Optional[dict] = None):
    """Returns parsed JSON, or None for 4xx (treated as 'not found'). Raises UserError if the site is down."""
    host = urlparse(url).netloc
    try:
        s = await get_session()
        async with s.get(url, params=params, headers=headers) as r:
            if 400 <= r.status < 500:
                return None
            if r.status != 200:
                raise UserError(f"{host} answered with status {r.status}. Try again later.")
            return await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise UserError(f"Couldn't reach {host}.")


async def read_attachment(att: discord.Attachment, kind: str) -> bytes:
    if att.size > CFG["MaxInputBytes"]:
        raise UserError(f"That file is {human_size(att.size)}. The limit is {human_size(CFG['MaxInputBytes'])}.")
    ctype = (att.content_type or "").lower()
    if ctype and not ctype.startswith(kind + "/") and not (kind == "audio" and ctype == "application/ogg"):
        raise UserError(f"That isn't an {kind} file.")
    return await att.read()


def out_file(data: bytes, name: str) -> discord.File:
    if len(data) > CFG["MaxOutputBytes"]:
        raise UserError(f"The result is {human_size(len(data))}, which is over Discord's upload limit. "
                        "Try `/compress` or `/resize` first.")
    return discord.File(io.BytesIO(data), filename=name)


async def heavy(fn: Callable, *args):
    """Runs CPU work off the event loop, a couple at a time, so the bot stays responsive."""
    async with _cpu_gate:
        return await asyncio.to_thread(fn, *args)


# ================================================================
# IMAGE ENGINE
# ================================================================

def open_image(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > CFG["MaxPixels"]:
            raise UserError("That image has too many pixels for the bot to handle.")
        if not getattr(img, "is_animated", False):
            img.load()
        return img
    except UnidentifiedImageError:
        raise UserError("I can't read that as an image.")
    except Image.DecompressionBombError:
        raise UserError("That image has too many pixels for the bot to handle.")


def flatten(img: Image.Image) -> Image.Image:
    """Puts transparency on white. JPEG and BMP can't store alpha."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    return img.convert("RGB")


def read_frames(img: Image.Image, keep_all: bool) -> tuple[list[Image.Image], list[int]]:
    if keep_all and getattr(img, "is_animated", False):
        frames, delays = [], []
        for i, f in enumerate(ImageSequence.Iterator(img)):
            if i >= CFG["MaxFrames"]:
                break
            frames.append(f.convert("RGBA"))
            delays.append(f.info.get("duration", 100))
        return frames, delays
    img.seek(0)
    return [img.convert("RGBA") if img.mode != "RGB" else img.copy()], [100]


def write_image(frames: list[Image.Image], delays: list[int], fmt: str, quality: int = 90) -> bytes:
    pil_name = IMAGE_FORMATS[fmt][0]
    if fmt in NO_ALPHA:
        frames = [flatten(f) for f in frames]
    out = io.BytesIO()
    opts: dict = {}
    if fmt in ("jpg", "webp"):
        opts["quality"] = quality
    if fmt == "png":
        opts["optimize"] = True
    if fmt == "ico":
        side = min(256, max(frames[0].size))
        frames = [frames[0].resize((side, side), Image.LANCZOS)] if frames[0].size[0] != frames[0].size[1] else frames
        opts["sizes"] = [(s, s) for s in (16, 32, 48, 64, 128, 256) if s <= max(frames[0].size)]
    if len(frames) > 1 and fmt in ANIMATED_OK:
        frames[0].save(out, pil_name, save_all=True, append_images=frames[1:], duration=delays, loop=0,
                       **({"disposal": 2} if fmt == "gif" else {}), **opts)
    else:
        frames[0].save(out, pil_name, **opts)
    return out.getvalue()


def convert_image(data: bytes, fmt: str) -> tuple[bytes, int]:
    img = open_image(data)
    frames, delays = read_frames(img, fmt in ANIMATED_OK)
    return write_image(frames, delays, fmt), len(frames)


def resize_image(data: bytes, width: Optional[int], height: Optional[int], scale: Optional[int],
                 fmt: str) -> tuple[bytes, tuple[int, int]]:
    img = open_image(data)
    w, h = img.size
    if scale:
        nw, nh = round(w * scale / 100), round(h * scale / 100)
    elif width and height:
        nw, nh = width, height
    elif width:
        nw, nh = width, round(h * width / w)
    elif height:
        nw, nh = round(w * height / h), height
    else:
        raise UserError("Give a width, a height, or a scale percent.")
    if not (1 <= nw <= CFG["MaxSide"] and 1 <= nh <= CFG["MaxSide"]):
        raise UserError(f"The result has to be between 1 and {CFG['MaxSide']} pixels on each side.")
    frames, delays = read_frames(img, fmt in ANIMATED_OK)
    frames = [f.resize((nw, nh), Image.LANCZOS) for f in frames]
    return write_image(frames, delays, fmt), (nw, nh)


def compress_image(data: bytes, target: int) -> tuple[bytes, str, int]:
    """Lowers quality first, then shrinks the picture, until it fits. Returns bytes, extension, quality used."""
    img = open_image(data)
    frames, _ = read_frames(img, False)
    base = frames[0]
    has_alpha = base.mode == "RGBA" and base.getchannel("A").getextrema()[0] < 255
    fmt = "webp" if has_alpha else "jpg"
    scale, best = 1.0, b""
    while True:
        work = base if scale == 1.0 else base.resize(
            (max(1, round(base.width * scale)), max(1, round(base.height * scale))), Image.LANCZOS)
        for q in (88, 78, 68, 58, 48, 38, 28):
            best = write_image([work], [100], fmt, q)
            if len(best) <= target:
                return best, fmt, q
        scale *= 0.8
        if scale < 0.1:
            return best, fmt, 28


def make_gif(datas: list[bytes], delay: int, loop: bool) -> bytes:
    first = open_image(datas[0])
    side = min(CFG["GifMaxSide"], max(first.size))
    ratio = side / max(first.size)
    size = (max(1, round(first.width * ratio)), max(1, round(first.height * ratio)))
    frames = []
    for raw in datas:
        img = open_image(raw)
        img.seek(0)
        frames.append(flatten(img).resize(size, Image.LANCZOS))
    out = io.BytesIO()
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:], duration=delay,
                   loop=0 if loop else 1, optimize=True, disposal=2)
    return out.getvalue()


# ================================================================
# IMAGE COMMANDS
# ================================================================

def stem(att: discord.Attachment) -> str:
    return re.sub(r"[^\w\-]+", "_", att.filename.rsplit(".", 1)[0])[:40] or "file"


def image_result(title: str, desc: str, data: bytes, name: str, fmt: str) -> tuple[discord.Embed, discord.File]:
    e = embed(title, desc)
    f = out_file(data, name)
    if fmt in INLINE_PREVIEW:
        e.set_image(url=f"attachment://{name}")
    return e, f


@app_commands.command(name="convert", description="Convert an image to another format (png, jpg, webp, gif, ico...)")
@app_commands.describe(file="The image to convert", format="What to turn it into")
@guarded
async def convert_cmd(interaction: discord.Interaction, file: discord.Attachment,
                      format: Literal["png", "jpg", "webp", "gif", "bmp", "ico", "tiff"]):
    data = await read_attachment(file, "image")
    out, frames = await heavy(convert_image, data, format)
    name = f"{stem(file)}.{IMAGE_FORMATS[format][1]}"
    note = f"{frames} frames kept." if frames > 1 else ""
    e, f = image_result("Converted", f"`{file.filename}` to `{format}`. {human_size(len(data))} to "
                                     f"{human_size(len(out))}. {note}".strip(), out, name, format)
    await reply(interaction, e, f)


@app_commands.command(name="resize", description="Resize an image by width, height, or percent")
@app_commands.describe(file="The image to resize", width="New width in pixels", height="New height in pixels",
                       percent="Scale in percent (overrides width and height)")
@guarded
async def resize_cmd(interaction: discord.Interaction, file: discord.Attachment,
                     width: Optional[app_commands.Range[int, 1, 4096]] = None,
                     height: Optional[app_commands.Range[int, 1, 4096]] = None,
                     percent: Optional[app_commands.Range[int, 1, 800]] = None):
    data = await read_attachment(file, "image")
    ext = (file.filename.rsplit(".", 1)[-1] or "png").lower()
    fmt = {"jpeg": "jpg", "tif": "tiff"}.get(ext, ext)
    fmt = fmt if fmt in IMAGE_FORMATS else "png"
    out, (w, h) = await heavy(resize_image, data, width, height, percent, fmt)
    name = f"{stem(file)}_{w}x{h}.{IMAGE_FORMATS[fmt][1]}"
    e, f = image_result("Resized", f"`{file.filename}` is now {w} x {h}. {human_size(len(out))}.", out, name, fmt)
    await reply(interaction, e, f)


@app_commands.command(name="compress", description="Shrink an image's file size so it fits an upload limit")
@app_commands.describe(file="The image to compress", target_mb="Size to aim for in MB (default 1)")
@guarded
async def compress_cmd(interaction: discord.Interaction, file: discord.Attachment,
                       target_mb: app_commands.Range[float, 0.05, 9.0] = 1.0):
    data = await read_attachment(file, "image")
    out, ext, quality = await heavy(compress_image, data, int(target_mb * 1024 * 1024))
    name = f"{stem(file)}_small.{ext}"
    e, f = image_result("Compressed", f"{human_size(len(data))} to {human_size(len(out))} "
                                      f"(quality {quality}, `{ext}`).", out, name, ext)
    await reply(interaction, e, f)


@app_commands.command(name="gifmaker", description="Turn up to 6 images into an animated GIF")
@app_commands.describe(image1="First frame", image2="Second frame", image3="Third frame", image4="Fourth frame",
                       image5="Fifth frame", image6="Sixth frame",
                       delay="Milliseconds per frame (default 500)", loop="Repeat forever (default yes)")
@guarded
async def gifmaker_cmd(interaction: discord.Interaction, image1: discord.Attachment,
                       image2: Optional[discord.Attachment] = None, image3: Optional[discord.Attachment] = None,
                       image4: Optional[discord.Attachment] = None, image5: Optional[discord.Attachment] = None,
                       image6: Optional[discord.Attachment] = None,
                       delay: app_commands.Range[int, 20, 5000] = 500, loop: bool = True):
    picked = [a for a in (image1, image2, image3, image4, image5, image6) if a]
    if len(picked) < 2:
        raise UserError("A GIF needs at least two images. Attach more in `image2`, `image3`, ...")
    datas = [await read_attachment(a, "image") for a in picked]
    out = await heavy(make_gif, datas, delay, loop)
    name = f"{stem(image1)}_anim.gif"
    e, f = image_result("GIF made", f"{len(picked)} frames, {delay} ms each. {human_size(len(out))}.", out, name, "gif")
    await reply(interaction, e, f)


# ================================================================
# AUDIO COMMANDS
# ================================================================

def convert_audio(data: bytes, fmt: str) -> tuple[bytes, float]:
    """Streams in blocks so a long track never sits fully decoded in memory."""
    container, subtype = AUDIO_FORMATS[fmt]
    out = io.BytesIO()
    try:
        with sf.SoundFile(io.BytesIO(data)) as src:
            if src.frames / max(src.samplerate, 1) > CFG["MaxAudioSeconds"]:
                raise UserError(f"That audio is longer than {CFG['MaxAudioSeconds'] // 60} minutes.")
            kwargs = {"subtype": subtype} if subtype else {}
            with sf.SoundFile(out, "w", samplerate=src.samplerate, channels=src.channels,
                              format=container, **kwargs) as dst:
                while True:
                    block = src.read(65536, dtype="float32", always_2d=True)
                    if len(block) == 0:
                        break
                    dst.write(block)
            seconds = src.frames / src.samplerate
    except sf.LibsndfileError:
        raise UserError("I can't read that audio. Supported inputs: mp3, wav, flac, ogg.")
    return out.getvalue(), seconds


@app_commands.command(name="audioconvert", description="Convert audio between mp3, wav, flac and ogg")
@app_commands.describe(file="The audio file", format="What to turn it into")
@guarded
async def audioconvert_cmd(interaction: discord.Interaction, file: discord.Attachment,
                           format: Literal["mp3", "wav", "flac", "ogg"]):
    data = await read_attachment(file, "audio")
    out, seconds = await heavy(convert_audio, data, format)
    name = f"{stem(file)}.{format}"
    e = embed("Audio converted", f"`{file.filename}` to `{format}`. {fmt_duration(seconds)} long, "
                                 f"{human_size(len(data))} to {human_size(len(out))}.")
    await reply(interaction, e, out_file(out, name))


@app_commands.command(name="waveform", description="Loudness stats and a waveform image for any audio file")
@app_commands.describe(file="mp3, wav, flac or ogg")
@guarded
async def waveform_cmd(interaction: discord.Interaction, file: discord.Attachment):
    data = await read_attachment(file, "audio")

    def work() -> tuple[dict, bytes]:
        stats = audio.analyze(data)
        return stats, audio.render_waveform(stats)

    try:
        stats, png = await heavy(work)
    except (sf.LibsndfileError, ValueError):
        raise UserError("I can't read that audio. Supported: mp3, wav, flac, ogg.")
    e = embed(clip(file.filename, 200))
    e.add_field(name="Length", value=fmt_duration(stats["duration"]))
    e.add_field(name="Sample rate", value=f"{stats['sample_rate']:,} Hz")
    e.add_field(name="Channels", value=str(stats["channels"]))
    e.add_field(name="Peak", value=f"{stats['peak_db']:.1f} dBFS")
    e.add_field(name="Average (RMS)", value=f"{stats['rms_db']:.1f} dBFS")
    e.add_field(name="Bitrate", value=f"{stats['bitrate_kbps']:.0f} kbps")
    e.set_image(url="attachment://waveform.png")
    await reply(interaction, e, out_file(png, "waveform.png"))


# ================================================================
# SOCIAL LOOKUPS
# ================================================================

YT_ID = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([\w-]{11})")
ISO_DUR = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def iso_seconds(value: str) -> int:
    h, m, s = (int(x or 0) for x in ISO_DUR.match(value or "").groups())
    return h * 3600 + m * 60 + s


async def youtube_api(video_id: Optional[str], query: Optional[str]) -> Optional[dict]:
    key = CFG["YoutubeKey"]
    if not video_id:
        found = await http_json("https://www.googleapis.com/youtube/v3/search", params={
            "part": "snippet", "q": query, "type": "video", "maxResults": 1, "key": key})
        items = (found or {}).get("items") or []
        if not items:
            return None
        video_id = items[0]["id"]["videoId"]
    data = await http_json("https://www.googleapis.com/youtube/v3/videos", params={
        "part": "snippet,statistics,contentDetails", "id": video_id, "key": key})
    items = (data or {}).get("items") or []
    return items[0] if items else None


@app_commands.command(name="youtube", description="Look up a YouTube video by link (or by search words if a key is set)")
@app_commands.describe(video="YouTube link, or search words")
@guarded
async def youtube_cmd(interaction: discord.Interaction, video: str):
    m = YT_ID.search(video)
    vid = m.group(1) if m else None
    if CFG["YoutubeKey"]:
        item = await youtube_api(vid, None if vid else video)
        if not item:
            raise UserError("No video found.")
        sn, st = item["snippet"], item.get("statistics", {})
        vid = item["id"]
        e = embed(clip(sn["title"], 250), clip(sn.get("description"), 300), url=f"https://youtu.be/{vid}")
        e.add_field(name="Channel", value=f"[{clip(sn['channelTitle'], 40)}](https://www.youtube.com/channel/{sn['channelId']})")
        e.add_field(name="Length", value=fmt_duration(iso_seconds(item["contentDetails"]["duration"])))
        e.add_field(name="Published", value=f"<t:{int(discord.utils.parse_time(sn['publishedAt']).timestamp())}:D>")
        e.add_field(name="Views", value=num(int(st["viewCount"])) if "viewCount" in st else "hidden")
        e.add_field(name="Likes", value=num(int(st["likeCount"])) if "likeCount" in st else "hidden")
        e.add_field(name="Comments", value=num(int(st["commentCount"])) if "commentCount" in st else "off")
        e.set_image(url=f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg")
        await reply(interaction, e)
        return
    if not vid:
        raise UserError("Paste a YouTube link. Searching by words needs `YOUTUBE_API_KEY` set on the bot.")
    data = await http_json("https://www.youtube.com/oembed", params={
        "url": f"https://www.youtube.com/watch?v={vid}", "format": "json"})
    if not data:
        raise UserError("That video is private, removed, or the link is wrong.")
    e = embed(clip(data["title"], 250), url=f"https://youtu.be/{vid}")
    e.add_field(name="Channel", value=f"[{clip(data['author_name'], 40)}]({data['author_url']})")
    e.set_image(url=f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg")
    e.set_footer(text=f"{CFG['Footer']}  |  Add YOUTUBE_API_KEY for views and likes")
    await reply(interaction, e)


@app_commands.command(name="tiktok", description="Look up a TikTok video by link")
@app_commands.describe(link="TikTok video link")
@guarded
async def tiktok_cmd(interaction: discord.Interaction, link: str):
    url = need_url(link)
    if "tiktok.com" not in urlparse(url).netloc:
        raise UserError("That isn't a TikTok link.")
    data = await http_json("https://www.tiktok.com/oembed", params={"url": url})
    if not data or data.get("code"):
        raise UserError("TikTok didn't return that video. It may be private or removed.")
    e = embed(clip(data.get("title") or "TikTok video", 250), url=url)
    e.add_field(name="Creator", value=f"[{clip(data.get('author_name'), 40)}]({data.get('author_url')})")
    if data.get("thumbnail_url"):
        e.set_image(url=data["thumbnail_url"])
    await reply(interaction, e)


@app_commands.command(name="tweet", description="Look up a post on X (Twitter) by link")
@app_commands.describe(link="Link to the post")
@guarded
async def tweet_cmd(interaction: discord.Interaction, link: str):
    m = re.search(r"status(?:es)?/(\d+)", link)
    if not m:
        raise UserError("That doesn't look like a link to a post.")
    data = await http_json(f"https://api.fxtwitter.com/i/status/{m.group(1)}")
    tw = (data or {}).get("tweet")
    if not tw:
        raise UserError("That post wasn't found. It may be deleted or from a private account.")
    au = tw["author"]
    e = embed(f"{clip(au['name'], 60)} (@{au['screen_name']})", clip(tw.get("text"), 3500), url=tw["url"])
    e.set_thumbnail(url=au.get("avatar_url"))
    e.add_field(name="Likes", value=num(tw.get("likes")))
    e.add_field(name="Reposts", value=num(tw.get("retweets")))
    e.add_field(name="Replies", value=num(tw.get("replies")))
    if tw.get("views") is not None:
        e.add_field(name="Views", value=num(tw["views"]))
    if tw.get("created_timestamp"):
        e.add_field(name="Posted", value=f"<t:{int(tw['created_timestamp'])}:F>")
    media = tw.get("media") or {}
    if media.get("photos"):
        e.set_image(url=media["photos"][0]["url"])
    elif media.get("videos"):
        e.add_field(name="Video", value=f"[Open video]({media['videos'][0]['url']})", inline=False)
        if media["videos"][0].get("thumbnail_url"):
            e.set_image(url=media["videos"][0]["thumbnail_url"])
    await reply(interaction, e)


@app_commands.command(name="embedfix", description="Fix a TikTok, Instagram, X or Reddit link so Discord embeds it")
@app_commands.describe(link="The link that won't embed")
@guarded
async def embedfix_cmd(interaction: discord.Interaction, link: str):
    url = need_url(link)
    parts = urlparse(url)
    host = parts.netloc.lower().removeprefix("www.")
    new_host = EMBED_FIXERS.get(host) or EMBED_FIXERS.get(".".join(host.split(".")[-2:]))
    if not new_host:
        raise UserError("I can fix links from X, TikTok, Instagram and Reddit.")
    fixed = parts._replace(netloc=new_host).geturl()
    await interaction.edit_original_response(content=fixed, embed=None, attachments=[])


# ================================================================
# UTILITIES
# ================================================================

@app_commands.command(name="github", description="Look up a GitHub user or repo")
@app_commands.describe(target="username, owner/repo, or a github.com link")
@guarded
async def github_cmd(interaction: discord.Interaction, target: str):
    path = re.sub(r"^https?://(www\.)?github\.com/", "", target.strip()).strip("/")
    parts = [p for p in path.split("/") if p][:2]
    if not parts:
        raise UserError("Give a username or `owner/repo`.")
    headers = {"Accept": "application/vnd.github+json"}
    if CFG["GithubToken"]:
        headers["Authorization"] = f"Bearer {CFG['GithubToken']}"
    base = "https://api.github.com"
    data = await http_json(f"{base}/repos/{parts[0]}/{parts[1]}" if len(parts) == 2 else f"{base}/users/{parts[0]}",
                           headers=headers)
    if not data or "message" in data:
        raise UserError("Nothing found, or GitHub's rate limit was hit.")
    if len(parts) == 2:
        e = embed(data["full_name"], clip(data.get("description"), 300), url=data["html_url"])
        e.add_field(name="Stars", value=num(data["stargazers_count"]))
        e.add_field(name="Forks", value=num(data["forks_count"]))
        e.add_field(name="Open issues", value=num(data["open_issues_count"]))
        e.add_field(name="Language", value=data.get("language") or "n/a")
        e.add_field(name="License", value=(data.get("license") or {}).get("spdx_id") or "none")
        e.add_field(name="Updated", value=f"<t:{int(discord.utils.parse_time(data['pushed_at']).timestamp())}:R>")
        e.set_thumbnail(url=data["owner"]["avatar_url"])
    else:
        e = embed(data.get("name") or data["login"], clip(data.get("bio"), 300), url=data["html_url"])
        e.add_field(name="Username", value=data["login"])
        e.add_field(name="Repos", value=num(data["public_repos"]))
        e.add_field(name="Followers", value=num(data["followers"]))
        e.add_field(name="Joined", value=f"<t:{int(discord.utils.parse_time(data['created_at']).timestamp())}:D>")
        if data.get("blog"):
            e.add_field(name="Website", value=clip(data["blog"], 100))
        e.set_thumbnail(url=data["avatar_url"])
    await reply(interaction, e)


@app_commands.command(name="weather", description="Current weather and today's range for a place")
@app_commands.describe(place="City name, like Winston-Salem or Tokyo")
@guarded
async def weather_cmd(interaction: discord.Interaction, place: str):
    geo = await http_json("https://geocoding-api.open-meteo.com/v1/search", params={"name": place, "count": 1})
    hits = (geo or {}).get("results")
    if not hits:
        raise UserError(f"No place found for `{clip(place, 60)}`.")
    g = hits[0]
    wx = await http_json("https://api.open-meteo.com/v1/forecast", params={
        "latitude": g["latitude"], "longitude": g["longitude"], "timezone": "auto",
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min"})
    if not wx:
        raise UserError("The weather service didn't answer.")
    cur, day = wx["current"], wx["daily"]

    def both(c: float) -> str:
        return f"{c:.0f} C / {c * 9 / 5 + 32:.0f} F"

    where = ", ".join(x for x in (g["name"], g.get("admin1"), g.get("country")) if x)
    e = embed(where, WEATHER_CODES.get(cur["weather_code"], "Unknown conditions"))
    e.add_field(name="Now", value=both(cur["temperature_2m"]))
    e.add_field(name="Feels like", value=both(cur["apparent_temperature"]))
    e.add_field(name="Humidity", value=f"{cur['relative_humidity_2m']}%")
    e.add_field(name="Wind", value=f"{cur['wind_speed_10m']:.0f} km/h")
    e.add_field(name="Today", value=f"{both(day['temperature_2m_min'][0])} to {both(day['temperature_2m_max'][0])}")
    await reply(interaction, e)


@app_commands.command(name="define", description="Dictionary definition of an English word")
@app_commands.describe(word="The word to look up")
@guarded
async def define_cmd(interaction: discord.Interaction, word: str):
    data = await http_json(f"https://api.dictionaryapi.dev/api/v2/entries/en/{quote(word.strip().lower())}")
    if not isinstance(data, list) or not data:
        raise UserError(f"No definition found for `{clip(word, 40)}`.")
    entry = data[0]
    e = embed(entry["word"], entry.get("phonetic") or None)
    for meaning in entry["meanings"][:3]:
        lines = []
        for d in meaning["definitions"][:2]:
            lines.append(f"{clip(d['definition'], 220)}" + (f"\n*{clip(d['example'], 120)}*" if d.get("example") else ""))
        e.add_field(name=meaning["partOfSpeech"], value="\n".join(lines), inline=False)
    await reply(interaction, e)


@app_commands.command(name="robloxstatus", description="Is Roblox up right now?")
@guarded
async def robloxstatus_cmd(interaction: discord.Interaction):
    data = await http_json("https://status.roblox.com/api/v2/summary.json")
    if not data:
        raise UserError("Roblox's status page didn't answer.")
    bad = [c for c in data.get("components", []) if c["status"] != "operational"]
    e = embed("Roblox status", data["status"]["description"], url="https://status.roblox.com")
    if bad:
        e.add_field(name="Affected", value="\n".join(f"{clip(c['name'], 50)}: {c['status'].replace('_', ' ')}"
                                                     for c in bad[:12]), inline=False)
    for inc in data.get("incidents", [])[:3]:
        e.add_field(name=clip(inc["name"], 100), value=inc["status"].replace("_", " ").title(), inline=False)
    await reply(interaction, e)


@app_commands.command(name="qr", description="Make a QR code from text or a link")
@app_commands.describe(text="What the QR code should contain")
@guarded
async def qr_cmd(interaction: discord.Interaction, text: app_commands.Range[str, 1, 1000]):
    def work() -> bytes:
        img = qrcode.make(text, box_size=10, border=3)
        out = io.BytesIO()
        img.save(out, "PNG")
        return out.getvalue()

    png = await heavy(work)
    e = embed("QR code", clip(text, 200))
    e.set_image(url="attachment://qr.png")
    await reply(interaction, e, out_file(png, "qr.png"))


@app_commands.command(name="color", description="Show a color swatch with its RGB, HSL and hex values")
@app_commands.describe(hex="Hex code like #F2F2F2 or fff")
@guarded
async def color_cmd(interaction: discord.Interaction, hex: str):
    m = re.fullmatch(r"#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", hex.strip())
    if not m:
        raise UserError("Give a hex color like `#F2F2F2` or `fff`.")
    h = m.group(1)
    h = "".join(c * 2 for c in h) if len(h) == 3 else h
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    hue, light, sat = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    img = Image.new("RGB", (400, 160), (r, g, b))
    out = io.BytesIO()
    img.save(out, "PNG")
    e = embed(f"#{h.upper()}", color=int(h, 16))
    e.add_field(name="RGB", value=f"{r}, {g}, {b}")
    e.add_field(name="HSL", value=f"{round(hue * 360)}, {round(sat * 100)}%, {round(light * 100)}%")
    e.set_image(url="attachment://color.png")
    await reply(interaction, e, out_file(out.getvalue(), "color.png"))


# ================================================================
# CSV EXPORT (used by /search and /bulkids in bot.py)
# ================================================================

def _safe_cell(v):
    """Spreadsheets run cells that start with = + - @ as formulas, and audio names are user-made text."""
    return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def _zip_bytes(name: str, data: bytes) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, data)
    return out.getvalue()


def csv_file(header: list[str], rows, filename: str) -> tuple[discord.File, str]:
    """Returns (file, note). Zips the CSV when it is too big, and trims rows only as a last resort."""
    rows = [[_safe_cell(c) for c in r] for r in rows]

    def build(rs) -> bytes:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        w.writerows(rs)
        return buf.getvalue().encode("utf-8-sig")   # BOM so Excel reads names with accents correctly

    cap = CFG["MaxOutputBytes"]
    data = build(rows)
    if len(data) <= cap:
        return discord.File(io.BytesIO(data), filename=filename), ""
    zname = filename.rsplit(".", 1)[0] + ".zip"
    packed = _zip_bytes(filename, data)
    if len(packed) <= cap:
        return discord.File(io.BytesIO(packed), filename=zname), "Zipped because the CSV was large."
    keep = max(1, int(len(rows) * cap / len(packed) * 0.8))
    packed = _zip_bytes(filename, build(rows[:keep]))
    return (discord.File(io.BytesIO(packed), filename=zname),
            f"Zipped, and only the first {keep:,} of {len(rows):,} rows fit under Discord's size limit.")


# ================================================================
# MORE IMAGE TOOLS
# ================================================================

def input_format(att: discord.Attachment) -> str:
    ext = (att.filename.rsplit(".", 1)[-1] or "png").lower()
    ext = {"jpeg": "jpg", "tif": "tiff"}.get(ext, ext)
    return ext if ext in IMAGE_FORMATS else "png"


def transform_image(data: bytes, fmt: str, fn: Callable[[Image.Image], Image.Image]) -> bytes:
    img = open_image(data)
    frames, delays = read_frames(img, fmt in ANIMATED_OK)
    return write_image([fn(f) for f in frames], delays, fmt)


EFFECTS = ["grayscale", "invert", "sepia", "blur", "sharpen", "pixelate", "mirror", "flip",
           "rotate90", "rotate180", "rotate270"]


def apply_effect(img: Image.Image, effect: str, strength: int) -> Image.Image:
    rgba = img.convert("RGBA")
    r, g, b, a = rgba.split()
    rgb = Image.merge("RGB", (r, g, b))
    if effect == "grayscale":
        out = ImageOps.grayscale(rgb).convert("RGB")
    elif effect == "invert":
        out = ImageOps.invert(rgb)
    elif effect == "sepia":
        gray = ImageOps.grayscale(rgb)
        out = ImageOps.colorize(gray, black=(30, 18, 8), white=(255, 240, 214), mid=(160, 110, 66))
    elif effect == "blur":
        out = rgb.filter(ImageFilter.GaussianBlur(radius=strength * max(rgba.size) / 400))
    elif effect == "sharpen":
        out = rgb.filter(ImageFilter.UnsharpMask(radius=2, percent=50 + strength * 40, threshold=2))
    elif effect == "pixelate":
        block = max(2, max(rgba.size) * strength // 200)
        small = rgb.resize((max(1, rgba.width // block), max(1, rgba.height // block)), Image.BILINEAR)
        out = small.resize(rgba.size, Image.NEAREST)
    elif effect == "mirror":
        return ImageOps.mirror(rgba)
    elif effect == "flip":
        return ImageOps.flip(rgba)
    else:
        angle = {"rotate90": Image.ROTATE_90, "rotate180": Image.ROTATE_180, "rotate270": Image.ROTATE_270}[effect]
        return rgba.transpose(angle)
    out = out.convert("RGBA")
    out.putalpha(a)
    return out


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    return lines + [line] if line else lines


def draw_caption(img: Image.Image, top: str, bottom: str) -> Image.Image:
    img = img.convert("RGBA")
    draw = ImageDraw.Draw(img)
    size = max(18, img.width // 10)
    font = ImageFont.load_default(size)
    stroke = max(2, size // 12)
    margin = max(6, img.height // 40)

    def block(text: str, from_top: bool):
        lines = wrap_text(draw, text.upper(), font, img.width - 2 * margin)
        height = len(lines) * int(size * 1.1)
        y = margin if from_top else img.height - margin - height
        for ln in lines:
            w = draw.textlength(ln, font=font)
            draw.text(((img.width - w) / 2, y), ln, font=font, fill="white", stroke_width=stroke, stroke_fill="black")
            y += int(size * 1.1)

    if top:
        block(top, True)
    if bottom:
        block(bottom, False)
    return img


def split_frames_to_sheet(frames: list[Image.Image], cols: int, cell: tuple[int, int]) -> Image.Image:
    rows = math.ceil(len(frames) / cols)
    w, h = cell
    if w * cols * h * rows > CFG["MaxPixels"]:
        raise UserError("That sheet would be too large. Use `/resize` on the image first or use fewer frames.")
    sheet = Image.new("RGBA", (w * cols, h * rows), (0, 0, 0, 0))
    for i, f in enumerate(frames):
        fit = ImageOps.contain(f.convert("RGBA"), cell, Image.LANCZOS)
        sheet.paste(fit, ((i % cols) * w + (w - fit.width) // 2, (i // cols) * h + (h - fit.height) // 2))
    return sheet


def png_bytes(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


@app_commands.command(name="filter", description="Apply an effect to an image: grayscale, blur, pixelate, rotate...")
@app_commands.describe(file="The image", effect="What to do to it", strength="1 to 10 for blur, sharpen and pixelate (default 5)")
@guarded
async def filter_cmd(interaction: discord.Interaction, file: discord.Attachment,
                     effect: Literal["grayscale", "invert", "sepia", "blur", "sharpen", "pixelate", "mirror",
                                     "flip", "rotate90", "rotate180", "rotate270"],
                     strength: app_commands.Range[int, 1, 10] = 5):
    data = await read_attachment(file, "image")
    fmt = input_format(file)
    out = await heavy(transform_image, data, fmt, lambda f: apply_effect(f, effect, strength))
    name = f"{stem(file)}_{effect}.{IMAGE_FORMATS[fmt][1]}"
    e, f = image_result("Filter applied", f"`{effect}` on `{file.filename}`. {human_size(len(out))}.", out, name, fmt)
    await reply(interaction, e, f)


@app_commands.command(name="caption", description="Add meme-style text to an image. Use | to split top and bottom text")
@app_commands.describe(file="The image", text="Text for the top, or 'top text | bottom text'")
@guarded
async def caption_cmd(interaction: discord.Interaction, file: discord.Attachment,
                      text: app_commands.Range[str, 1, 200]):
    top, _, bottom = text.partition("|")
    top, bottom = top.strip(), bottom.strip()
    data = await read_attachment(file, "image")
    fmt = input_format(file)
    out = await heavy(transform_image, data, fmt, lambda f: draw_caption(f, top, bottom))
    name = f"{stem(file)}_caption.{IMAGE_FORMATS[fmt][1]}"
    e, f = image_result("Captioned", f"{human_size(len(out))}.", out, name, fmt)
    await reply(interaction, e, f)


@app_commands.command(name="gifframes", description="Split a GIF or animated WebP into a sprite sheet or a zip of frames")
@app_commands.describe(file="The animated image", mode="One sheet image, or a zip with every frame",
                       columns="Columns for the sheet (default: square-ish)")
@guarded
async def gifframes_cmd(interaction: discord.Interaction, file: discord.Attachment,
                        mode: Literal["sheet", "zip"] = "sheet",
                        columns: Optional[app_commands.Range[int, 1, 30]] = None):
    data = await read_attachment(file, "image")

    def work():
        img = open_image(data)
        frames, _ = read_frames(img, True)
        if mode == "zip":
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
                for i, f in enumerate(frames):
                    z.writestr(f"frame_{i + 1:03d}.png", png_bytes(f))
            return out.getvalue(), len(frames), "zip"
        cols = columns or math.ceil(math.sqrt(len(frames)))
        return png_bytes(split_frames_to_sheet(frames, cols, frames[0].size)), len(frames), "png"

    out, count, ext = await heavy(work)
    name = f"{stem(file)}_frames.{ext}"
    e = embed("Frames extracted", f"{count} frames from `{file.filename}`. {human_size(len(out))}.")
    if ext == "png":
        e.set_image(url=f"attachment://{name}")
    await reply(interaction, e, out_file(out, name))


@app_commands.command(name="spritesheet", description="Pack 2 to 6 images into one sprite sheet")
@app_commands.describe(image1="First image", image2="Second image", image3="Third", image4="Fourth", image5="Fifth",
                       image6="Sixth", columns="Columns (default: all in one row up to 3, then a grid)")
@guarded
async def spritesheet_cmd(interaction: discord.Interaction, image1: discord.Attachment,
                          image2: Optional[discord.Attachment] = None, image3: Optional[discord.Attachment] = None,
                          image4: Optional[discord.Attachment] = None, image5: Optional[discord.Attachment] = None,
                          image6: Optional[discord.Attachment] = None,
                          columns: Optional[app_commands.Range[int, 1, 6]] = None):
    picked = [a for a in (image1, image2, image3, image4, image5, image6) if a]
    if len(picked) < 2:
        raise UserError("A sheet needs at least two images.")
    datas = [await read_attachment(a, "image") for a in picked]

    def work():
        frames = []
        for raw in datas:
            im = open_image(raw)
            im.seek(0)
            frames.append(im.convert("RGBA"))
        cell = frames[0].size
        cols = columns or (len(frames) if len(frames) <= 3 else math.ceil(len(frames) / 2))
        return png_bytes(split_frames_to_sheet(frames, cols, cell))

    out = await heavy(work)
    e, f = image_result("Sprite sheet", f"{len(picked)} images, each fitted to the size of the first. "
                                        f"{human_size(len(out))}.", out, "spritesheet.png", "png")
    await reply(interaction, e, f)


@app_commands.command(name="ocr", description="Read the text out of a screenshot or photo")
@app_commands.describe(file="The image", language="Language of the text (default English)")
@guarded
async def ocr_cmd(interaction: discord.Interaction, file: discord.Attachment,
                  language: Literal["eng", "spa", "fre", "ger", "por", "ita", "rus", "jpn", "kor", "chs"] = "eng"):
    if not CFG["OcrKey"]:
        raise UserError("`/ocr` needs a free key from ocr.space. Set it as `OCR_SPACE_KEY` on the bot.")
    data = await read_attachment(file, "image")
    small, _, _ = await heavy(compress_image, data, 950_000)   # the free plan only takes files under 1 MB
    form = aiohttp.FormData()
    form.add_field("apikey", CFG["OcrKey"])
    form.add_field("language", language)
    form.add_field("OCREngine", "2")
    form.add_field("file", small, filename="image.jpg", content_type="image/jpeg")
    try:
        s = await get_session()
        async with s.post("https://api.ocr.space/parse/image", data=form,
                          timeout=aiohttp.ClientTimeout(total=40)) as r:
            res = await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise UserError("Couldn't reach the OCR service.")
    if res.get("IsErroredOnProcessing"):
        raise UserError(clip(str(res.get("ErrorMessage") or "The OCR service couldn't read that image."), 300))
    text = "\n".join(p.get("ParsedText", "") for p in res.get("ParsedResults") or []).strip()
    if not text:
        raise UserError("No text was found in that image.")
    if len(text) <= 3900:
        await reply(interaction, embed("Text found", f"```\n{text}\n```"))
    else:
        await reply(interaction, embed("Text found", f"{len(text):,} characters, attached as a file."),
                    discord.File(io.BytesIO(text.encode()), filename="ocr.txt"))


# ================================================================
# AUDIO TRIM
# ================================================================

def parse_clock(raw: str) -> float:
    """'83', '1:23', '1:23.5' or '1:02:03' to seconds."""
    parts = raw.strip().split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise UserError(f"`{clip(raw, 20)}` isn't a time. Use seconds or mm:ss, like `83` or `1:23.5`.")
    if not 1 <= len(nums) <= 3 or any(n < 0 for n in nums):
        raise UserError("Use seconds or mm:ss, like `83` or `1:23.5`.")
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


CONTAINER_TO_EXT = {v[0]: k for k, v in AUDIO_FORMATS.items()}


def trim_audio(data: bytes, start: float, end: Optional[float]) -> tuple[bytes, str, float]:
    try:
        with sf.SoundFile(io.BytesIO(data)) as src:
            sr = src.samplerate
            total = src.frames / sr
            if total > CFG["MaxAudioSeconds"]:
                raise UserError(f"That audio is longer than {CFG['MaxAudioSeconds'] // 60} minutes.")
            stop = min(end, total) if end is not None else total
            if start >= stop:
                raise UserError(f"The start has to be before the end. The file is {fmt_duration(total)} long.")
            ext = CONTAINER_TO_EXT.get(src.format, "wav")
            container, subtype = AUDIO_FORMATS[ext]
            kwargs = {"subtype": subtype} if subtype else {}
            out = io.BytesIO()
            src.seek(int(start * sr))
            left = int((stop - start) * sr)
            with sf.SoundFile(out, "w", samplerate=sr, channels=src.channels, format=container, **kwargs) as dst:
                while left > 0:
                    block = src.read(min(65536, left), dtype="float32", always_2d=True)
                    if len(block) == 0:
                        break
                    dst.write(block)
                    left -= len(block)
    except sf.LibsndfileError:
        raise UserError("I can't read that audio. Supported inputs: mp3, wav, flac, ogg.")
    return out.getvalue(), ext, stop - start


@app_commands.command(name="trim", description="Cut an audio file to a start and end time")
@app_commands.describe(file="mp3, wav, flac or ogg", start="Where to start, like 12 or 0:12.5",
                       end="Where to stop (default: the end)")
@guarded
async def trim_cmd(interaction: discord.Interaction, file: discord.Attachment, start: str = "0",
                   end: Optional[str] = None):
    data = await read_attachment(file, "audio")
    out, ext, seconds = await heavy(trim_audio, data, parse_clock(start), parse_clock(end) if end else None)
    e = embed("Audio trimmed", f"{fmt_duration(seconds)} kept from `{file.filename}`. {human_size(len(out))}.")
    await reply(interaction, e, out_file(out, f"{stem(file)}_trim.{ext}"))


# ================================================================
# TIME: /timestamp
# ================================================================

_UNIT_WORDS = [(r"weeks?", "w"), (r"days?", "d"), (r"hours?|hrs?", "h"), (r"minutes?|mins?", "m"),
               (r"seconds?|secs?", "s")]
_REL = re.compile(r"(\d+(?:\.\d+)?)([wdhms])")
_UNIT_SECONDS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}
_ABS = re.compile(r"^(?:(\d{4})-(\d{1,2})-(\d{1,2}))?[ t]*"
                  r"(?:(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(am|pm)?|(\d{1,2})\s*(am|pm))?$")


def parse_tz(name: str):
    raw = (name or "UTC").strip()
    low = raw.lower().replace(" ", "")
    if low in ("utc", "gmt", "z"):
        return timezone.utc
    m = re.fullmatch(r"(?:utc|gmt)?([+-])(\d{1,2})(?::?(\d{2}))?", low)
    if m:
        delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
        return timezone(delta if m.group(1) == "+" else -delta)
    for candidate in (raw, "/".join(p.title() for p in raw.replace(" ", "_").split("/"))):
        try:
            return ZoneInfo(candidate)
        except Exception:
            continue
    raise UserError(f"I don't know the timezone `{clip(raw, 30)}`. Use a name like `America/New_York` or an offset like `UTC-5`.")


def parse_when(text: str, tz) -> datetime:
    """'in 2h30m', '2026-11-05 18:30', '6:30pm', '2026-12-25'. Relative times ignore the timezone."""
    t = text.strip().lower().removeprefix("in ").strip()
    rel = t
    for pattern, short in _UNIT_WORDS:
        rel = re.sub(rf"(\d)\s*(?:{pattern})\b", rf"\1{short}", rel)
    rel = re.sub(r"\band\b|[,\s]", "", rel)
    if rel and re.fullmatch(r"(?:\d+(?:\.\d+)?[wdhms])+", rel):
        seconds = sum(float(n) * _UNIT_SECONDS[u] for n, u in _REL.findall(rel))
        return datetime.now(timezone.utc) + timedelta(seconds=seconds)
    m = _ABS.match(t)
    if not m or not any(m.groups()):
        raise UserError("I couldn't read that time. Try `in 2h30m`, `2026-11-05 18:30`, `6:30pm` or `2026-12-25`.")
    y, mo, d, hh, mm, ss, ap1, hh2, ap2 = m.groups()
    now = datetime.now(tz)
    hour, minute, sec, ap = (int(hh), int(mm), int(ss or 0), ap1) if hh else (int(hh2), 0, 0, ap2) if hh2 else (0, 0, 0, None)
    if ap:
        if not 1 <= hour <= 12:
            raise UserError("Hours with am/pm have to be 1 to 12.")
        hour = hour % 12 + (12 if ap == "pm" else 0)
    try:
        return datetime(int(y) if y else now.year, int(mo) if mo else now.month, int(d) if d else now.day,
                        hour, minute, sec, tzinfo=tz)
    except ValueError:
        raise UserError("That date or time doesn't exist.")


@app_commands.command(name="timestamp", description="Make Discord timestamp codes that show in everyone's own timezone")
@app_commands.describe(when="like 2026-11-05 18:30, 6:30pm, or in 2h30m", timezone="Name like America/New_York, or UTC-5 (default UTC)")
async def timestamp_cmd(interaction: discord.Interaction, when: str, timezone: str = "UTC"):
    try:
        dt = parse_when(when, parse_tz(timezone))
    except UserError as err:
        await interaction.response.send_message(embed=error_embed(str(err)), ephemeral=True)
        return
    ts = int(dt.timestamp())
    styles = [("t", "Short time"), ("T", "Long time"), ("d", "Short date"), ("D", "Long date"),
              ("f", "Short date and time"), ("F", "Full date and time"), ("R", "Relative")]
    lines = [f"`<t:{ts}:{k}>`  {label}: <t:{ts}:{k}>" for k, label in styles]
    await interaction.response.send_message(embed=embed("Timestamp", "\n".join(lines)))


# ================================================================
# MONEY: /currency and /crypto
# ================================================================

def parse_amount(raw: str) -> float:
    m = re.fullmatch(r"\s*([\d,]*\.?\d+)\s*([kKmM]?)\s*", raw or "")
    if not m:
        raise UserError("Give a number like `250`, `1,500`, `5k` or `1.5m`.")
    return float(m.group(1).replace(",", "")) * {"": 1, "k": 1e3, "m": 1e6}[m.group(2).lower()]


@app_commands.command(name="currency", description="Convert money between currencies (daily European Central Bank rates)")
@app_commands.describe(amount="How much, like 250 or 5k", source="Currency code to convert from, like USD",
                       target="Currency code to convert to, like EUR")
@guarded
async def currency_cmd(interaction: discord.Interaction, amount: str, source: str, target: str):
    n = parse_amount(amount)
    a, b = source.strip().upper(), target.strip().upper()
    if not (re.fullmatch(r"[A-Z]{3}", a) and re.fullmatch(r"[A-Z]{3}", b)):
        raise UserError("Currencies are three letters, like `USD` or `EUR`.")
    if a == b:
        raise UserError("Those are the same currency.")
    data = await http_json("https://api.frankfurter.dev/v1/latest", params={"base": a, "symbols": b})
    rate = ((data or {}).get("rates") or {}).get(b)
    if not rate:
        raise UserError("One of those currencies isn't supported. The ECB covers about 30 major ones, so "
                        "things like RUB and crypto aren't included.")
    e = embed(f"{n:,.2f} {a} = {n * rate:,.2f} {b}", f"1 {a} = {rate:,.4f} {b}")
    e.set_footer(text=f"{CFG['Footer']}  |  Rates from {data.get('date')}")
    await reply(interaction, e)


def money(v) -> str:
    if v is None:
        return "n/a"
    return f"{v:,.2f}" if abs(v) >= 1 else f"{v:,.6f}".rstrip("0").rstrip(".")


@app_commands.command(name="crypto", description="Price, 24h change and market cap for a coin")
@app_commands.describe(coin="Name or symbol, like bitcoin or eth", currency="Currency code (default usd)")
@guarded
async def crypto_cmd(interaction: discord.Interaction, coin: str, currency: str = "usd"):
    cur = currency.strip().lower()
    if not re.fullmatch(r"[a-z]{3,5}", cur):
        raise UserError("Currency should be a code like `usd` or `eur`.")
    found = await http_json("https://api.coingecko.com/api/v3/search", params={"query": coin.strip()})
    coins = (found or {}).get("coins") or []
    if not coins:
        raise UserError(f"No coin matched `{clip(coin, 40)}`.")
    rows = await http_json("https://api.coingecko.com/api/v3/coins/markets", params={
        "vs_currency": cur, "ids": coins[0]["id"], "price_change_percentage": "24h"})
    if not rows:
        raise UserError("CoinGecko didn't return prices. It may be rate limiting, or that currency isn't supported.")
    c = rows[0]
    change = c.get("price_change_percentage_24h")
    e = embed(f"{c['name']} ({c['symbol'].upper()})", f"**{money(c['current_price'])} {cur.upper()}**",
              url=f"https://www.coingecko.com/en/coins/{c['id']}")
    e.add_field(name="24h change", value=f"{change:+.2f}%" if change is not None else "n/a")
    e.add_field(name="24h high / low", value=f"{money(c.get('high_24h'))} / {money(c.get('low_24h'))}")
    e.add_field(name="Market cap", value=f"{money(c.get('market_cap'))}")
    e.add_field(name="Rank", value=f"#{c['market_cap_rank']}" if c.get("market_cap_rank") else "n/a")
    if c.get("image"):
        e.set_thumbnail(url=c["image"])
    await reply(interaction, e)


# ================================================================
# SMALL DEVELOPER TOOLS: /hash and /base64
# ================================================================

@app_commands.command(name="hash", description="MD5, SHA-1, SHA-256 and SHA-512 of some text or a file")
@app_commands.describe(text="Text to hash", file="Or a file to hash instead")
@guarded
async def hash_cmd(interaction: discord.Interaction, text: Optional[str] = None,
                   file: Optional[discord.Attachment] = None):
    if file:
        if file.size > CFG["MaxInputBytes"]:
            raise UserError(f"That file is over {human_size(CFG['MaxInputBytes'])}.")
        data, what = await file.read(), file.filename
    elif text is not None:
        data, what = text.encode(), f"{len(text)} characters of text"
    else:
        raise UserError("Give some text or attach a file.")
    lines = [f"{algo.upper():<7}{hashlib.new(algo, data).hexdigest()}" for algo in ("md5", "sha1", "sha256", "sha512")]
    await reply(interaction, embed("Hashes", f"{clip(what, 100)}\n```\n" + "\n".join(lines) + "\n```"))


@app_commands.command(name="base64", description="Encode text to Base64 or decode it back")
@app_commands.describe(mode="Encode or decode", text="The text")
@guarded
async def base64_cmd(interaction: discord.Interaction, mode: Literal["encode", "decode"],
                     text: app_commands.Range[str, 1, 4000]):
    if mode == "encode":
        result = base64.b64encode(text.encode()).decode()
    else:
        try:
            clean = re.sub(r"\s+", "", text)
            raw = base64.b64decode(clean + "=" * (-len(clean) % 4), validate=True)
            result = raw.decode()
        except UnicodeDecodeError:  # valid Base64 that isn't text
            await reply(interaction, embed("Decoded", "That decodes to binary data, attached as a file."),
                        discord.File(io.BytesIO(raw), filename="decoded.bin"))
            return
        except Exception:
            raise UserError("That isn't valid Base64.")
    if len(result) > 3800:
        await reply(interaction, embed(mode.title() + "d", "Result attached as a file."),
                    discord.File(io.BytesIO(result.encode()), filename=f"{mode}d.txt"))
    else:
        await reply(interaction, embed(mode.title() + "d", f"```\n{result}\n```"))


# ================================================================
# /unshorten (follows redirects without opening anything on private networks)
# ================================================================

class SafeResolver(aiohttp.abc.AbstractResolver):
    """Refuses to connect to private, loopback or otherwise non-public addresses (SSRF protection).
    Checking here, at connect time, also defeats DNS tricks where a name flips to 127.0.0.1 after a check."""

    def __init__(self):
        self._inner = aiohttp.ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        found = await self._inner.resolve(host, port, family)
        safe = [r for r in found if ipaddress.ip_address(r["host"]).is_global]
        if not safe:
            raise OSError("blocked: that address isn't public")
        return safe

    async def close(self):
        await self._inner.close()


def check_public_url(url: str) -> None:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise UserError("Only http and https links can be followed.")
    if p.port not in (None, 80, 443):
        raise UserError("I only follow links on the normal web ports.")
    try:
        if not ipaddress.ip_address(p.hostname).is_global:
            raise UserError("That link points at a private address, so I won't open it.")
    except ValueError:
        pass   # a name, not an IP literal: SafeResolver checks it when connecting


@app_commands.command(name="unshorten", description="Follow a short link and show where it really goes")
@app_commands.describe(link="The shortened link")
@guarded
async def unshorten_cmd(interaction: discord.Interaction, link: str):
    url = need_url(link)
    chain: list[tuple[int, str]] = []
    connector = aiohttp.TCPConnector(resolver=SafeResolver(), use_dns_cache=False)
    async with aiohttp.ClientSession(connector=connector, headers={"User-Agent": CFG["UserAgent"]},
                                     timeout=aiohttp.ClientTimeout(total=10)) as s:
        for _ in range(CFG["UnshortenHops"]):
            check_public_url(url)
            try:
                async with s.get(url, allow_redirects=False) as r:
                    status, loc = r.status, r.headers.get("Location")
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                raise UserError(f"Couldn't open `{clip(url, 80)}`.")
            chain.append((status, url))
            if status in (301, 302, 303, 307, 308) and loc:
                url = urlparse(url)._replace(path="", query="", fragment="").geturl().rstrip("/") + loc \
                    if loc.startswith("/") else loc
                continue
            break
        else:
            raise UserError(f"That link redirects more than {CFG['UnshortenHops']} times.")
    final_status, final_url = chain[-1]
    steps = "\n".join(f"`{st}` {clip(u, 90)}" for st, u in chain[:-1]) or "No redirects."
    e = embed("Where it goes", f"**{clip(final_url, 500)}**")
    e.add_field(name="Final status", value=str(final_status))
    e.add_field(name="Hops", value=str(len(chain) - 1))
    e.add_field(name="Path", value=steps, inline=False)
    e.set_footer(text=f"{CFG['Footer']}  |  Redirects done by scripts or meta tags aren't followed")
    await reply(interaction, e)


# ================================================================
# MUSIC: /spotify and /soundcloud
# ================================================================

SPOTIFY_URL = re.compile(r"open\.spotify\.com/(?:intl-[a-z]+/)?(track|album|artist|playlist|episode|show)/([A-Za-z0-9]+)")
_spotify_token = {"value": "", "until": 0.0}


async def spotify_api(kind: str, sid: str) -> Optional[dict]:
    if not (CFG["SpotifyId"] and CFG["SpotifySecret"]):
        return None
    if time.time() > _spotify_token["until"]:
        try:
            s = await get_session()
            async with s.post("https://accounts.spotify.com/api/token", data={"grant_type": "client_credentials"},
                              auth=aiohttp.BasicAuth(CFG["SpotifyId"], CFG["SpotifySecret"])) as r:
                if r.status != 200:
                    return None
                tok = await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None
        _spotify_token.update(value=tok["access_token"], until=time.time() + tok.get("expires_in", 3600) - 60)
    return await http_json(f"https://api.spotify.com/v1/{kind}s/{sid}",
                           headers={"Authorization": f"Bearer {_spotify_token['value']}"})


@app_commands.command(name="spotify", description="Look up a Spotify track, album, artist or playlist by link")
@app_commands.describe(link="open.spotify.com link")
@guarded
async def spotify_cmd(interaction: discord.Interaction, link: str):
    m = SPOTIFY_URL.search(link)
    if not m:
        raise UserError("That isn't a Spotify link to a track, album, artist or playlist.")
    kind, sid = m.groups()
    url = f"https://open.spotify.com/{kind}/{sid}"
    d = await spotify_api(kind, sid)
    art = lambda x: ", ".join(a["name"] for a in x.get("artists", []))
    img = lambda x: ((x.get("images") or x.get("album", {}).get("images") or [{}])[0]).get("url")
    if d and kind == "track":
        e = embed(clip(d["name"], 250), url=url)
        e.add_field(name="Artist", value=clip(art(d), 200))
        e.add_field(name="Album", value=clip(d["album"]["name"], 100))
        e.add_field(name="Length", value=fmt_duration(d["duration_ms"] / 1000))
        e.add_field(name="Released", value=d["album"].get("release_date", "n/a"))
        e.add_field(name="Popularity", value=f"{d.get('popularity', 0)} / 100")
        e.add_field(name="Explicit", value="Yes" if d.get("explicit") else "No")
    elif d and kind == "album":
        e = embed(clip(d["name"], 250), url=url)
        e.add_field(name="Artist", value=clip(art(d), 200))
        e.add_field(name="Released", value=d.get("release_date", "n/a"))
        e.add_field(name="Tracks", value=str(d.get("total_tracks", "n/a")))
    elif d and kind == "artist":
        e = embed(clip(d["name"], 250), url=url)
        e.add_field(name="Followers", value=num(d["followers"]["total"]))
        e.add_field(name="Popularity", value=f"{d.get('popularity', 0)} / 100")
        if d.get("genres"):
            e.add_field(name="Genres", value=clip(", ".join(d["genres"][:5]), 200), inline=False)
    elif d and kind == "playlist":
        e = embed(clip(d["name"], 250), clip(d.get("description"), 300) or None, url=url)
        e.add_field(name="Owner", value=clip(d["owner"].get("display_name") or "n/a", 60))
        e.add_field(name="Tracks", value=num(d["tracks"]["total"]))
        e.add_field(name="Followers", value=num(d["followers"]["total"]))
    else:
        data = await http_json("https://open.spotify.com/oembed", params={"url": url})
        if not data:
            raise UserError("Spotify didn't return that link. It may be private or removed.")
        e = embed(clip(data["title"], 250), f"Spotify {kind}", url=url)
        e.set_footer(text=f"{CFG['Footer']}  |  Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET for artist, album and more")
        if data.get("thumbnail_url"):
            e.set_thumbnail(url=data["thumbnail_url"])
        await reply(interaction, e)
        return
    if img(d):
        e.set_thumbnail(url=img(d))
    await reply(interaction, e)


@app_commands.command(name="soundcloud", description="Look up a SoundCloud track or playlist by link")
@app_commands.describe(link="soundcloud.com link")
@guarded
async def soundcloud_cmd(interaction: discord.Interaction, link: str):
    url = need_url(link)
    if "soundcloud.com" not in urlparse(url).netloc:
        raise UserError("That isn't a SoundCloud link.")
    data = await http_json("https://soundcloud.com/oembed", params={"format": "json", "url": url})
    if not data:
        raise UserError("SoundCloud didn't return that link. It may be private or removed.")
    e = embed(clip(data.get("title"), 250), clip(data.get("description"), 300) or None, url=url)
    e.add_field(name="Artist", value=f"[{clip(data.get('author_name'), 60)}]({data.get('author_url')})")
    if data.get("thumbnail_url"):
        e.set_thumbnail(url=data["thumbnail_url"])
    await reply(interaction, e)



# ================================================================
# REGISTER
# ================================================================

COMMANDS = [
    convert_cmd, resize_cmd, compress_cmd, gifmaker_cmd, audioconvert_cmd, waveform_cmd,
    youtube_cmd, tiktok_cmd, tweet_cmd, embedfix_cmd,
    github_cmd, weather_cmd, define_cmd, robloxstatus_cmd, qr_cmd, color_cmd,
    filter_cmd, caption_cmd, gifframes_cmd, spritesheet_cmd, ocr_cmd, trim_cmd,
    timestamp_cmd, currency_cmd, crypto_cmd, hash_cmd, base64_cmd, unshorten_cmd,
    spotify_cmd, soundcloud_cmd,
]


def register(bot) -> None:
    for cmd in COMMANDS:
        bot.tree.add_command(cmd)
