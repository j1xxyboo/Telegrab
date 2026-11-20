"""Checks the Android AES-IGE replacement against Telethon's reference code.

Run: python3 tests/test_android_crypto.py   (needs pycryptodome; skips otherwise)
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHIM = ROOT / "android" / "app" / "src" / "main" / "python" / "tegrab_android.py"

if importlib.util.find_spec("Crypto") is None:
    print("skip  pycryptodome not installed")
    sys.exit(0)

spec = importlib.util.spec_from_file_location("tegrab_android", SHIM)
shim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shim)

encrypt, decrypt = shim._ige_pycryptodome()
reference = shim._reference_ige()

for size in (16, 32, 160, 4096, 65536):
    key, iv, data = os.urandom(32), os.urandom(32), os.urandom(size)
    sealed = encrypt(data, key, iv)
    assert decrypt(sealed, key, iv) == data, size
    assert reference(sealed, key, iv) == data, size
print("ok  pycryptodome IGE matches the pure-Python reference")

assert shim._verified(encrypt, decrypt)
assert not shim._verified(lambda d, k, i: d, lambda d, k, i: d), "identity cipher must be rejected"
assert not shim._verified(encrypt, lambda d, k, i: d), "broken decrypt must be rejected"
print("ok  self-test accepts a correct cipher and rejects broken ones")

# Telethon is optional here, but when present the patched class must still agree
# with the stock implementation on padding behaviour.
try:
    from telethon.crypto import aes as tl_aes
except ImportError:
    tl_aes = None
if tl_aes is not None:
    stock_encrypt, stock_decrypt = tl_aes.AES.encrypt_ige, tl_aes.AES.decrypt_ige
    backend = shim.install_fast_aes()
    key, iv, data = os.urandom(32), os.urandom(32), os.urandom(100)  # not a multiple of 16
    sealed = tl_aes.AES.encrypt_ige(data, key, iv)
    assert len(sealed) == 112, len(sealed)
    assert tl_aes.AES.decrypt_ige(sealed, key, iv)[:100] == data
    assert stock_decrypt(sealed, key, iv)[:100] == data, "stock Telethon must read what the shim wrote"
    print(f"ok  installed backend={backend}; padding and interop match stock Telethon")
    tl_aes.AES.encrypt_ige, tl_aes.AES.decrypt_ige = stock_encrypt, stock_decrypt

blob, key, iv = os.urandom(8 * 1024 * 1024), os.urandom(32), os.urandom(32)
sealed = encrypt(blob, key, iv)
started = time.perf_counter()
decrypt(sealed, key, iv)
rate = len(blob) / (time.perf_counter() - started) / 1e6
print(f"ok  decrypt throughput on this machine: {rate:.1f} MB/s")
print("\nall good")
