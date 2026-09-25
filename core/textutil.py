# -*- coding: utf-8 -*-
"""Windows 命令输出解码 / 控制台编码适配。

乱码根因：中文 Windows 下 sc.exe、netstat、PowerShell 的输出默认是 GBK(cp936)，
而部分环境（PowerShell 脚本里显式设置过 OutputEncoding）又是 UTF-8。
如果固定用 utf-8 且 errors="replace" 解码，GBK 中文会被替换成 U+FFFD 变成乱码。

这里统一按 BOM → UTF-8 → 系统 ANSI(mbcs) → GBK → 末位替换 的顺序解码，
保证两种编码下的中文都能正确还原。
"""

from __future__ import annotations

import ctypes
import sys

#: 解码尝试顺序（严格模式，成功即返回）
#: 不放 latin-1 —— 它对任何字节都能"解码"成功，会把中文变成 Ã¿Â»Ã¿ 这类乱码
_CANDIDATES = ("utf-8", "mbcs", "gbk", "cp936")

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

    # 1. 带 BOM 时以 BOM 为准（PowerShell 重定向输出常见）
    for bom, enc in _BOMS:
        if raw.startswith(bom):
            try:
                return raw.decode(enc)
            except Exception:
                break

    # 2. 严格逐个尝试，成功即返回（utf-8 能解说明确实是 UTF-8）
    for enc in _CANDIDATES:
        try:
            return raw.decode(enc)
        except Exception:
            continue

    # 3. 兜底：按系统 ANSI 代码页解码，无法识别的字节替换掉
    try:
        return raw.decode("mbcs", errors="replace")
    except Exception:
        return raw.decode("utf-8", errors="replace")


def _console_codepage() -> int:
    """返回控制台输出代码页（936 = GBK，65001 = UTF-8）；无控制台返回 0。"""
    if os_name() != "nt":
        return 0
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        return int(k32.GetConsoleOutputCP())
    except Exception:
        return 0


def _ansi_codepage() -> int:
    """系统 ANSI 代码页（中文 Windows 为 936）。"""
    if os_name() != "nt":
        return 0
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        return int(k32.GetACP())
    except Exception:
        return 936


def os_name() -> str:
    import os
    return os.name


def fix_console_encoding() -> None:
    """让 print 输出在中文 Windows 控制台（GBK）下也不乱码。

    - 有控制台：按控制台自身代码页输出（cp936 / cp65001），保证 cmd 里显示正确
    - 无控制台（重定向、管道、GUI 模式）：保持 UTF-8
    同时打开行缓冲，避免输出被重定向到文件时因缓冲丢失。
    """
    streams = [s for s in (sys.stdout, sys.stderr) if s is not None]
    encoding = "utf-8"

    if os_name() == "nt":
        # Windows 下无论输出目标是控制台、PowerShell 管道还是重定向文件，
        # 都按控制台 / 系统 ANSI 代码页输出：cmd 与 PowerShell 都按该代码页解码，
        # 这样中文在任何一种调用方式下都不会乱码。
        # （若直接用 UTF-8，输出被 PowerShell 管道接收时会被按 GBK 解码而变成乱码）
        cp = _console_codepage() or _ansi_codepage()
        if cp and cp != 65001:
            try:
                "测试中文".encode(f"cp{cp}")
                encoding = f"cp{cp}"
            except Exception:
                encoding = "utf-8"

    for s in streams:
        try:
            s.reconfigure(encoding=encoding, errors="replace", line_buffering=True)
        except Exception:
            try:
                s.reconfigure(errors="replace")
            except Exception:
                pass
