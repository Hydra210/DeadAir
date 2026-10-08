# DeadAir — roblox.py
# Server-side port of the EXE Audio Checker's Roblox logic.
# Credits: @Nexesmere / EXE Development

import asyncio
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import aiohttp

# ================================================================
# CONFIG
# ================================================================

CFG = {
    "UserAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Origin": "https://create.roblox.com",
    "StatusChunkSize": 20,      # develop.roblox.com/v1/assets batch size (same as the extension)
    "ListPageLimit": 100,
    "MaxScan": 100_000,         # hard ceiling for "all" (1,000 requests). The old cap was 2,500.
    "MaxRetries": 4,
    "RetryBackoff": 1.0,        # seconds, doubles per retry (1s, 2s, 4s) for 5xx errors on GET requests
    "ScanCacheSeconds": 300,    # a group scan is reused/resumed for this long
    "ScanCacheGroups": 3,       # how many groups' scans to keep in memory
    "MaxAudioBytes": 30_000_000,  # refuse to download anything bigger than this
    "PlaceId": os.getenv("ROBLOX_PLACE_ID", "1818"),  # sent as Roblox-Place-Id on asset delivery (any public place)
    "JoinCooldownSeconds": 30,  # min gap between group joins so the account doesn't get flagged
}


# ================================================================
# ERRORS / TYPES
# ================================================================

class RobloxError(Exception):
    pass


class AuthError(RobloxError):
    """The .ROBLOSECURITY cookie is dead."""


class ChallengeRequired(RobloxError):
    """Roblox demanded a captcha / challenge. Can't be solved from a bot."""


@dataclass
class GroupInfo:
    id: int
    name: str
    public_entry: bool
    locked: bool
    member_count: int = 0


def status_to_working(item: Optional[dict]) -> str:
    # Same logic as the extension: isModerated is the real signal.
    if not item:
        return "unknown"
    review = item.get("reviewStatus")
    if review and review != "Finished":
        return "unknown"
    return "broken" if item.get("isModerated") else "working"


def _chunk(arr, size):
    for i in range(0, len(arr), size):
        yield arr[i:i + size]


# ================================================================
# CLIENT
# ================================================================

