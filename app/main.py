"""FastAPI application: REST API + static single-page client."""
from __future__ import annotations

import asyncio
import logging
import platform
from contextlib import asynccontextmanager
import os
from logging.handlers import RotatingFileHandler
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, tg

log = logging.getLogger("tegrab")


def _setup_logging() -> None:
    """Console + rotating file log, so a failed login is readable afterwards."""
    if getattr(_setup_logging, "done", False):
        return
    _setup_logging.done = True

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    log.setLevel(logging.INFO)
    log.propagate = False
    if not any(isinstance(h, logging.StreamHandler) for h in log.handlers):
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
        log.addHandler(console)
    try:
        if not any(isinstance(h, RotatingFileHandler) for h in log.handlers):
            file_handler = RotatingFileHandler(
                config.LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
            )
            file_handler.setFormatter(fmt)
            log.addHandler(file_handler)
    except OSError as exc:  # read-only fs, etc. — console logging still works
        log.warning("cannot open log file %s: %s", config.LOG_FILE, exc)

    # Telethon logs every RPC at INFO; keep the file useful.
    logging.getLogger("telethon").setLevel(logging.WARNING)


_setup_logging()


@asynccontextmanager
async def lifespan(_: FastAPI):
    async def reaper():
        while True:
            await asyncio.sleep(300)
            try:
                await tg.POOL.reap()
            except Exception:
                pass

    log.info(
        "TeleGrab starting — api_id=%s configured=%s sessions_dir=%s log=%s",
        config.API_ID or None, config.credentials_ok(), config.SESSIONS_DIR, config.LOG_FILE,
    )
    if not config.credentials_ok():
        log.warning("Telegram credentials missing: %s", config.missing_credentials_message())

    task = asyncio.create_task(reaper())
    try:
        yield
    finally:
        task.cancel()
        await tg.POOL.shutdown()


app = FastAPI(title="TeleGrab", docs_url=None, redoc_url=None, lifespan=lifespan)

STATIC_DIR = config.BASE_DIR / "app" / "static"


# --------------------------------------------------------------------------- #
# Session plumbing
# --------------------------------------------------------------------------- #

def current(request: Request, *, required: bool = True) -> tg.UserSession | None:
    session = tg.POOL.get(request.cookies.get(config.COOKIE_NAME))
    if session is None and required:
        raise HTTPException(401, "auth_required")
    return session


async def authorized(request: Request) -> tg.UserSession:
    session = current(request)
    assert session is not None
    if not await session.is_authorized():
        raise HTTPException(401, "not_signed_in")
    return session


def set_cookie(response: Response, sid: str) -> None:
    response.set_cookie(
        config.COOKIE_NAME,
        sid,
        max_age=config.COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=config.COOKIE_SECURE,
        path="/",
    )


def login_error(exc: Exception, status: int = 400) -> HTTPException:
    if isinstance(exc, tg.LoginError):
        return HTTPException(429 if exc.flood else status, exc.message)
    log.exception("unhandled login error")
    return HTTPException(500, f"{type(exc).__name__}: {exc}")


class PhoneBody(BaseModel):
    phone: str


class CodeBody(BaseModel):
    code: str


class PasswordBody(BaseModel):
    password: str


class DiagSendBody(BaseModel):
    phone: str


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

@app.get("/api/status")
async def status(request: Request):
    if not config.credentials_ok():
        return {"configured": False, "message": config.missing_credentials_message(), "authorized": False}
    session = current(request, required=False)
    if session is None:
        return {"configured": True, "authorized": False}
    if await session.is_authorized():
        try:
            return {"configured": True, "authorized": True, "me": await session.me()}
        except Exception:
            return {"configured": True, "authorized": False}
    return {"configured": True, "authorized": False}


@app.post("/api/auth/qr")
async def auth_qr(request: Request, response: Response, fresh: int = 0):
    """Start (or restart) a QR login and return the code to display."""
    if not config.credentials_ok():
        raise HTTPException(500, config.missing_credentials_message())
    session = current(request, required=False)
    if session is None:
        session = tg.POOL.create()
    elif fresh:
        await session._cancel_qr()
        session.qr_state = {"status": "idle"}
    try:
        state = await session.start_qr()
    except Exception as exc:
        tg.POOL.drop(session.sid)
        await session.discard()
        raise login_error(exc)
    set_cookie(response, session.sid)
    return state


