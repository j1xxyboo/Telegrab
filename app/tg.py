"""Telethon client pool: one MTProto client per browser session.

Every browser gets a random session id (cookie). That id maps to a Telethon
SQLite session file on disk plus a live client in memory. Because the client
talks MTProto directly, media download is not limited by the "restrict saving
content" flag that the official apps enforce — the server hands us the file
reference as long as the account is allowed to view the message.
"""
from __future__ import annotations

import asyncio
import io
import logging
import mimetypes
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Optional

from telethon import TelegramClient, errors, utils
from telethon.tl import functions, types
from telethon.tl.types import (
    InputMessagesFilterDocument,
    InputMessagesFilterPhotos,
    InputMessagesFilterVideo,
)

from . import config, qr

log = logging.getLogger("tegrab")

# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

VIDEO_MIMES = ("video/",)
AUDIO_MIMES = ("audio/",)

KIND_EXT = {
    "photo": ".jpg",
    "video": ".mp4",
    "animation": ".mp4",
    "voice": ".ogg",
    "audio": ".mp3",
    "sticker": ".webp",
    "file": "",
}

# Telegram caps concurrent GetFile calls per DC; thumbnails are fired in bursts
# by the gallery grid, so keep them polite.
THUMB_CONCURRENCY = 4
_thumb_sem: Optional[asyncio.Semaphore] = None

# Cached message objects (thumbnail lookups, streamed downloads). File
# references stay valid for about an hour, so a few minutes here is safe.
MESSAGE_CACHE_TTL = 300
MESSAGE_CACHE_MAX = 600


def _thumb_limit() -> asyncio.Semaphore:
    global _thumb_sem
    if _thumb_sem is None:
        _thumb_sem = asyncio.Semaphore(THUMB_CONCURRENCY)
    return _thumb_sem


def media_size(message) -> Optional[int]:
    """Exact byte length of the file this message points at.

    ``message.file.size`` lies for photos that have a ``PhotoSizeProgressive``
    entry: it reports the largest plain ``PhotoSize`` while Telegram returns the
    progressive variant, which is a different (usually smaller) number of bytes.
    Declaring that wrong length makes the browser abort the transfer with
    "network error", so ask Telethon's own location resolver instead — it is the
    same code path ``iter_download`` uses to pick the bytes.
    """
    media = getattr(message, "media", None)
    if media is not None:
        try:
            info = utils._get_file_info(media)
            if info.size:
                return int(info.size)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("could not resolve file info: %r", exc)
    size = getattr(getattr(message, "file", None), "size", None)
    return int(size) if size else None


def _thumb_sort_key(size):
    """Same ordering Telethon uses in ``TelegramClient._get_thumb``."""
    if isinstance(size, (types.PhotoStrippedSize, types.PhotoCachedSize)):
        return (1, len(size.bytes))
    if isinstance(size, types.PhotoSize):
        return (1, size.size)
    if isinstance(size, types.PhotoSizeProgressive):
        return (1, max(size.sizes or [0]))
    if isinstance(size, types.VideoSize):
        return (2, size.size)
    return (0, 0)


def thumb_index(message, target: int = 320) -> Optional[int]:
    """Pick a grid-sized preview instead of the full-resolution image.

    ``thumb=-1`` makes Telethon fetch the largest size, which for a photo can be
    the entire original (hundreds of KB) — a 48-tile grid then pulls tens of MB
    through MTProto and crawls. Choose the smallest size whose longest edge
    covers ``target`` px. ``-1`` remains the fallback for documents, whose
    thumbnails are already small.
    """
    photo = getattr(message, "photo", None)
    sizes = getattr(photo, "sizes", None) if photo is not None else None
    if not sizes:
        doc = getattr(message, "document", None)
        thumbs = getattr(doc, "thumbs", None) if doc is not None else None
        return -1 if thumbs else None
    entries = [s for s in sizes if not isinstance(s, types.PhotoPathSize)]
    entries.sort(key=_thumb_sort_key)
    for index, size in enumerate(entries):
        if not isinstance(size, (types.PhotoSize, types.PhotoSizeProgressive)):
            continue
        dim = max(getattr(size, "w", 0) or 0, getattr(size, "h", 0) or 0)
        if dim >= target:
            return index
    return len(entries) - 1 if entries else None


