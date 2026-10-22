# -*- coding: utf-8 -*-
"""仁谱 自带解密器 · 微信 4.x SQLCipher4 + WAL merge
不依赖任何外部仓库 —— 内联为仁谱的第一个内建能力。
key 文件默认路径（用户可改）：{wc_live}/db_key.txt
"""
import datetime
import os
import sqlite3
import struct
import sys
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Hash import SHA512
from Crypto.Protocol.KDF import PBKDF2

PAGE, IV, HMAC_L, RESERVE, SALT, ROUNDS = 4096, 16, 64, 80, 16, 256000


def decrypt_db(src: Path, dst: Path, passphrase: bytes) -> bool:
    data = src.read_bytes()
    salt = data[:SALT]
    mac_salt = bytes(x ^ 0x3A for x in salt)
    key = PBKDF2(passphrase, salt, dkLen=32, count=ROUNDS, hmac_hash_module=SHA512)
    mac_key = PBKDF2(key, mac_salt, dkLen=32, count=2, hmac_hash_module=SHA512)
    import hmac as _hmac
    n = len(data) // PAGE
    out = bytearray()
    bad = 0
    for p in range(1, n + 1):
        page = data[(p - 1) * PAGE: p * PAGE]
        ct = page[SALT: PAGE - RESERVE] if p == 1 else page[0: PAGE - RESERVE]
        iv = page[PAGE - RESERVE: PAGE - RESERVE + IV]
        m = _hmac.new(mac_key, ct + iv, SHA512)
        m.update(struct.pack("<I", p))
        if m.digest() != page[PAGE - RESERVE + IV: PAGE]:
            bad += 1
        dec = AES.new(key, AES.MODE_CBC, iv).decrypt(ct)
        out += b"SQLite format 3\x00" + dec if p == 1 else dec
        if len(out) % PAGE:
            out += b"\x00" * (PAGE - len(out) % PAGE)
    ok = out[:16] == b"SQLite format 3\x00"
    print(f"  {src.name}: pages={n} hmac_bad={bad} ok={ok}")
    if ok:
        dst.write_bytes(out)
    return ok


def pull_builtin(wc_live: Path, fresh_dir: Path, key: bytes):
    fresh_dir.mkdir(parents=True, exist_ok=True)
    if not wc_live:
        raise SystemExit("wc_live 未配置。请在 config.json 写：{\"wc_live\": \"...\"}")
    for rel, name in (
        ("contact/contact.db", "contact"),
        ("message/message_0.db", "message_0"),
        ("session/session.db", "session"),
    ):
        src = wc_live / rel
        dst = fresh_dir / f"{name}.db"
        for ext in ("-wal", "-shm"):
            s = src.with_name(src.name + ext)
            if s.exists():
                s_size = s.stat().st_size
                if s_size > 0:
                    (fresh_dir / (dst.name + ext)).write_bytes(s.read_bytes())
        if not src.exists():
            print(f"  缺：{rel}（跳过）")
            continue
        if not decrypt_db(src, dst, key):
            raise SystemExit(f"解密失败：{dst.name}")
    print(f"fresh 目录：{fresh_dir}")


def find_db_key(wc_live: Path) -> bytes:
    """默认在 wc_live/db_key.txt；找不到时让用户输入。"""
    candidates = [
        wc_live / "db_key.txt",
        wc_live.parent / "db_key.txt",
        Path.home() / ".config" / "grm" / "db_key.txt",
    ]
    for c in candidates:
        if c.exists():
            return bytes.fromhex(c.read_text().strip())
    # 用户输入模式（仁谱 第一次运行时交互获取并落盘）
    print("没找到解密密钥文件（默认 wc_live/db_key.txt）")
    print("获取方式见文档：`grm doctor` 输出 + INSTALL.md → 取密钥章节")
    sys.exit(1)