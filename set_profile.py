# DeadAir — set_profile.py
# Sets the bot's banner (and optionally avatar) through Discord's API.
# Usage:  python set_profile.py --banner assets/deadair_banner_gradient.png
#         python set_profile.py --avatar halo_pfp_big.png
#         python set_profile.py --banner assets/deadair_banner_glow.png --dry-run   (checks files, sends nothing)
# Credits: @Nexesmere / EXE Development

import argparse
import base64
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

API = "https://discord.com/api/v10/users/@me"
MAX_BYTES = 8 * 1024 * 1024


def data_uri(path: str) -> str:
    p = Path(path)
    if not p.is_file():
        sys.exit(f"File not found: {path}")
    mime = mimetypes.guess_type(p.name)[0]
    if mime not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        sys.exit(f"{path}: use a PNG, JPG, GIF or WebP image.")
    raw = p.read_bytes()
    if len(raw) > MAX_BYTES:
        sys.exit(f"{path} is {len(raw) / 1e6:.1f} MB, over the {MAX_BYTES / 1e6:.0f} MB limit.")
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def main():
    ap = argparse.ArgumentParser(description="Set the DeadAir bot's banner and/or avatar.")
    ap.add_argument("--banner", help="path to the banner image")
    ap.add_argument("--avatar", help="path to the avatar image")
    ap.add_argument("--dry-run", action="store_true", help="validate the files and show what would be sent")
    args = ap.parse_args()
    if not (args.banner or args.avatar):
        ap.error("give --banner and/or --avatar")

    payload = {}
    if args.banner:
        payload["banner"] = data_uri(args.banner)
    if args.avatar:
        payload["avatar"] = data_uri(args.avatar)

    if args.dry_run:
        print("Dry run, nothing sent. Would PATCH", API, "with:",
              {k: f"<{len(v)} chars of image data>" for k, v in payload.items()})
        return

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        sys.exit("DISCORD_TOKEN isn't set. Put it in .env first.")

    req = urllib.request.Request(
        API, data=json.dumps(payload).encode(), method="PATCH",
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                 "User-Agent": "DiscordBot (https://github.com/, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            user = json.loads(res.read())
            print(f"Done. Updated {user.get('username')}: "
                  f"banner={'set' if user.get('banner') else 'not set'}, avatar={'set' if user.get('avatar') else 'not set'}")
    except urllib.error.HTTPError as e:
        print(f"Discord said {e.code}: {e.read().decode()[:400]}")
        if e.code == 429:
            print("You're rate limited. Profile changes are limited, so wait a while and try again.")
        sys.exit(1)


if __name__ == "__main__":
    main()