def _safe_name(name: str) -> str:
    name = os.path.basename(name or "")
    cleaned = "".join(c for c in name if c.isprintable() and c not in '\\/:*?"<>|\r\n\t')
    return cleaned.strip() or "file"


def media_kind(message) -> Optional[str]:
    if message is None:
        return None
    if getattr(message, "photo", None):
        return "photo"
    if getattr(message, "video", None):
        return "video"
    if getattr(message, "gif", None):
        return "animation"
    if getattr(message, "voice", None):
        return "voice"
    if getattr(message, "audio", None):
        return "audio"
    if getattr(message, "sticker", None):
        return "sticker"
    if getattr(message, "document", None):
        return "file"
    return None


def media_filename(message, kind: str) -> str:
    file = getattr(message, "file", None)
    name = getattr(file, "name", None) if file else None
    if name:
        return _safe_name(name)
    ext = KIND_EXT.get(kind, "")
    if not ext and file is not None:
        ext = mimetypes.guess_extension(file.mime_type or "") or ""
    return f"{kind if kind != 'file' else 'document'}_{message.id}{ext}"


def message_payload(message) -> dict:
    kind = media_kind(message)
    file = getattr(message, "file", None)
    media = None
    if kind:
        media = {
            "kind": kind,
            "size": media_size(message) or 0,
            "name": media_filename(message, kind),
            "mime": getattr(file, "mime_type", None) if file else None,
            "duration": getattr(file, "duration", None) if file else None,
            "width": getattr(file, "width", None) if file else None,
            "height": getattr(file, "height", None) if file else None,
        }
    return {
        "id": message.id,
        "date": message.date.isoformat() if message.date else None,
        "text": message.message or "",
        "out": bool(message.out),
        "grouped_id": str(message.grouped_id) if getattr(message, "grouped_id", None) else None,
        "media": media,
    }


# --------------------------------------------------------------------------- #
# Caches (thumbnails / avatars) — plain files on disk, LRU-ish eviction
# --------------------------------------------------------------------------- #

def _cache_path(name: str) -> Path:
    return config.CACHE_DIR / name


def _cache_evict() -> None:
    files = sorted(config.CACHE_DIR.glob("*.bin"), key=lambda p: p.stat().st_mtime)
    while len(files) > config.THUMB_CACHE_MAX:
        oldest = files.pop(0)
        try:
            oldest.unlink()
        except OSError:
            pass


async def cached_thumbnail(session: "UserSession", message) -> Optional[bytes]:
    key = f"t_{session.sid[:12]}_{utils.get_peer_id(message.peer_id)}_{message.id}.bin"
    path = _cache_path(key)
    if path.is_file():
        try:
            path.touch()
            return path.read_bytes()
        except OSError:
            pass
    task = _thumb_inflight.get(key)
    if task is None:
        task = asyncio.create_task(_fetch_thumbnail(session, message, path))
        _thumb_inflight[key] = task

        def _done(fut: asyncio.Task, key=key) -> None:
            _thumb_inflight.pop(key, None)
            if not fut.cancelled():
                fut.exception()  # mark retrieved, avoid "never awaited" warnings

        task.add_done_callback(_done)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        raise
    except Exception:
        return None


_thumb_inflight: dict[str, asyncio.Task] = {}


async def _fetch_thumbnail(session: "UserSession", message, path: Path) -> Optional[bytes]:
    index = thumb_index(message)
    if index is None:
        return None
    async with _thumb_limit():
        buf = io.BytesIO()
        try:
            await session.client.download_media(message, file=buf, thumb=index)
        except Exception as exc:
            log.debug("thumbnail %s/%s failed: %r", utils.get_peer_id(message.peer_id), message.id, exc)
            return None
    data = buf.getvalue()
    if not data:
        return None
    try:
        path.write_bytes(data)
        _cache_evict()
    except OSError:
        pass
    return data