@app.get("/api/auth/qr")
async def auth_qr_status(request: Request):
    session = current(request, required=False)
    if session is None:
        return {"status": "idle"}
    return await session.public_qr()


@app.post("/api/auth/start")
async def auth_start(body: PhoneBody, request: Request, response: Response):
    if not config.credentials_ok():
        raise HTTPException(500, config.missing_credentials_message())
    session = current(request, required=False)
    created = session is None
    if session is None:
        session = tg.POOL.create()
    try:
        result = await session.send_code(body.phone.strip())
    except Exception as exc:
        if created:  # don't leak half-open clients / session files
            tg.POOL.drop(session.sid)
            await session.discard()
        raise login_error(exc)
    if await session.is_authorized():
        result = {"status": "ok", "me": await session.me()}
    set_cookie(response, session.sid)
    return result


@app.post("/api/auth/code")
async def auth_code(body: CodeBody, request: Request, response: Response):
    session = current(request, required=False)
    if session is None:
        raise HTTPException(401, "auth_required")
    try:
        result = await session.submit_code(body.code)
    except Exception as exc:
        raise login_error(exc)
    set_cookie(response, session.sid)
    return result


@app.post("/api/auth/password")
async def auth_password(body: PasswordBody, request: Request):
    session = current(request)
    try:
        return await session.submit_password(body.password)
    except Exception as exc:
        raise login_error(exc)


@app.post("/api/auth/resend")
async def auth_resend(request: Request, response: Response):
    session = current(request)
    try:
        result = await session.resend_code()
    except Exception as exc:
        raise login_error(exc)
    set_cookie(response, session.sid)
    return result


@app.get("/api/diag")
async def diag():
    import telethon

    info = {
        "configured": config.credentials_ok(),
        "api_id": config.API_ID or None,
        "api_hash_set": bool(config.API_HASH),
        "python": platform.python_version(),
        "telethon": telethon.__version__,
        "proxy": {
            key: val
            for key, val in os.environ.items()
            if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
        },
        "sessions_dir": str(config.SESSIONS_DIR),
        "log_file": str(config.LOG_FILE),
        "qr_available": tg.qr.data_uri("test") is not None,
        "live_sessions": len(tg.POOL.sessions),
    }
    info["telegram"] = await tg.probe()
    return info


@app.post("/api/diag/send")
async def diag_send(body: DiagSendBody):
    """Raw ``auth.sendCode`` — shows exactly what Telegram answered."""
    if not config.credentials_ok():
        raise HTTPException(500, config.missing_credentials_message())
    return await tg.raw_send_probe(body.phone.strip())


@app.get("/api/diag/log")
async def diag_log(lines: int = 200):
    lines = max(10, min(lines, 2000))
    try:
        text = config.LOG_FILE.read_text("utf-8", errors="replace")
    except FileNotFoundError:
        return PlainTextResponse("(no log file yet)")
    except OSError as exc:
        return PlainTextResponse(f"(cannot read log: {exc})")
    tail = text.splitlines()[-lines:]
    return PlainTextResponse("\n".join(tail) + "\n")


@app.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response):
    session = current(request, required=False)
    if session is not None:
        await session.wipe()
        tg.POOL.drop(session.sid)
    response.delete_cookie(config.COOKIE_NAME, path="/")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #

@app.get("/api/dialogs")
async def dialogs(request: Request, limit: int = config.DIALOG_LIMIT):
    session = await authorized(request)
    try:
        items = await session.dialogs(limit=limit)
    except Exception as exc:
        raise HTTPException(502, f"Could not load chats: {exc}")
    return {"dialogs": items}


@app.get("/api/dialogs/{peer}/messages")
async def messages(
    peer: int,
    request: Request,
    limit: int = 48,
    offset_id: int = 0,
    kind: str = "media",
    search: str = "",
):
    session = await authorized(request)
    limit = max(1, min(limit, 100))
    if kind not in {"media", "photo", "video", "file"}:
        kind = "media"
    try:
        return await session.messages(peer, limit=limit, offset_id=offset_id, kind=kind, search=search.strip())
    except Exception as exc:
        raise HTTPException(502, f"Could not load messages: {exc}")


