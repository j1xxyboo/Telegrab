"""Android entry point: runs the TeleGrab FastAPI server inside the app process.

Kotlin calls :func:`start` once (from a foreground service). The server binds to
127.0.0.1 only and the WebView talks to it, so the web UI is the exact one used
on a PC.

Telethon encrypts every MTProto packet with AES-IGE. Without ``cryptg`` or a
loadable libssl it falls back to a pure-Python loop that manages roughly
100 KB/s — useless for video. ``cryptg`` has no Android build, so this module
installs a replacement backed by OpenSSL (via ctypes) or PyCryptodome, and
checks it against Telethon's own reference implementation before using it.
"""
from __future__ import annotations

import logging
import os
import sys
import threading

log = logging.getLogger("tegrab.android")

_lock = threading.Lock()
_started = False
_aes_backend = "pure-python"


# --------------------------------------------------------------------------- #
# Fast AES-IGE
# --------------------------------------------------------------------------- #

def _ige_pycryptodome():
    """AES-IGE on top of PyCryptodome's ECB primitive (block-at-a-time)."""
    from Crypto.Cipher import AES as _AES

    from_bytes = int.from_bytes

    def decrypt_ige(cipher_text, key, iv):
        ecb = _AES.new(bytes(key), _AES.MODE_ECB)
        decrypt = ecb.decrypt
        iv1 = from_bytes(iv[:16], "big")      # previous ciphertext block
        iv2 = from_bytes(iv[16:32], "big")    # previous plaintext block
        out = bytearray(len(cipher_text))
        for i in range(0, len(cipher_text), 16):
            c = from_bytes(cipher_text[i:i + 16], "big")
            p = from_bytes(decrypt((c ^ iv2).to_bytes(16, "big")), "big") ^ iv1
            out[i:i + 16] = p.to_bytes(16, "big")
            iv1, iv2 = c, p
        return bytes(out)

    def encrypt_ige(plain_text, key, iv):
        ecb = _AES.new(bytes(key), _AES.MODE_ECB)
        encrypt = ecb.encrypt
        iv1 = from_bytes(iv[:16], "big")      # previous ciphertext block
        iv2 = from_bytes(iv[16:32], "big")    # previous plaintext block
        out = bytearray(len(plain_text))
        for i in range(0, len(plain_text), 16):
            p = from_bytes(plain_text[i:i + 16], "big")
            c = from_bytes(encrypt((p ^ iv1).to_bytes(16, "big")), "big") ^ iv2
            out[i:i + 16] = c.to_bytes(16, "big")
            iv1, iv2 = c, p
        return bytes(out)

    return encrypt_ige, decrypt_ige


def _ige_libcrypto():
    """AES-IGE straight from an OpenSSL libcrypto (``AES_ige_encrypt``)."""
    import ctypes

    lib = None
    for name in ("libcrypto_chaquopy.so", "libcrypto_3_chaquopy.so", "libcrypto_1_1_chaquopy.so",
                 "libcrypto.so.3", "libcrypto.so.1.1", "libcrypto.so"):
        try:
            lib = ctypes.CDLL(name)
            lib.AES_ige_encrypt  # noqa: B018 - symbol must exist
            break
        except (OSError, AttributeError):
            lib = None
    if lib is None:
        raise OSError("no usable libcrypto")

    class AesKey(ctypes.Structure):
        _fields_ = [("rd_key", ctypes.c_uint32 * 60), ("rounds", ctypes.c_uint)]

    lib.AES_ige_encrypt.argtypes = [
        ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.POINTER(AesKey), ctypes.c_void_p, ctypes.c_int,
    ]
    lib.AES_ige_encrypt.restype = None
    lib.AES_set_encrypt_key.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(AesKey)]
    lib.AES_set_decrypt_key.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(AesKey)]

    def _run(data, key, iv, encrypt):
        aes_key = AesKey()
        setter = lib.AES_set_encrypt_key if encrypt else lib.AES_set_decrypt_key
        if setter(bytes(key), len(key) * 8, ctypes.byref(aes_key)) != 0:
            raise ValueError("bad AES key")
        out = ctypes.create_string_buffer(len(data))
        # OpenSSL rewrites the IV while it works. ``bytes(iv)`` would hand back
        # the caller's own object for a bytes input and corrupt it, so the IV
        # goes into a private, mutable buffer.
        iv_buf = ctypes.create_string_buffer(bytes(iv), len(iv))
        lib.AES_ige_encrypt(
            bytes(data), out, len(data), ctypes.byref(aes_key),
            ctypes.addressof(iv_buf), 1 if encrypt else 0,
        )
        return out.raw

    return (lambda d, k, i: _run(d, k, i, True)), (lambda d, k, i: _run(d, k, i, False))