async def cached_avatar(client: TelegramClient, entity) -> Optional[bytes]:
    peer_id = utils.get_peer_id(entity)
    path = _cache_path(f"a_{peer_id}.bin")
    if path.is_file():
        try:
            path.touch()
            return path.read_bytes()
        except OSError:
            pass
    buf = io.BytesIO()
    try:
        await client.download_profile_photo(entity, file=buf)
    except Exception:
        return None
    data = buf.getvalue()
    if not data:
        return None
    try:
        path.write_bytes(data)
        _cache_evict()
    except OSError:
        pass
    return data


# --------------------------------------------------------------------------- #
# Per-browser session
# --------------------------------------------------------------------------- #

DELIVERY = {
    "SentCodeTypeApp": ("app", "in the Telegram app"),
    "SentCodeTypeSms": ("sms", "by SMS"),
    "SentCodeTypeCall": ("call", "by phone call"),
    "SentCodeTypeFlashCall": ("flash_call", "by flash call"),
    "SentCodeTypeMissedCall": ("missed_call", "by missed call"),
    "SentCodeTypeEmailCode": ("email", "by email"),
    "SentCodeTypeFragmentSms": ("sms", "by SMS"),
    "SentCodeTypeFirebaseSms": ("sms", "by SMS"),
    "SentCodeTypeSmsWord": ("sms", "by SMS"),
    "SentCodeTypeSmsPhrase": ("sms", "by SMS"),
}

HINTS = {
    "app": (
        "Telegram sent the code inside the app. On the PC where you are signed in, open the "
        "chat named “Telegram” (the official service chat, id 777000 — check Archived chats; "
        "sending a code un-archives it). If no code shows there, switch to the QR code tab: "
        "scanning it from Settings → Devices → Link Desktop Device needs no code at all."
    ),
    "sms": "Check the SMS on that number. It can take up to a minute.",
    "call": "Telegram will call that number and read the code out.",
    "flash_call": "Telegram will ring that number; the code is in the calling number.",
    "missed_call": "Telegram will ring that number and hang up; the last digits are the code.",
    "email": "Check the email address registered on your Telegram account.",
    "unknown": (
        "Telegram accepted the request but did not say how it delivered the code. "
        "Use the QR code tab instead — it needs no code."
    ),
}


def describe_sent(sent) -> dict:
    kind = type(getattr(sent, "type", None)).__name__
    channel, label = DELIVERY.get(kind, ("unknown", "through an unknown channel"))
    next_kind = type(getattr(sent, "next_type", None)).__name__
    next_label = DELIVERY.get(next_kind, (None, None))[1]
    hint = HINTS.get(channel, "Telegram accepted the request.")
    if channel == "app" and next_label:
        hint += f" If nothing arrives, Telegram will retry {next_label}."
    return {
        "delivery": channel,
        "delivery_label": label,
        "hint": hint,
        "retry_after": getattr(sent, "timeout", None),
    }


class LoginError(Exception):
    def __init__(self, message: str, *, flood: int = 0):
        super().__init__(message)
        self.message = message
        self.flood = flood


