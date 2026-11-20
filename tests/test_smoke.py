"""Offline smoke test: exercises the HTTP layer with a fake Telegram session.

Run: python3 tests/test_smoke.py
No network, no credentials, no real account needed.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, main, tg  # noqa: E402

# Pretend credentials exist so /api/status takes the normal path.
config.API_ID, config.API_HASH = 1, "deadbeef"

BLOB = bytes(range(256)) * 40  # 10240 bytes


class FakeSession:
    sid = "f" * 48
    last_seen = 0.0
    client = None

    async def is_authorized(self):
        return True

    async def me(self):
        return {"id": 1, "name": "Test User", "username": "tester", "phone": "100"}

    async def resend_code(self):
        return {"status": "code_sent", "delivery": "app", "delivery_label": "in the Telegram app",
                "hint": "check the Telegram chat", "retry_after": 60}

    async def dialogs(self, limit=300):
        return [
            {"id": -1001234567890, "title": "Locked Channel", "type": "channel",
             "username": "locked", "unread": 0, "pinned": False,
             "restricted": True, "noforwards": True},
        ]

    def _message(self):
        return SimpleNamespace(
            id=42,
            date=dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc),
            message="caption here",
            out=False,
            grouped_id=None,
            photo=None,
            video=SimpleNamespace(),
            gif=None, voice=None, audio=None, sticker=None,
            document=SimpleNamespace(),
            media=SimpleNamespace(),
            file=SimpleNamespace(size=len(BLOB), name="clip final.mp4", mime_type="video/mp4",
                                 duration=63.5, width=1920, height=1080),
        )

    async def messages(self, peer, *, limit=48, offset_id=0, kind="media", search=""):
        return {
            "messages": [tg.message_payload(self._message())],
            "next_offset": 0,
        }

    async def message(self, peer, message_id):
        return self._message() if message_id == 42 else None

    async def entity(self, peer):
        return SimpleNamespace(id=peer)

    async def iter_media(self, media, start=0, length=None):
        data = BLOB[start:None if length is None else start + length]
        step = 4096
        for i in range(0, len(data), step):
            yield data[i:i + step]


def fail(msg: str) -> None:
    print("FAIL:", msg)
    raise SystemExit(1)


async def run() -> None:
    fake = FakeSession()
    tg.POOL.sessions[fake.sid] = fake  # type: ignore[assignment]
    transport = httpx.ASGITransport(app=main.app)
    cookies = {config.COOKIE_NAME: fake.sid}
    async with httpx.AsyncClient(transport=transport, base_url="http://test", cookies=cookies) as client:
        r = await client.get("/api/status")
        assert r.status_code == 200, r.text
        assert r.json()["authorized"] is True, r.text

        r = await client.get("/api/dialogs")
        assert r.status_code == 200, r.text
        dialog = r.json()["dialogs"][0]
        if not dialog["noforwards"]:
            fail("noforwards flag not passed through")
        print("ok  dialogs:", dialog["title"], "| restricted:", dialog["restricted"])

        r = await client.get("/api/dialogs/-1001234567890/messages")
        assert r.status_code == 200, r.text
        msg = r.json()["messages"][0]
        assert msg["media"]["kind"] == "video", msg
        assert msg["media"]["name"] == "clip final.mp4", msg
        assert msg["media"]["size"] == len(BLOB), msg
        print("ok  messages: kind=%s name=%r size=%d" % (msg["media"]["kind"], msg["media"]["name"], msg["media"]["size"]))

        # full download (forced attachment)
        r = await client.get("/api/media/-1001234567890/42")
        assert r.status_code == 200, r.text
        assert r.content == BLOB, (len(r.content), len(BLOB))
        assert int(r.headers["content-length"]) == len(r.content), (
            "Content-Length must equal the bytes actually sent, or browsers abort the download"
        )
        disp = r.headers["content-disposition"]
        assert disp.startswith("attachment;"), disp
        assert "clip%20final.mp4" in disp, disp
        assert r.headers["content-type"] == "video/mp4"
        print("ok  full download:", len(r.content), "bytes |", disp)

        # inline preview
        r = await client.get("/api/media/-1001234567890/42?dl=0")
        assert r.headers["content-disposition"].startswith("inline;")

        # range request
        r = await client.get("/api/media/-1001234567890/42", headers={"Range": "bytes=100-199"})
        assert r.status_code == 206, r.status_code
        assert r.content == BLOB[100:200], len(r.content)
        assert r.headers["content-range"] == f"bytes 100-199/{len(BLOB)}"
        print("ok  range 206:", r.headers["content-range"])

        # suffix range
        r = await client.get("/api/media/-1001234567890/42", headers={"Range": "bytes=-50"})
        assert r.status_code == 206 and r.content == BLOB[-50:], r.status_code

        # head
        r = await client.head("/api/media/-1001234567890/42")
        assert r.status_code == 200 and r.headers["content-length"] == str(len(BLOB)), r.status_code
        assert r.content == b""
        print("ok  HEAD:", r.headers["content-length"])

        # bad range
        r = await client.get("/api/media/-1001234567890/42", headers={"Range": "bytes=999999-"})
        assert r.status_code == 416, r.status_code
        print("ok  invalid range ->416")

        # missing message
        r = await client.get("/api/media/-1001234567890/7")
        assert r.status_code == 404, r.status_code

        # photos with a progressive size: message.file.size disagrees with the
        # bytes Telegram actually serves, and the grid must not fetch the
        # full-resolution image as its thumbnail.
        from telethon.tl import types as tl

        photo = tl.Photo(
            id=11,
            access_hash=22,
            file_reference=b"ref",
            date=dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc),
            sizes=[
                tl.PhotoSize(type="m", w=320, h=320, size=30_000),
                tl.PhotoSizeProgressive(type="p", w=1280, h=1280, sizes=[10_000, 20_000, 55_000]),
            ],
            dc_id=4,
        )
        photo_msg = SimpleNamespace(
            id=43,
            media=tl.MessageMediaPhoto(photo=photo),
            photo=photo,
            file=SimpleNamespace(size=30_000),  # what Telethon's Message.file reports
        )
        assert tg.media_size(photo_msg) == 55_000, tg.media_size(photo_msg)
        assert tg.thumb_index(photo_msg) == 0, tg.thumb_index(photo_msg)
        print("ok  photo size resolves to progressive bytes; thumbnail stays small")

        # delivery description
        from telethon.tl import types as t

        sent = SimpleNamespace(
            type=t.auth.SentCodeTypeApp(length=5),
            next_type=t.auth.SentCodeTypeSms(length=5),
            timeout=60,
        )
        d = tg.describe_sent(sent)
        assert d["delivery"] == "app" and "Telegram" in d["hint"]
        assert d["retry_after"] == 60
        assert d["delivery_label"] == "in the Telegram app"
        sms = tg.describe_sent(SimpleNamespace(type=t.auth.SentCodeTypeSms(length=5), next_type=None, timeout=30))
        assert sms["delivery"] == "sms", sms
        print("ok  delivery channel mapping:", d["delivery"], "/", sms["delivery"])

        # resend
        r = await client.post("/api/auth/resend")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "code_sent"
        print("ok  resend endpoint")

        # diag (probe stubbed so the test stays offline)
        async def fake_probe():
            return {"ok": True, "latency_ms": 5, "dc": 2, "authorized": False}

        original_probe = tg.probe
        tg.probe = fake_probe
        try:
            r = await client.get("/api/diag")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["configured"] is True and body["telegram"]["ok"] is True, body
            print("ok  diag endpoint")
        finally:
            tg.probe = original_probe

        # unauthorised peer param shape is int-validated by FastAPI
        r = await client.get("/api/media/abc/42")
        assert r.status_code == 422, r.status_code

        # QR login surface (session stubbed; no network)
        class FakeQrSession:
            sid = "a" * 48
            last_seen = 0.0
            client = None

            async def is_authorized(self):
                return False

            async def start_qr(self):
                return await self.public_qr()

            async def public_qr(self):
                url = "tg://login?token=TG9naW4tdG9rZW4"
                return {
                    "status": "pending",
                    "url": url,
                    "image": tg.qr.data_uri(url),
                    "expires_in": 29,
                }

        qr_session = FakeQrSession()
        tg.POOL.sessions[qr_session.sid] = qr_session  # type: ignore[assignment]
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test",
                cookies={config.COOKIE_NAME: qr_session.sid},
            ) as qr_client:
                r = await qr_client.post("/api/auth/qr")
                assert r.status_code == 200, r.text
                body = r.json()
                assert body["status"] == "pending", body
                assert body["image"].startswith("data:image/png;base64,"), body.get("image", "")[:40]
                assert body["url"].startswith("tg://login?token="), body
                print("ok  qr start: png", len(body["image"]), "bytes, expires", body["expires_in"])

                r = await qr_client.get("/api/auth/qr")
                assert r.status_code == 200 and r.json()["status"] == "pending", r.text
                print("ok  qr poll")

                # no cookie at all -> idle, not an error
                r = await qr_client.get("/api/auth/qr")
                assert r.status_code == 200
        finally:
            tg.POOL.sessions.pop(qr_session.sid, None)

        r = await client.get("/api/diag/log?lines=5")
        assert r.status_code == 200, r.status_code
        print("ok  diag log tail")

    # no cookie -> 401
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as anon:
        r = await anon.get("/api/dialogs")
        assert r.status_code == 401, r.status_code
        r = await anon.get("/api/auth/qr")
        assert r.status_code == 200 and r.json()["status"] == "idle", r.text
        r = await anon.get("/")
        assert r.status_code == 200 and b"TeleGrab" in r.content
        print("ok  anonymous 401 + index served")

    tg.POOL.sessions.clear()
    print("\nall good")


if __name__ == "__main__":
    asyncio.run(run())
