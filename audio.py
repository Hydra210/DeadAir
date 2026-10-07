# DeadAir — audio.py
# Decodes a Roblox audio file, measures loudness, and renders a black-and-white waveform.
# Credits: @Nexesmere / EXE Development

import io
import math

import numpy as np
import soundfile as sf
from PIL import Image, ImageDraw

# ================================================================
# CONFIG
# ================================================================

CFG = {
    "Bars": 96,
    "ImageWidth": 1000,
    "ImageHeight": 240,
    "Supersample": 2,          # draw at 2x then downscale for clean edges
    "Margin": 28,
    "BarFill": 0.58,           # bar width as a fraction of its slot
    "Gamma": 0.6,              # <1 lifts quiet parts so the shape stays readable
    "PeakColor": (52, 52, 52),
    "RmsColor": (255, 255, 255),
    "Background": (0, 0, 0),
    "BaselineColor": (26, 26, 26),
}


# ================================================================
# ANALYSIS
# ================================================================

def _db(v: float) -> float:
    return 20.0 * math.log10(max(v, 1e-9))


def _reduce(values: list[float], n: int, mode: str) -> list[float]:
    arr = np.asarray(values, dtype=np.float64)
    if len(arr) <= n:
        return arr.tolist()
    out = []
    for chunk in np.array_split(arr, n):
        out.append(float(np.sqrt(np.mean(chunk ** 2))) if mode == "rms" else float(chunk.max()))
    return out


def analyze(data: bytes, bars: int = CFG["Bars"]) -> dict:
    """Streams the file in blocks so a long track never sits fully decoded in memory."""
    with sf.SoundFile(io.BytesIO(data)) as f:
        sr, channels, frames = f.samplerate, f.channels, f.frames
        fmt, subtype = f.format, f.subtype
        block = max(frames // bars, 256) if frames and frames > 0 else max(sr // 4, 256)

        rms_list, peak_list = [], []
        sumsq, count, peak, total = 0.0, 0, 0.0, 0
        while True:
            x = f.read(block, dtype="float32", always_2d=True)
            if len(x) == 0:
                break
            mono = x.mean(axis=1)
            rms_list.append(float(np.sqrt(np.mean(mono ** 2))))
            p = float(np.abs(x).max())
            peak_list.append(p)
            sumsq += float(np.sum(mono.astype(np.float64) ** 2))
            count += len(mono)
            peak = max(peak, p)
            total += len(x)

    if total == 0:
        raise ValueError("audio file is empty")

    duration = total / sr
    rms = math.sqrt(sumsq / count) if count else 0.0
    return {
        "duration": duration,
        "sample_rate": sr,
        "channels": channels,
        "format": fmt,
        "subtype": subtype,
        "size_bytes": len(data),
        "bitrate_kbps": (len(data) * 8 / duration / 1000) if duration else 0,
        "peak_db": _db(peak),
        "rms_db": _db(rms),
        "range_db": _db(peak) - _db(rms),
        "rms_bars": _reduce(rms_list, bars, "rms"),
        "peak_bars": _reduce(peak_list, bars, "peak"),
    }


# ================================================================
# WAVEFORM IMAGE (black and white)
# ================================================================

def render_waveform(stats: dict) -> bytes:
    ss = CFG["Supersample"]
    W, H = CFG["ImageWidth"] * ss, CFG["ImageHeight"] * ss
    img = Image.new("RGB", (W, H), CFG["Background"])
    d = ImageDraw.Draw(img)

    rms_bars, peak_bars = stats["rms_bars"], stats["peak_bars"]
    n = len(rms_bars)
    margin = CFG["Margin"] * ss
    pitch = (W - 2 * margin) / n
    bw = pitch * CFG["BarFill"]
    mid = H / 2
    max_h = H / 2 - 22 * ss
    scale = max(peak_bars) or 1.0
    gamma = CFG["Gamma"]

    d.line([(margin, mid), (W - margin, mid)], fill=CFG["BaselineColor"], width=ss)

    for i in range(n):
        x = margin + i * pitch + (pitch - bw) / 2
        ph = max((peak_bars[i] / scale) ** gamma * max_h, 3 * ss)   # peak layer (dark gray)
        rh = max((rms_bars[i] / scale) ** gamma * max_h, 2 * ss)    # average layer (white)
        rh = min(rh, ph)
        r = bw / 2
        d.rounded_rectangle([x, mid - ph, x + bw, mid + ph], radius=r, fill=CFG["PeakColor"])
        d.rounded_rectangle([x, mid - rh, x + bw, mid + rh], radius=r, fill=CFG["RmsColor"])

    img = img.resize((CFG["ImageWidth"], CFG["ImageHeight"]), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()