class UserSession:
    def __init__(self, sid: str):
        self.sid = sid
        # Telethon appends ".session" to the filename it is given; name it
        # explicitly so deletes and restart-reattach use the real path.
        self.path = config.SESSIONS_DIR / f"{sid}.session"
        self.client = TelegramClient(
            str(self.path),
            config.API_ID,
            config.API_HASH,
            device_model="TeleGrab Web",
            system_version="1.0",
            app_version="1.0",
            lang_code="en",
            system_lang_code="en",
            connection_retries=3,
            request_retries=3,
        )
        self.lock = asyncio.Lock()
        self.connect_lock = asyncio.Lock()
        self.qr_lock = asyncio.Lock()
        self.phone: Optional[str] = None
        self.code_hash: Optional[str] = None
        self.delivery: Optional[dict] = None
        self.last_seen = time.time()
        self.closing = False
        # QR login state
        self.qr = None                      # telethon.tl.custom.qrlogin.QRLogin
        self.qr_task: Optional[asyncio.Task] = None
        self.qr_state: dict = {"status": "idle"}
        self.qr_image: Optional[str] = None
        self.qr_image_url: Optional[str] = None
        # Recently fetched messages: the gallery asks for a thumbnail per tile,
        # and each miss costs a getMessages round-trip. Keyed by (peer, id).
        self._msg_cache: dict[tuple[int, int], tuple[float, object]] = {}

    # -- lifecycle ---------------------------------------------------------- #

    async def ensure_connected(self) -> None:
        async with self.connect_lock:
            if not self.client.is_connected():
                await self.client.connect()

    async def is_authorized(self) -> bool:
        try:
            await self.ensure_connected()
            return await self.client.is_user_authorized()
        except Exception:
            return False

    async def close(self) -> None:
        self.closing = True
        await self._cancel_qr()
        try:
            await self.client.disconnect()
        except Exception:
            pass

    async def discard(self) -> None:
        """Disconnect and delete the session file (used for idle expiry)."""
        await self.close()
        for suffix in ("", "-journal", "-wal", "-shm"):
            try:
                Path(str(self.path) + suffix).unlink()
            except OSError:
                pass

    async def wipe(self) -> None:
        """Revoke the login and delete the local session file."""
        try:
            if self.client.is_connected() and await self.client.is_user_authorized():
                await self.client.log_out()
        except Exception:
            pass
        await self.close()
        for suffix in ("", "-journal", "-wal", "-shm"):
            try:
                Path(str(self.path) + suffix).unlink()
            except OSError:
                pass

    # -- login -------------------------------------------------------------- #

    async def _cancel_qr(self) -> None:
        task, self.qr_task = self.qr_task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def start_qr(self) -> dict:
        """Begin (or restart) a QR login: no code, no SMS, no phone number.

        The browser shows ``tg://login?token=…`` as a QR code; the user scans it
        from *any* already signed-in Telegram app, which authorises this MTProto
        client directly. That is the same "link a device" handshake Telegram
        Desktop and Web use, so it works even when login codes never arrive.
        """
        async with self.qr_lock:
            if await self.is_authorized():
                self.qr_state = {"status": "ok"}
                return await self.public_qr()
            try:
                await self.ensure_connected()
            except Exception as exc:
                log.warning("qr connect failed: %r", exc)
                self.qr_state = {
                    "status": "error",
                    "error": f"Cannot reach Telegram from the server ({type(exc).__name__}: {exc}).",
                }
                return await self.public_qr()

            await self._cancel_qr()
            self.qr_state = {"status": "starting"}
            try:
                self.qr = await asyncio.wait_for(self.client.qr_login(), timeout=45)
            except asyncio.TimeoutError:
                self.qr_state = {"status": "error", "error": "Telegram did not answer within 45 s."}
                return await self.public_qr()
            except errors.ApiIdInvalidError:
                self.qr_state = {
                    "status": "error",
                    "error": "Telegram rejected TELEGRAM_API_ID / TELEGRAM_API_HASH.",
                }
                return await self.public_qr()
            except errors.FloodWaitError as exc:
                self.qr_state = {
                    "status": "error",
                    "error": f"Telegram is rate-limiting logins. Wait {exc.seconds}s.",
                }
                return await self.public_qr()
            except Exception as exc:
                log.exception("qr_login failed")
                self.qr_state = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
                return await self.public_qr()

            self.qr_state = {"status": "pending"}
            self.qr_task = asyncio.create_task(self._qr_loop(self.qr), name=f"qr-{self.sid[:8]}")
            await self.public_qr()  # populates self.qr_image for the first token
            log.info("qr login token issued for session %s", self.sid[:8])
            return await self.public_qr()

    async def _qr_loop(self, qr_login) -> None:
        """Keep waiting on the token, rolling it over when it expires."""
        while not self.closing:
            try:
                await qr_login.wait()
            except asyncio.CancelledError:
                raise
            except errors.SessionPasswordNeededError:
                log.info("qr scanned, two-step password required (%s)", self.sid[:8])
                self.qr_state = {"status": "password_needed"}
                return
            except (asyncio.TimeoutError, TimeoutError):
                # Token expired unscanned: mint a fresh one and keep going.
                try:
                    await qr_login.recreate()
                except errors.SessionPasswordNeededError:
                    self.qr_state = {"status": "password_needed"}
                    return
                except errors.FloodWaitError as exc:
                    self.qr_state = {
                        "status": "error",
                        "error": f"Telegram is rate-limiting logins. Wait {exc.seconds}s.",
                    }
                    return
                except Exception as exc:
                    log.warning("qr token refresh failed: %r", exc)
                    self.qr_state = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
                    return
                self.qr_state = {"status": "pending"}
                self.qr_image_url = None
                log.info("qr token refreshed for session %s", self.sid[:8])
                continue
            except Exception as exc:
                log.exception("qr wait failed")
                self.qr_state = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
                return

            self.last_seen = time.time()
            self.qr_state = {"status": "ok"}
            log.info("qr login succeeded for session %s", self.sid[:8])
            return

    async def public_qr(self) -> dict:
        """Serialisable QR state for the browser (URL + rendered PNG)."""
        state = dict(self.qr_state)
        if state.get("status") == "ok":
            try:
                state["me"] = await self.me()
            except Exception:
                state["status"] = "pending"
        if state.get("status") == "pending" and self.qr is not None:
            try:
                url = self.qr.url
            except Exception:
                return state
            if url != self.qr_image_url:
                self.qr_image_url = url
                self.qr_image = qr.data_uri(url)
            state["url"] = url
            state["image"] = self.qr_image
            try:
                left = (self.qr.expires - datetime.now(timezone.utc)).total_seconds()
                state["expires_in"] = max(0, int(left))
            except Exception:
                pass
        return state

    async def send_code(self, phone: str) -> dict:
        try:
            await self.ensure_connected()
        except Exception as exc:
            log.warning("connect failed: %r", exc)
            raise LoginError(
                f"Cannot reach Telegram from the server ({type(exc).__name__}: {exc}). "
                "If this machine needs a proxy, set HTTPS_PROXY and restart."
            )
        if await self.client.is_user_authorized():
            return {"status": "authorized"}
        async with self.lock:
            try:
                sent = await asyncio.wait_for(self.client.send_code_request(phone), timeout=45)
            except asyncio.TimeoutError:
                raise LoginError("Telegram did not answer within 45 s. Check the server's network or proxy.")
            except errors.FloodWaitError as exc:
                raise LoginError(f"Telegram is rate-limiting code requests. Wait {exc.seconds}s.", flood=exc.seconds)
            except errors.PhoneNumberInvalidError:
                raise LoginError("That phone number is not valid. Use international format: +<country><number>.")
            except errors.PhoneNumberBannedError:
                raise LoginError("Telegram has banned this phone number.")
            except errors.ApiIdInvalidError:
                raise LoginError("Telegram rejected TELEGRAM_API_ID / TELEGRAM_API_HASH. Recheck the values from my.telegram.org.")
            except errors.SendCodeUnavailableError:
                raise LoginError("Telegram is refusing to send codes to this number right now. Try again later.")
            except Exception as exc:
                log.exception("send_code_request failed for %s", phone)
                raise LoginError(f"Telegram rejected the login request ({type(exc).__name__}): {exc}")
            self.phone = phone
            self.code_hash = sent.phone_code_hash or self.code_hash
            self.delivery = describe_sent(sent)
        log.info("code sent for %s via %s", phone, self.delivery.get("delivery"))
        return {"status": "code_sent", **self.delivery}

    async def resend_code(self) -> dict:
        if not self.phone:
            raise LoginError("Request a code first.")
        return await self.send_code(self.phone)

    async def submit_code(self, code: str) -> dict:
        if not self.phone or not self.code_hash:
            raise LoginError("Request a login code first.")
        async with self.lock:
            try:
                await self.client.sign_in(self.phone, code=code.strip(), phone_code_hash=self.code_hash)
            except errors.SessionPasswordNeededError:
                return {"status": "password_needed"}
            except errors.PhoneCodeInvalidError:
                raise LoginError("Wrong code, try again.")
            except errors.PhoneCodeExpiredError:
                raise LoginError("That code expired — request a new one.")
            except errors.PhoneNumberUnoccupiedError:
                raise LoginError("No Telegram account exists for that number — sign up in the app first.")
            except errors.PhoneNumberBannedError:
                raise LoginError("Telegram has banned this phone number.")
            except errors.FloodWaitError as exc:
                raise LoginError(f"Too many attempts. Retry in {exc.seconds}s.", flood=exc.seconds)
            except Exception as exc:
                raise LoginError(f"Sign-in failed: {exc}")
            self.code_hash = None
        return {"status": "ok", "me": await self.me()}

    async def submit_password(self, password: str) -> dict:
        async with self.lock:
            try:
                await self.client.sign_in(password=password)
            except errors.PasswordHashInvalidError:
                raise LoginError("Wrong two-step password.")
            except errors.FloodWaitError as exc:
                raise LoginError(f"Too many attempts. Retry in {exc.seconds}s.", flood=exc.seconds)
            except Exception as exc:
                raise LoginError(f"Sign-in failed: {exc}")
        self.qr_state = {"status": "ok"}
        return {"status": "ok", "me": await self.me()}

    async def me(self) -> dict:
        user = await self.client.get_me()
        if user is None:
            raise LoginError("Not signed in.")
        name = " ".join(p for p in (user.first_name, user.last_name) if p) or (user.username or str(user.id))
        return {
            "id": user.id,
            "name": name,
            "username": user.username,
            "phone": user.phone,
        }

    # -- reading ------------------------------------------------------------ #

    async def dialogs(self, limit: int = 300) -> list[dict]:
        out: list[dict] = []
        async for dialog in self.client.iter_dialogs(limit=limit):
            entity = dialog.entity
            if getattr(entity, "broadcast", False):
                ctype = "channel"
            elif getattr(entity, "megagroup", False):
                ctype = "group"
            elif getattr(entity, "gigagroup", False):
                ctype = "group"
            elif dialog.is_user:
                ctype = "user"
            elif dialog.is_group:
                ctype = "group"
            else:
                ctype = "chat"
            last = dialog.message
            preview = ""
            if last is not None:
                text = (getattr(last, "message", "") or "").strip().splitlines()
                kind = media_kind(last)
                label = {"photo": "Photo", "video": "Video", "animation": "GIF", "voice": "Voice message",
                         "audio": "Audio", "sticker": "Sticker", "file": "File"}.get(kind or "", "")
                preview = text[0] if text else label
                if text and label:
                    preview = f"{label}, {preview}"
            mute_until = getattr(getattr(dialog.dialog, "notify_settings", None), "mute_until", None)
            if isinstance(mute_until, datetime):
                muted = mute_until > datetime.now(timezone.utc)
            else:
                muted = bool(mute_until and mute_until > time.time())
            out.append(
                {
                    "id": utils.get_peer_id(entity),
                    "title": dialog.title or "Saved messages",
                    "type": ctype,
                    "username": getattr(entity, "username", None),
                    "unread": dialog.unread_count or 0,
                    "pinned": bool(getattr(dialog, "pinned", False)),
                    "restricted": bool(getattr(entity, "restricted", False)),
                    "noforwards": bool(getattr(entity, "noforwards", False)),
                    "date": dialog.date.isoformat() if dialog.date else None,
                    "preview": preview[:120],
                    "muted": muted,
                }
            )
        return out

    async def entity(self, peer: int):
        return await self.client.get_entity(peer)

    async def messages(
        self,
        peer: int,
        *,
        limit: int = 48,
        offset_id: int = 0,
        kind: str = "media",
        search: str = "",
    ) -> dict:
        entity = await self.client.get_entity(peer)
        filter_ = None
        if kind == "photo":
            filter_ = InputMessagesFilterPhotos()
        elif kind == "video":
            filter_ = InputMessagesFilterVideo()
        elif kind == "file":
            filter_ = InputMessagesFilterDocument()

        kwargs = {"limit": limit, "offset_id": offset_id}
        if search:
            kwargs["search"] = search

        items: list = []
        if kind == "media":
            # Merge photo+video+document result sets by id.
            seen: set[int] = set()
            for flt in (InputMessagesFilterPhotos(), InputMessagesFilterVideo(), InputMessagesFilterDocument()):
                batch = await self.client.get_messages(entity, filter=flt, **kwargs)
                for m in batch:
                    if m.id in seen or not media_kind(m):
                        continue
                    seen.add(m.id)
                    items.append(m)
            items.sort(key=lambda m: m.id, reverse=True)
            items = items[:limit]
            next_offset = items[-1].id if len(items) == limit else 0
        else:
            batch = await self.client.get_messages(entity, filter=filter_, **kwargs)
            items = [m for m in batch if media_kind(m)]
            next_offset = items[-1].id if len(items) == limit else 0

        for item in items:
            self._remember(item)
        return {
            "messages": [message_payload(m) for m in items],
            "next_offset": next_offset,
        }

    def _remember(self, message) -> None:
        if message is None or not getattr(message, "id", None):
            return
        try:
            peer = utils.get_peer_id(message.peer_id)
        except Exception:
            return
        if len(self._msg_cache) >= MESSAGE_CACHE_MAX:
            self._msg_cache.clear()
        self._msg_cache[(peer, message.id)] = (time.time(), message)

    def _recall(self, peer: int, message_id: int):
        hit = self._msg_cache.get((peer, message_id))
        if hit is not None:
            if time.time() - hit[0] < MESSAGE_CACHE_TTL:
                return hit[1]
            self._msg_cache.pop((peer, message_id), None)
        return None

    async def message(self, peer: int, message_id: int, *, fresh: bool = False):
        if not fresh:
            cached = self._recall(peer, message_id)
            if cached is not None:
                return cached
        entity = await self.client.get_entity(peer)
        msg = await self.client.get_messages(entity, ids=message_id)
        if isinstance(msg, list):
            msg = msg[0] if msg else None
        self._remember(msg)
        return msg

    # -- download ----------------------------------------------------------- #

    async def iter_media(self, message, start: int = 0, length: Optional[int] = None) -> AsyncIterator[bytes]:
        """Stream bytes of ``message``'s media, refreshing expired file refs once."""
        sent = 0
        refreshed = False
        current = message
        while True:
            offset = start + sent
            try:
                async for chunk in self.client.iter_download(
                    current.media, offset=offset, request_size=config.CHUNK_SIZE
                ):
                    # A long download is activity: keep the idle reaper away.
                    self.last_seen = time.time()
                    if length is not None:
                        left = length - sent
                        if left <= 0:
                            return
                        if len(chunk) > left:
                            chunk = chunk[:left]
                    sent += len(chunk)
                    yield bytes(chunk)
                    if length is not None and sent >= length:
                        return
                return
            except errors.FileReferenceExpiredError:
                # Telegram file references die after ~an hour; the cached
                # message may be old, so re-fetch it and resume where we are.
                if refreshed:
                    raise
                refreshed = True
                fresh = await self.message(utils.get_peer_id(current.peer_id), current.id, fresh=True)
                if fresh is None or getattr(fresh, "media", None) is None:
                    raise
                current = fresh


