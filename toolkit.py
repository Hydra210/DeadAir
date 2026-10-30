# DeadAir — toolkit.py
# File converters, social/link lookups and small utilities that sit next to the Roblox commands.
# Credits: @Nexesmere / EXE Development
#
# bot.py calls register(bot) once. Nothing in here needs a Roblox cookie.

import asyncio
import colorsys
import functools
import io
import math
import os
import re
from typing import Awaitable, Callable, Literal, Optional
from urllib.parse import quote, urlparse

import aiohttp
import discord
import qrcode
import soundfile as sf
from discord import app_commands
from PIL import Image, ImageDraw, ImageSequence, UnidentifiedImageError

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
# REGISTER
# ================================================================

COMMANDS = [
    convert_cmd, resize_cmd, compress_cmd, gifmaker_cmd, audioconvert_cmd, waveform_cmd,
    youtube_cmd, tiktok_cmd, tweet_cmd, embedfix_cmd,
    github_cmd, weather_cmd, define_cmd, robloxstatus_cmd, qr_cmd, color_cmd,
]


def register(bot) -> None:
    for cmd in COMMANDS:
        bot.tree.add_command(cmd)
