# -*- coding: utf-8 -*-
"""Linux 命令输出解码 / 控制台编码适配。

Linux 下系统命令输出统一为 UTF-8（locale 为 C/POSIX 时可能退回 ASCII），
正常情况直接 decode 即可；这里保留防御性兜底，保证遇到异常字节也不会崩溃。
"""

from __future__ import annotations

import sys

_BOMS = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


def decode_output(raw: bytes | str | None, default: str = "") -> str:
    """把子进程输出的字节安全地解码为文本，中文不乱码。"""
    if raw is None:
        return default
    if isinstance(raw, str):
        return raw
    if not raw:
        return default

    for bom, enc in _BOMS:
        if raw.startswith(bom):
            try:
                return raw.decode(enc)
            except Exception:
                break

    try:
        return raw.decode("utf-8")
    except Exception:
        pass

    # 兜底：按系统 locale 再试一次，仍失败则替换非法字节
    try:
        import locale  # noqa: WPS433

        enc = locale.getpreferredencoding(False) or "utf-8"
        return raw.decode(enc, errors="replace")
    except Exception:
        return raw.decode("utf-8", errors="replace")


def fix_console_encoding() -> None:
    """让 print 输出在各类终端 / 管道下不乱码、不丢缓冲。

    Linux 终端基本是 UTF-8；若 LANG=C 等导致 stdout 编码为 ASCII，
    强制切回 UTF-8，避免中文提示输出报错。
    """
    streams = [s for s in (sys.stdout, sys.stderr) if s is not None]
    for s in streams:
        try:
            enc = (getattr(s, "encoding", "") or "").lower()
            if enc in ("", "utf-8", "utf8"):
                s.reconfigure(errors="replace", line_buffering=True)
            else:
                s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            try:
                s.reconfigure(errors="replace")
            except Exception:
                pass