# --------------------------------------------------------------------------- #
# Pool
# --------------------------------------------------------------------------- #

_SID_RE = re.compile(r"^[0-9a-f]{48}$")


class SessionPool:
    def __init__(self) -> None:
        self.sessions: dict[str, UserSession] = {}

    def create(self) -> UserSession:
        sid = os.urandom(24).hex()
        session = UserSession(sid)
        self.sessions[sid] = session
        return session

    def get(self, sid: Optional[str]) -> Optional[UserSession]:
        """Return the live session for a cookie, reattaching from disk after a restart."""
        if not sid or not _SID_RE.match(sid):
            return None
        session = self.sessions.get(sid)
        if session is None:
            if (config.SESSIONS_DIR / f"{sid}.session").is_file():
                session = UserSession(sid)
                self.sessions[sid] = session
            else:
                return None
        session.last_seen = time.time()
        return session

    def drop(self, sid: str) -> None:
        self.sessions.pop(sid, None)

    async def reap(self) -> None:
        now = time.time()
        for sid, session in list(self.sessions.items()):
            if now - session.last_seen > config.SESSION_TTL:
                self.sessions.pop(sid, None)
                await session.discard()

    async def shutdown(self) -> None:
        for session in list(self.sessions.values()):
            await session.close()
        self.sessions.clear()