@app.api_route("/api/media/{peer}/{message_id}", methods=["GET", "HEAD"])
async def media(peer: int, message_id: int, request: Request, dl: int = 1):
    session = await authorized(request)
    try:
        msg = await session.message(peer, message_id)
    except Exception as exc:
        raise HTTPException(502, f"Could not open message: {exc}")
    if msg is None:
        raise HTTPException(404, "No such message")
    view = tg.downloadable(msg)
    if view is None:
        info = tg.paid_info(msg)
        if info is None:
            raise HTTPException(404, "No downloadable media in that message")
        # The purchase may have landed since we last asked Telegram.
        view = tg.downloadable(await session.resolve_paid(msg, force=True))
        if view is None:
            raise HTTPException(
                402,
                f"Paid post — {info['stars']} Stars. Telegram never sends the file until the post is paid for.",
            )
    kind = tg.media_kind(view)
    if kind is None:
        raise HTTPException(404, "No downloadable media in that message")
    filename = tg.media_filename(view, kind)
    file = getattr(view, "file", None)
    mime = (getattr(file, "mime_type", None) if file else None) or (
        "image/jpeg" if kind == "photo" else "application/octet-stream"
    )
    # ``message.file.size`` is wrong for photos carrying a progressive size, and
    # a wrong Content-Length makes the browser kill the transfer ("network
    # error") once the real end of the stream arrives.
    size = tg.media_size(view)

    disposition = "attachment" if dl else "inline"
    headers = {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename)}",
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
    }

    start, end = 0, (size - 1 if size else None)
    status_code = 200
    range_header = request.headers.get("range")
    if range_header and size:
        try:
            unit, _, spec = range_header.partition("=")
            if unit.strip().lower() == "bytes":
                first, _, last = spec.partition(",")[0].partition("-")
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                elif last:
                    start = max(size - int(last), 0)
                    end = size - 1
                if start > end or start >= size:
                    raise ValueError
                end = min(end, size - 1)
                status_code = 206
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        except (ValueError, TypeError):
            raise HTTPException(416, "Invalid range", headers={"Content-Range": f"bytes */{size}"})

    length = (end - start + 1) if end is not None else None
    if length is not None:
        headers["Content-Length"] = str(length)
    headers["Content-Type"] = mime

    if request.method == "HEAD":
        return Response(status_code=status_code, headers=headers)

    async def body():
        try:
            async for chunk in session.iter_media(view, start=start, length=length):
                yield chunk
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Headers are already out; the only honest option is to stop and
            # leave a trace instead of dumping a traceback into the log.
            log.warning("stream %s/%s failed at %s: %r", peer, message_id, start, exc)

    return StreamingResponse(body(), status_code=status_code, headers=headers, media_type=mime)


@app.post("/api/paid/{peer}/{message_id}/check")
async def paid_check(peer: int, message_id: int, request: Request):
    """Re-ask Telegram whether this paid post has been bought.

    ``getMessages`` keeps returning the locked preview after payment, so a post
    bought moments ago still looks locked on a page loaded a minute earlier.
    This forces the ``messages.getExtendedMedia`` lookup and reports the result.
    """
    session = await authorized(request)
    try:
        msg = await session.message(peer, message_id, fresh=True)
    except Exception as exc:
        raise HTTPException(502, f"Could not open message: {exc}")
    if msg is None:
        raise HTTPException(404, "No such message")
    info = tg.paid_info(msg)
    if info is None:
        raise HTTPException(400, "That message is not a paid post")
    unlocked = tg.downloadable(msg) is not None
    return {
        "unlocked": unlocked,
        "stars": info["stars"],
        "media": tg.message_payload(msg).get("media"),
    }


@app.get("/api/thumb/{peer}/{message_id}")
async def thumb(peer: int, message_id: int, request: Request):
    session = await authorized(request)
    try:
        msg = await session.message(peer, message_id)
    except Exception as exc:
        raise HTTPException(502, f"Could not open message: {exc}")
    if msg is None:
        raise HTTPException(404, "No such message")
    msg = await session.resolve_paid(msg)
    data = await tg.cached_thumbnail(session, msg)
    if not data:
        raise HTTPException(404, "No thumbnail")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/avatar/{peer}")
async def avatar(peer: int, request: Request):
    session = await authorized(request)
    try:
        entity = await session.entity(peer)
    except Exception as exc:
        raise HTTPException(502, f"Could not resolve peer: {exc}")
    data = await tg.cached_avatar(session.client, entity)
    if not data:
        raise HTTPException(404, "No avatar")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


# --------------------------------------------------------------------------- #
# Static client
# --------------------------------------------------------------------------- #

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/health")
async def health():
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