class RobloxClient:
    def __init__(self, cookie: str):
        cookie = cookie.strip().strip('"').strip("'")
        if cookie.startswith(".ROBLOSECURITY="):
            cookie = cookie[len(".ROBLOSECURITY="):]
        self._cookie = cookie
        self._csrf: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._plain: Optional[aiohttp.ClientSession] = None  # no cookie, used for CDN downloads
        self._join_lock = asyncio.Lock()
        self._last_join = 0.0
        self.user: Optional[dict] = None
        self.last_skipped = 0
        self.last_meta = {"pages": 0, "raw": 0, "truncated": False, "limit": 0, "hit_ceiling": False,
                          "incomplete": False, "error": None}
        self._scan_cache: dict[int, dict] = {}  # group_id -> {items, cursor, complete, skipped, ts}

    async def start(self):
        self._session = aiohttp.ClientSession(
            headers={
                "User-Agent": CFG["UserAgent"],
                "Origin": CFG["Origin"],
                "Referer": CFG["Origin"] + "/",
                "Cookie": f".ROBLOSECURITY={self._cookie}",
                # What Chrome adds to a cross-site fetch from create.roblox.com (the extension's tab does this).
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-site",
            },
            timeout=aiohttp.ClientTimeout(total=30),
        )
        self._plain = aiohttp.ClientSession(
            headers={"User-Agent": CFG["UserAgent"]},
            timeout=aiohttp.ClientTimeout(total=60),
        )
        self.user = await self.get_me()

    async def close(self):
        if self._session:
            await self._session.close()
        if self._plain:
            await self._plain.close()

    # ---------- core request: CSRF retry, 429 backoff, challenge detection ----------

    async def _request(self, method: str, url: str, *, params=None, json=None, extra_headers=None):
        last_err = "unknown error"
        for attempt in range(CFG["MaxRetries"]):
            headers = {"x-csrf-token": self._csrf} if self._csrf else {}
            if extra_headers:
                headers.update(extra_headers)
            try:
                async with self._session.request(
                    method, url, params=params, json=json, headers=headers
                ) as res:
                    text = await res.text()

                    if "rblx-challenge-id" in res.headers:
                        raise ChallengeRequired("Roblox wants a captcha/challenge for this action.")

                    new_csrf = res.headers.get("x-csrf-token")
                    if res.status == 403 and new_csrf and new_csrf != self._csrf:
                        self._csrf = new_csrf
                        continue  # retry with the fresh token

                    if res.status == 429:
                        wait = float(res.headers.get("retry-after", 2 * (attempt + 1)))
                        await asyncio.sleep(min(wait, 15))
                        last_err = "429 rate limited"
                        continue

                    if res.status == 401:
                        raise AuthError("401 — the .ROBLOSECURITY cookie is invalid or expired.")

                    if res.status in (500, 502, 503, 504) and method.upper() == "GET":
                        last_err = f"{res.status} from Roblox"
                        await asyncio.sleep(min(CFG["RetryBackoff"] * (2 ** attempt), 8))
                        continue

                    if not res.ok:
                        raise RobloxError(f"{res.status} — {self._extract_error(text)}")

                    if not text:
                        return {}
                    try:
                        return await res.json(content_type=None)
                    except Exception:
                        return {}
            except aiohttp.ClientError as e:
                last_err = f"network error: {e}"
                await asyncio.sleep(1 + attempt)
        raise RobloxError(f"Gave up after retries ({last_err})")

    @staticmethod
    def _extract_error(text: str) -> str:
        import json as _json
        try:
            data = _json.loads(text)
            errs = data.get("errors") or []
            if errs:
                return errs[0].get("message") or errs[0].get("userFacingMessage") or text[:200]
        except Exception:
            pass
        return (text or "no body")[:200]

    # ---------- account / groups ----------

    async def get_me(self) -> dict:
        return await self._request("GET", "https://users.roblox.com/v1/users/authenticated")

    async def in_group(self, group_id: int) -> bool:
        data = await self._request(
            "GET", f"https://groups.roblox.com/v1/users/{self.user['id']}/groups/roles"
        )
        return any(int(g["group"]["id"]) == int(group_id) for g in data.get("data", []))

    async def group_role(self, group_id: int) -> Optional[dict]:
        """The account's role in a group, e.g. {"name": "Member", "rank": 1}, or None if it isn't in it."""
        data = await self._request(
            "GET", f"https://groups.roblox.com/v1/users/{self.user['id']}/groups/roles"
        )
        for g in data.get("data", []):
            if int(g["group"]["id"]) == int(group_id):
                role = g.get("role") or {}
                return {"name": role.get("name", "?"), "rank": role.get("rank")}
        return None

    async def group_info(self, group_id: int) -> GroupInfo:
        d = await self._request("GET", f"https://groups.roblox.com/v1/groups/{group_id}")
        return GroupInfo(
            id=int(d["id"]),
            name=d.get("name", "Unknown group"),
            public_entry=bool(d.get("publicEntryAllowed")),
            locked=bool(d.get("isLocked")),
            member_count=int(d.get("memberCount") or 0),
        )

    async def join_group(self, group_id: int) -> str:
        """Returns: 'joined' | 'pending' | 'already'. Raises ChallengeRequired / RobloxError."""
        async with self._join_lock:
            if await self.in_group(group_id):
                return "already"

            # Cooldown so rapid joins don't get the account flagged.
            wait = CFG["JoinCooldownSeconds"] - (time.monotonic() - self._last_join)
            if wait > 0 and self._last_join:
                await asyncio.sleep(wait)

            await self._request("POST", f"https://groups.roblox.com/v1/groups/{group_id}/users", json={})
            self._last_join = time.monotonic()

            await asyncio.sleep(1.5)
            return "joined" if await self.in_group(group_id) else "pending"

    # ---------- audio ----------

    async def list_group_audio(self, group_id: int, max_items: Optional[int] = None, progress=None,
                               refresh: bool = False) -> list[dict]:
        """Lists a group's audio. max_items=None means everything (up to CFG['MaxScan']).

        - Transient Roblox errors are retried; a page that keeps failing is retried with a smaller page size.
        - If Roblox still fails partway, what was loaded is returned and last_meta['incomplete'] is set.
        - The scan is cached for a few minutes, so a repeat search is instant and a broken scan resumes
          from where it stopped. refresh=True throws the cache away.
        progress(count) is awaited after every page."""
        ceiling = CFG["MaxScan"]
        want = min(max_items, ceiling) if max_items else ceiling
        now = time.monotonic()

        entry = self._scan_cache.get(group_id)
        if refresh or not entry or now - entry["ts"] > CFG["ScanCacheSeconds"]:
            entry = {"items": [], "cursor": "", "complete": False, "skipped": 0, "ts": now}
            self._scan_cache[group_id] = entry
        resumed = bool(entry["items"])
        age = now - entry["ts"]
        items = entry["items"]

        page_limits = [CFG["ListPageLimit"], 50, 25]
        li, pages, error = 0, 0, None
        while not entry["complete"] and len(items) < want:
            params = {"assetType": "Audio", "groupId": str(group_id), "limit": str(page_limits[li])}
            if entry["cursor"]:
                params["cursor"] = entry["cursor"]
            try:
                data = await self._request(
                    "GET", "https://itemconfiguration.roblox.com/v1/creations/get-assets", params=params)
            except (AuthError, ChallengeRequired):
                raise
            except RobloxError as e:
                transient = str(e).startswith("Gave up after retries")
                if transient and li < len(page_limits) - 1:
                    li += 1  # deep pages time out less with smaller pages
                    continue
                if not items:
                    raise  # nothing loaded at all: this is a real failure, not a partial result
                m = re.search(r"(\d{3}) from Roblox", str(e))
                error = f"HTTP {m.group(1)}" if m else str(e)
                break

            for it in data.get("data", []):
                try:
                    if int(it["assetId"]) > 0:
                        items.append(it)
                    else:
                        entry["skipped"] += 1
                except (KeyError, ValueError, TypeError):
                    entry["skipped"] += 1
            entry["cursor"] = data.get("nextPageCursor") or ""
            entry["complete"] = not entry["cursor"]
            entry["ts"] = time.monotonic()
            pages += 1
            if progress:
                try:
                    await progress(len(items))
                except Exception:
                    pass  # a failed progress message must never break the scan

        # keep only the most recent few groups in memory
        if len(self._scan_cache) > CFG["ScanCacheGroups"]:
            for gid_old in sorted(self._scan_cache, key=lambda g: self._scan_cache[g]["ts"])[:-CFG["ScanCacheGroups"]]:
                self._scan_cache.pop(gid_old, None)

        truncated = (not entry["complete"]) or len(items) > want
        self.last_skipped = entry["skipped"]  # entries with no asset ID yet (still processing)
        self.last_meta = {
            "pages": pages, "raw": len(items) + entry["skipped"], "truncated": truncated and not error,
            "limit": want, "hit_ceiling": truncated and not error and want >= ceiling,
            "incomplete": bool(error), "error": error, "resumed": resumed,
            "from_cache": resumed and pages == 0, "cached_age": age, "page_limit": page_limits[li],
        }
        return list(items[:want])  # [{ name, assetId }]

    async def asset_details(self, asset_ids: list[int]) -> dict[str, dict]:
        """Moderation info from develop.roblox.com. One bad ID never sinks the whole batch."""
        out: dict[str, dict] = {}

        async def fetch(ids: list[int]):
            data = await self._request(
                "GET", "https://develop.roblox.com/v1/assets",
                params={"assetIds": ",".join(str(i) for i in ids)},
            )
            for d in data.get("data", []):
                out[str(d.get("id"))] = d

        for batch in _chunk(asset_ids, CFG["StatusChunkSize"]):
            try:
                await fetch(batch)
            except (AuthError, ChallengeRequired):
                raise
            except RobloxError:
                if len(batch) == 1:
                    continue
                for aid in batch:
                    try:
                        await fetch([aid])
                    except (AuthError, ChallengeRequired):
                        raise
                    except RobloxError:
                        continue
        return out

    async def economy_details(self, asset_id: int) -> Optional[dict]:
        """Public asset info: name, description, creator, dates, asset type."""
        try:
            return await self._request("GET", f"https://economy.roblox.com/v2/assets/{asset_id}/details")
        except (AuthError, ChallengeRequired):
            raise
        except RobloxError:
            return None

    async def asset_name_fallback(self, asset_id: int) -> Optional[str]:
        d = await self.economy_details(asset_id)
        return d.get("Name") if d else None

    async def toolbox_audio_info(self, asset_id: int) -> dict:
        """Best effort: artist / genre / album / duration from the Creator Store. Silent on failure."""
        try:
            d = await self._request(
                "GET", "https://apis.roblox.com/toolbox-service/v1/items/details",
                params={"assetIds": str(asset_id)},
            )
        except RobloxError:
            return {}
        out = {}
        for key, names in (
            ("duration", ("duration",)),
            ("artist", ("artist",)),
            ("album", ("albumTitle", "album")),
            ("genre", ("genre", "musicType")),
        ):
            v = _find_key(d, names)
            if v not in (None, ""):
                out[key] = v
        return out

    async def group_icon(self, group_id: int) -> Optional[str]:
        try:
            d = await self._request(
                "GET", "https://thumbnails.roblox.com/v1/groups/icons",
                params={"groupIds": str(group_id), "size": "150x150", "format": "Png", "isCircular": "false"},
            )
        except RobloxError:
            return None
        item = (d.get("data") or [{}])[0]
        return item.get("imageUrl") if item.get("state") == "Completed" else None

    # ---------- public lookups (users, groups, assets, games, badges) ----------

    async def _safe(self, coro):
        """Runs a request and returns None if Roblox refuses it. Auth/challenge errors still propagate."""
        try:
            return await coro
        except (AuthError, ChallengeRequired):
            raise
        except RobloxError:
            return None

    async def _thumb(self, url: str, params: dict) -> Optional[str]:
        """First completed thumbnail URL, retrying once if Roblox is still rendering it."""
        for attempt in range(2):
            d = await self._safe(self._request("GET", url, params=params))
            item = ((d or {}).get("data") or [{}])[0]
            if item.get("state") == "Completed" and item.get("imageUrl"):
                return item["imageUrl"]
            if item.get("state") not in ("Pending", "InReview"):
                return None
            await asyncio.sleep(1.5)
        return None

    async def user_details(self, user_id: int) -> Optional[dict]:
        try:
            return await self._request("GET", f"https://users.roblox.com/v1/users/{user_id}")
        except (AuthError, ChallengeRequired):
            raise
        except RobloxError as e:
            if str(e)[:3] in ("400", "404"):
                return None
            raise

    async def resolve_user(self, query: str) -> Optional[dict]:
        """Accepts a username, @username, numeric ID, or a roblox.com/users/<id>/profile link."""
        q = (query or "").strip()
        m = re.search(r"roblox\.com/(?:[a-z-]+/)?users/(\d+)", q, re.I)
        if m or q.isdigit():
            return await self.user_details(int(m.group(1) if m else q))
        name = q.lstrip("@").strip()
        if not name:
            return None
        d = await self._request(
            "POST", "https://users.roblox.com/v1/usernames/users",
            json={"usernames": [name], "excludeBannedUsers": False},
        )
        hits = d.get("data") or []
        return await self.user_details(int(hits[0]["id"])) if hits else None

    async def user_count(self, user_id: int, kind: str) -> Optional[int]:
        """kind: friends | followers | followings"""
        d = await self._safe(self._request("GET", f"https://friends.roblox.com/v1/users/{user_id}/{kind}/count"))
        return d.get("count") if isinstance(d, dict) and "count" in d else None

    async def username_history(self, user_id: int, limit: int = 10) -> list[str]:
        d = await self._safe(self._request(
            "GET", f"https://users.roblox.com/v1/users/{user_id}/username-history",
            params={"limit": str(limit), "sortOrder": "Desc"}))
        return [x["name"] for x in (d or {}).get("data", []) if x.get("name")]

    async def user_groups(self, user_id: int) -> list[dict]:
        """[{ group: {id, name, memberCount, hasVerifiedBadge}, role: {name, rank} }]"""
        d = await self._safe(self._request("GET", f"https://groups.roblox.com/v2/users/{user_id}/groups/roles"))
        return (d or {}).get("data", [])

    async def user_headshot(self, user_id: int) -> Optional[str]:
        return await self._thumb("https://thumbnails.roblox.com/v1/users/avatar-headshot", {
            "userIds": str(user_id), "size": "420x420", "format": "Png", "isCircular": "false"})

    async def user_avatar(self, user_id: int) -> Optional[str]:
        return await self._thumb("https://thumbnails.roblox.com/v1/users/avatar", {
            "userIds": str(user_id), "size": "720x720", "format": "Png", "isCircular": "false"})

    async def group_details(self, group_id: int) -> Optional[dict]:
        try:
            return await self._request("GET", f"https://groups.roblox.com/v1/groups/{group_id}")
        except (AuthError, ChallengeRequired):
            raise
        except RobloxError as e:
            if str(e)[:3] in ("400", "404"):
                return None
            raise

    async def group_roles(self, group_id: int) -> list[dict]:
        d = await self._safe(self._request("GET", f"https://groups.roblox.com/v1/groups/{group_id}/roles"))
        return (d or {}).get("roles", [])

    async def asset_thumbnail(self, asset_id: int) -> Optional[str]:
        return await self._thumb("https://thumbnails.roblox.com/v1/assets", {
            "assetIds": str(asset_id), "returnPolicy": "PlaceHolder", "size": "420x420",
            "format": "Png", "isCircular": "false"})

    async def universe_from_place(self, place_id: int) -> Optional[int]:
        d = await self._safe(self._request("GET", f"https://apis.roblox.com/universes/v1/places/{place_id}/universe"))
        uid = (d or {}).get("universeId")
        return int(uid) if uid else None

    async def game_details(self, universe_id: int) -> Optional[dict]:
        d = await self._safe(self._request(
            "GET", "https://games.roblox.com/v1/games", params={"universeIds": str(universe_id)}))
        data = (d or {}).get("data") or []
        return data[0] if data else None

    async def game_votes(self, universe_id: int) -> Optional[dict]:
        d = await self._safe(self._request(
            "GET", "https://games.roblox.com/v1/games/votes", params={"universeIds": str(universe_id)}))
        data = (d or {}).get("data") or []
        return data[0] if data else None

    async def game_icon(self, universe_id: int) -> Optional[str]:
        return await self._thumb("https://thumbnails.roblox.com/v1/games/icons", {
            "universeIds": str(universe_id), "size": "256x256", "format": "Png", "isCircular": "false"})

    async def badge_details(self, badge_id: int) -> Optional[dict]:
        try:
            return await self._request("GET", f"https://badges.roblox.com/v1/badges/{badge_id}")
        except (AuthError, ChallengeRequired):
            raise
        except RobloxError as e:
            if str(e)[:3] in ("400", "404"):
                return None
            raise

    async def badge_icon(self, badge_id: int) -> Optional[str]:
        return await self._thumb("https://thumbnails.roblox.com/v1/badges/icons", {
            "badgeIds": str(badge_id), "size": "150x150", "format": "Png", "isCircular": "false"})

    # ---------- search by name ----------

    async def search_groups(self, query: str, limit: int = 10) -> list[dict]:
        """[{ id, name, memberCount, publicEntryAllowed, hasVerifiedBadge, ... }], exact matches first."""
        d = await self._safe(self._request(
            "GET", "https://groups.roblox.com/v1/groups/search",
            params={"keyword": query, "prioritizeExactMatch": "true", "limit": str(limit)}))
        return (d or {}).get("data", []) or []

    async def search_games(self, query: str) -> list[dict]:
        """[{ universeId, name, rootPlaceId, playerCount, creator }] from Roblox's omni-search."""
        d = await self._safe(self._request(
            "GET", "https://apis.roblox.com/search-api/omni-search",
            params={"searchQuery": query, "pageType": "all", "sessionId": str(uuid.uuid4())}))
        out, seen = [], set()
        for grp in (d or {}).get("searchResults", []) or []:
            if "game" not in str(grp.get("contentGroupType", "")).lower():
                continue
            for c in grp.get("contents", []) or []:
                try:
                    uid = int(c.get("contentId") or c.get("universeId"))
                except (TypeError, ValueError):
                    continue
                if uid in seen:
                    continue
                seen.add(uid)
                out.append({"universeId": uid, "name": c.get("name"), "rootPlaceId": c.get("rootPlaceId"),
                            "playerCount": c.get("playerCount"), "creator": c.get("creatorName")})
        return out

    # ---------- paged lists (errors propagate so the bot can explain private lists) ----------

    async def _page(self, url: str, *, cursor: str = "", params: Optional[dict] = None):
        p = dict(params or {})
        if cursor:
            p["cursor"] = cursor
        d = await self._request("GET", url, params=p)
        return (d.get("data") or []), (d.get("nextPageCursor") or None)

    async def friends_list(self, user_id: int) -> list[dict]:
        d = await self._request("GET", f"https://friends.roblox.com/v1/users/{user_id}/friends")
        return d.get("data") or []

    async def follow_page(self, user_id: int, kind: str, cursor: str = "", order: str = "Desc"):
        """kind: followers | followings"""
        return await self._page(f"https://friends.roblox.com/v1/users/{user_id}/{kind}",
                                cursor=cursor, params={"limit": "100", "sortOrder": order})

    async def user_badges_page(self, user_id: int, cursor: str = ""):
        return await self._page(f"https://badges.roblox.com/v1/users/{user_id}/badges",
                                cursor=cursor, params={"limit": "100", "sortOrder": "Desc"})

    async def user_games_page(self, user_id: int, cursor: str = ""):
        return await self._page(f"https://games.roblox.com/v2/users/{user_id}/games",
                                cursor=cursor, params={"limit": "50", "sortOrder": "Desc"})

    async def user_favorites_page(self, user_id: int, cursor: str = ""):
        return await self._page(f"https://games.roblox.com/v2/users/{user_id}/favorite/games",
                                cursor=cursor, params={"limit": "50", "sortOrder": "Desc"})

    async def group_members_page(self, group_id: int, cursor: str = "", order: str = "Desc"):
        return await self._page(f"https://groups.roblox.com/v1/groups/{group_id}/users",
                                cursor=cursor, params={"limit": "100", "sortOrder": order})

    async def group_games_page(self, group_id: int, cursor: str = ""):
        return await self._page(f"https://games.roblox.com/v2/groups/{group_id}/games",
                                cursor=cursor, params={"limit": "50", "sortOrder": "Desc"})

    async def game_passes_page(self, universe_id: int, cursor: str = ""):
        return await self._page(f"https://games.roblox.com/v1/games/{universe_id}/game-passes",
                                cursor=cursor, params={"limit": "100", "sortOrder": "Asc"})

    async def game_badges_page(self, universe_id: int, cursor: str = ""):
        return await self._page(f"https://badges.roblox.com/v1/universes/{universe_id}/badges",
                                cursor=cursor, params={"limit": "100", "sortOrder": "Asc"})

    async def currently_wearing(self, user_id: int) -> list[int]:
        d = await self._request("GET", f"https://avatar.roblox.com/v1/users/{user_id}/currently-wearing")
        return [int(i) for i in (d.get("assetIds") or [])]

    # ---------- audio file download ----------

    async def audio_download_url(self, asset_id: int) -> str:
        """Finds the CDN location of the audio. The first attempt copies what the BTRoblox extension does for
        audio: GET /v2/asset/?id=... with the cookie and the Roblox-Browser-Asset-Request header."""
        place = {"Roblox-Place-Id": str(CFG["PlaceId"])}
        attempts = [
            ("https://assetdelivery.roblox.com/v2/asset/", {"id": str(asset_id)},
             {"Roblox-Browser-Asset-Request": "true"}),
            (f"https://assetdelivery.roblox.com/v2/assetId/{asset_id}", None, place),
        ]
        errors = []
        for url, params, hdrs in attempts:
            try:
                d = await self._request("GET", url, params=params, extra_headers=hdrs)
            except (AuthError, ChallengeRequired):
                raise
            except RobloxError as e:
                errors.append(str(e))
                continue
            for loc in d.get("locations") or []:
                if loc.get("location"):
                    return loc["location"]
            errors.append(str(d.get("errors") or "no download location returned")[:150])

        # v1 fallback: it answers with a redirect to the CDN. Don't follow it with our cookie attached.
        try:
            async with self._session.get(
                "https://assetdelivery.roblox.com/v1/asset/",
                params={"id": str(asset_id)}, allow_redirects=False,
                headers={"Roblox-Browser-Asset-Request": "true", **place},
            ) as res:
                loc = res.headers.get("Location")
                if res.status in (301, 302, 303, 307, 308) and loc:
                    return loc
                errors.append(f"v1 fallback: {res.status}")
        except aiohttp.ClientError as e:
            errors.append(f"network error: {e}")
        raise RobloxError(" | ".join(errors))

    async def download_audio(self, asset_id: int) -> bytes:
        url = await self.audio_download_url(asset_id)
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        allowed = ("rbxcdn.com", "roblox.com")
        if parsed.scheme != "https" or not any(host == d or host.endswith("." + d) for d in allowed):
            raise RobloxError(f"Asset delivery pointed to an unexpected host: {host or 'unknown'}")

        buf = bytearray()
        try:
            async with self._plain.get(url) as res:
                if not res.ok:
                    raise RobloxError(f"{res.status} downloading the audio file")
                async for part in res.content.iter_chunked(64 * 1024):
                    buf.extend(part)
                    if len(buf) > CFG["MaxAudioBytes"]:
                        raise RobloxError("Audio file is too large to analyze.")
        except aiohttp.ClientError as e:
            raise RobloxError(f"network error downloading audio: {e}")
        return bytes(buf)


# ================================================================
# HELPERS
# ================================================================

def _find_key(obj, names: tuple):
    """First scalar value stored under any of `names`, searching nested dicts/lists."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k in names and v not in (None, "") and not isinstance(v, (dict, list)):
                    return v
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None