POOL = SessionPool()


# --------------------------------------------------------------------------- #
# Connectivity probe (used by /api/diag and the login screen's test button)
# --------------------------------------------------------------------------- #

async def probe() -> dict:
    from telethon.sessions import StringSession

    if not config.credentials_ok():
        return {"ok": False, "error": "Telegram API credentials are missing or still the .env.example placeholders."}

    client = TelegramClient(
        StringSession(), config.API_ID, config.API_HASH,
        device_model="TeleGrab probe", system_version="1.0", app_version="1.0", timeout=15,
    )
    started = time.perf_counter()
    try:
        await asyncio.wait_for(client.connect(), timeout=20)
        latency = int((time.perf_counter() - started) * 1000)
        authorized = await client.is_user_authorized()
        return {
            "ok": True,
            "latency_ms": latency,
            "dc": client.session.dc_id,
            "authorized": authorized,
        }
    except Exception as exc:
        log.warning("probe failed: %r", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def raw_send_probe(phone: str) -> dict:
    """Call ``auth.sendCode`` directly and report the exact outcome.

    Telethon's ``send_code_request`` wraps this call, so anything it does —
    including a delivery channel you did not expect — is visible here as the
    literal ``SentCode`` the server returned.
    """
    from telethon.sessions import StringSession

    parsed = utils.parse_phone(phone) or phone
    out: dict = {"ok": False, "input": phone, "parsed_phone": parsed, "api_id": config.API_ID}
    if not config.credentials_ok():
        out["error"] = "Telegram API credentials are missing or still placeholders."
        return out

    client = TelegramClient(
        StringSession(), config.API_ID, config.API_HASH,
        device_model="TeleGrab diag", system_version="1.0", app_version="1.0",
        connection_retries=3, request_retries=3, timeout=20,
    )
    try:
        await asyncio.wait_for(client.connect(), timeout=30)
        sent = await asyncio.wait_for(
            client(
                functions.auth.SendCodeRequest(
                    phone=parsed,
                    api_id=config.API_ID,
                    api_hash=config.API_HASH,
                    settings=types.CodeSettings(),
                )
            ),
            timeout=45,
        )
        sent_type = getattr(sent, "type", None)
        kind = type(sent_type).__name__
        out.update(
            {
                "ok": True,
                "sent_type": kind,
                "delivery": DELIVERY.get(kind, ("unknown", ""))[0],
                "code_length": getattr(sent_type, "length", None),
                "timeout_s": getattr(sent, "timeout", None),
                "next_type": type(getattr(sent, "next_type", None)).__name__,
                "sent_repr": repr(sent)[:600],
            }
        )
        log.info("raw send probe: %s -> %s (%s)", parsed, kind, out["delivery"])
    except asyncio.TimeoutError:
        out["error_type"] = "TimeoutError"
        out["error"] = "Telegram did not answer within 45 s."
    except Exception as exc:
        out["error_type"] = type(exc).__name__
        out["error"] = str(exc)
        log.warning("raw send probe failed: %s: %s", type(exc).__name__, exc)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    return out