def _reference_ige():
    """Telethon's own pure-Python implementation, used as the oracle."""
    import pyaes

    def decrypt(cipher_text, key, iv):
        iv1, iv2 = iv[:16], iv[16:]
        aes = pyaes.AES(key)
        out = []
        block = [0] * 16
        for index in range(len(cipher_text) // 16):
            for i in range(16):
                block[i] = cipher_text[index * 16 + i] ^ iv2[i]
            plain = aes.decrypt(block)
            for i in range(16):
                plain[i] ^= iv1[i]
            iv1 = cipher_text[index * 16:index * 16 + 16]
            iv2 = plain
            out.extend(plain)
        return bytes(out)

    return decrypt


def _verified(encrypt, decrypt) -> bool:
    """Round-trip random data and compare decryption with the reference."""
    try:
        reference = _reference_ige()
        for size in (16, 64, 4096):
            key, iv, data = os.urandom(32), os.urandom(32), os.urandom(size)
            sealed = encrypt(data, key, iv)
            if len(sealed) != size or sealed == data:
                return False
            if decrypt(sealed, key, iv) != data:
                return False
            if reference(sealed, key, iv) != data:
                return False
        return True
    except Exception as exc:  # noqa: BLE001 - any failure means "do not use it"
        log.debug("AES self-test failed: %r", exc)
        return False


def install_fast_aes() -> str:
    """Swap Telethon's AES-IGE for a native-backed one. Returns the backend name."""
    global _aes_backend
    from telethon.crypto import aes as tl_aes

    for name, factory in (("libcrypto", _ige_libcrypto), ("pycryptodome", _ige_pycryptodome)):
        try:
            encrypt, decrypt = factory()
        except Exception as exc:  # noqa: BLE001
            log.info("AES backend %s unavailable: %r", name, exc)
            continue
        if not _verified(encrypt, decrypt):
            log.warning("AES backend %s failed its self-test; skipping", name)
            continue

        def encrypt_ige(plain_text, key, iv, _encrypt=encrypt):
            padding = len(plain_text) % 16
            if padding:
                plain_text += os.urandom(16 - padding)
            return _encrypt(plain_text, key, iv)

        tl_aes.AES.encrypt_ige = staticmethod(encrypt_ige)
        tl_aes.AES.decrypt_ige = staticmethod(decrypt)
        _aes_backend = name
        log.info("Telethon AES-IGE backend: %s", name)
        return name

    log.warning("no fast AES backend; downloads will be slow")
    return _aes_backend


# --------------------------------------------------------------------------- #
# Server lifecycle
# --------------------------------------------------------------------------- #

def set_credentials(api_id, api_hash) -> None:
    """Update the Telegram API credentials without restarting the server."""
    from app import config

    config.API_ID = int(api_id or 0)
    config.API_HASH = (api_hash or "").strip()


def aes_backend() -> str:
    return _aes_backend


def start(port, sessions_dir, cache_dir, log_file, api_id, api_hash) -> None:
    """Start the web server on a background thread (idempotent)."""
    global _started
    with _lock:
        if _started:
            set_credentials(api_id, api_hash)
            return

        logging.basicConfig(
            level=logging.INFO, stream=sys.stderr,
            format="%(levelname)s %(name)s: %(message)s",
        )
        os.environ.update({
            "TELE_HOST": "127.0.0.1",
            "TELE_PORT": str(int(port)),
            "TELE_SESSIONS_DIR": str(sessions_dir),
            "TELE_CACHE_DIR": str(cache_dir),
            "TELE_LOG_FILE": str(log_file),
            "TELEGRAM_API_ID": str(int(api_id or 0)),
            "TELEGRAM_API_HASH": str(api_hash or ""),
        })
        install_fast_aes()

        import asyncio

        import uvicorn

        from app import main

        server = uvicorn.Server(uvicorn.Config(
            main.app, host="127.0.0.1", port=int(port), log_config=None, access_log=False,
            loop="asyncio", http="h11", ws="none", lifespan="on",
        ))

        def run() -> None:
            try:
                asyncio.run(server.serve())
            except BaseException:  # noqa: BLE001
                log.exception("server crashed")

        threading.Thread(target=run, name="tegrab-server", daemon=True).start()
        _started = True
        log.info("TeleGrab server starting on 127.0.0.1:%s", port)
