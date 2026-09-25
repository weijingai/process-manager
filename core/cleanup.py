# -*- coding: utf-8 -*-
"""一键清理模块：内存清理 / 磁盘空间清理 / 大文件管理。

设计原则（重要）
---------------
本模块会真正删除用户文件，因此采取三层防护：

1. **白名单 + ID 化**：磁盘清理只能通过预定义的 target id 触发，
   绝不接受外部直接传入的目录路径；大文件删除虽接受路径，但会逐一校验。
2. **默认预演**：``clean_disk(dry_run=True)`` 是默认值，只统计不删除。
3. **删除走回收站**：用户勾选的文件默认经 ``SHFileOperationW(FO_DELETE|FOF_ALLOWUNDO)``
   送入回收站，可还原；遇到系统保护路径则直接跳过，不尝试任何写操作。

单项失败不影响整体，所有失败都会计入 failed 列表返回。
"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from typing import Any, Callable, Iterable

import psutil

from .monitor import human_bytes

try:  # Windows 专有，非 Windows 环境下降级为空操作
    from ctypes.wintypes import MAX_PATH
except Exception:  # pragma: no cover
    MAX_PATH = 260


# --------------------------------------------------------------------------- #
# Windows API 常量与结构
# --------------------------------------------------------------------------- #

FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400

SHERB_NOCONFIRMATION = 0x00000001
SHERB_NOPROGRESSUI = 0x00000002
SHERB_NOSOUND = 0x00000004

PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_INFORMATION = 0x0400

# psutil / 系统进程名与 PID，清理时一律跳过
_SKIP_PID_MAX = 4
_SKIP_NAMES = {
    "system", "registry", "memory compression", "smss.exe", "csrss.exe",
    "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe",
    "fontdrvhost.exe", "dwm.exe", "sihost.exe", "ctfmon.exe",
}
# 允许发现大文件的分区；系统保留分区不扫
_SYSTEM_ROOT = os.environ.get("SystemDrive", "C:") or "C:"


class SHFILEOPSTRUCTW(ctypes.Structure):
    """SHFileOperationW 的参数结构（64 位下按默认对齐即可）。"""

    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def _is_reparse(path: str) -> bool:
    """判断是否是符号链接 / 重解析点（删除时可能导致越界删除）。"""
    try:
        return bool(os.lstat(path).st_reparse_tag if hasattr(os, "lstat") else False)
    except Exception:
        return False


def _safe_path(path: str) -> bool:
    """是否为允许删除的普通文件。"""
    try:
        if not path or not os.path.isabs(path):
            return False
        st = os.lstat(path)
        if not os.path.isfile(path):
            return False
        # 跳过符号链接与重解析点，避免顺着链接删到别处
        if hasattr(st, "st_reparse_tag") and st.st_reparse_tag:
            return False
    except Exception:
        return False
    return True


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _is_system_path(path: str) -> bool:
    """系统保护路径，禁止删除（即使出现在扫描结果里）。"""
    p = _norm(path)
    guards = [
        _norm(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"))),
        _norm(os.environ.get("ProgramFiles", r"C:\Program Files")),
        _norm(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        _norm(os.environ.get("ProgramData", r"C:\ProgramData")),
    ]
    return any(p.startswith(g + os.sep) or p == g for g in guards)


# --------------------------------------------------------------------------- #
# 目录遍历工具
# --------------------------------------------------------------------------- #

def _iter_files(root: str, max_depth: int = 6):
    """安全遍历：不跟随符号链接，限制深度，单项错误忽略。"""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return

    def onerror(exc: OSError) -> None:
        return

    try:
        walker = os.walk(root, topdown=True, onerror=onerror, followlinks=False)
    except Exception:
        return

    try:
        for dirpath, dirnames, filenames in walker:
            depth = dirpath[len(root.rstrip(os.sep)):].count(os.sep)
            if depth >= max_depth:
                dirnames[:] = []
            # 过滤掉符号链接目录
            keep = []
            for d in dirnames:
                full = os.path.join(dirpath, d)
                try:
                    st = os.lstat(full)
                    if hasattr(st, "st_reparse_tag") and st.st_reparse_tag:
                        continue
                except Exception:
                    continue
                keep.append(d)
            dirnames[:] = keep
            for f in filenames:
                yield os.path.join(dirpath, f)
    except Exception:
        return


def scan_dir(root: str, max_depth: int = 6) -> dict[str, Any]:
    """统计目录总大小与文件数（不删除）。"""
    total = 0
    count = 0
    for f in _iter_files(root, max_depth=max_depth):
        try:
            st = os.lstat(f)
            if hasattr(st, "st_reparse_tag") and st.st_reparse_tag:
                continue
            total += st.st_size
            count += 1
        except Exception:
            continue
    return {"size": total, "count": count, "size_text": human_bytes(total)}


def purge_dir(root: str, max_depth: int = 6,
              older_than_sec: float = 0.0) -> dict[str, Any]:
    """删除目录下的文件（不清空子目录本身），返回释放量与失败数。"""
    freed = 0
    deleted = 0
    failed = 0
    now = time.time()
    for f in _iter_files(root, max_depth=max_depth):
        try:
            st = os.lstat(f)
            if hasattr(st, "st_reparse_tag") and st.st_reparse_tag:
                continue
            if older_than_sec > 0 and now - st.st_mtime < older_than_sec:
                continue
            os.remove(f)
            freed += st.st_size
            deleted += 1
        except Exception:
            failed += 1
    return {"freed": freed, "deleted": deleted, "failed": failed,
            "freed_text": human_bytes(freed)}


# --------------------------------------------------------------------------- #
# 磁盘清理目标清单（白名单）
# --------------------------------------------------------------------------- #

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


def _glob_profile_dirs(base: str, pattern_parts: Iterable[str]) -> list[str]:
    """展开带通配符的配置目录，例如 Firefox 的 Profiles\\*\\cache2。"""
    path = os.path.join(base, *pattern_parts)
    if "*" not in path:
        return [path] if os.path.isdir(path) else []
    head, _, tail = path.partition("*")
    head = head.rstrip(os.sep) or os.sep
    try:
        entries = os.listdir(head)
    except Exception:
        return []
    out = []
    for e in entries:
        candidate = os.path.join(head, e, tail.lstrip("\\/"))
        if os.path.isdir(candidate):
            out.append(candidate)
    return out


def build_targets() -> list[dict[str, Any]]:
    """磁盘清理项目清单。每项含一组白名单根目录。"""
    win = _env("SystemRoot", r"C:\Windows")
    local = _env("LOCALAPPDATA", "")
    temp = _env("TEMP", "")
    prog_data = _env("ProgramData", "")

    targets: list[dict[str, Any]] = [
        {
            "id": "windows_temp",
            "name": "Windows 临时文件",
            "group": "系统",
            "risk": "safe",
            "desc": "用户临时目录与系统临时目录，程序安装/运行时的中间产物",
            "admin": False,
            "paths": [p for p in (temp, os.path.join(win, "Temp")) if p],
        },
        {
            "id": "update_cache",
            "name": "Windows 更新缓存",
            "group": "系统",
            "risk": "safe",
            "desc": "已下载的安装包缓存，删除后不影响当前系统版本",
            "admin": True,
            "paths": [os.path.join(win, "SoftwareDistribution", "Download")],
        },
        {
            "id": "delivery_opt",
            "name": "传递优化缓存",
            "group": "系统",
            "risk": "safe",
            "desc": "Windows 更新 P2P 分发缓存，删除后下次更新会重新下载",
            "admin": True,
            "paths": [ os.path.join(win, "SoftwareDistribution", "DeliveryOptimization"),
                       r"C:\Windows\ServiceProfiles\NetworkService\AppData\Local"
                       r"\Microsoft\Windows\DeliveryOptimization" ],
        },
        {
            "id": "error_report",
            "name": "Windows 错误报告",
            "group": "系统",
            "risk": "safe",
            "desc": "崩溃转储与错误上报队列（WER）",
            "admin": False,
            "paths": [p for p in (os.path.join(local, "Microsoft", "Windows", "WER"),
                                  os.path.join(prog_data, "Microsoft", "Windows", "WER")) if p],
        },
        {
            "id": "thumbnails",
            "name": "缩略图缓存",
            "group": "系统",
            "risk": "safe",
            "desc": "资源管理器缩略图数据库，删除后会自动重建",
            "admin": False,
            "paths": [os.path.join(local, "Microsoft", "Windows", "Explorer")],
            "pattern": "thumbcache_*.db",
        },
        {
            "id": "inet_cache",
            "name": "系统网络缓存",
            "group": "系统",
            "risk": "safe",
            "desc": "IE / WebView 遗留的网络缓存与 Cookies",
            "admin": False,
            "paths": [os.path.join(local, "Microsoft", "Windows", "INetCache"),
                      os.path.join(local, "Microsoft", "Windows", "INetCookies")],
        },
        {
            "id": "font_cache",
            "name": "字体缓存",
            "group": "系统",
            "risk": "safe",
            "desc": "字体缓存数据，删除后首次启动会略慢",
            "admin": True,
            "paths": [r"C:\Windows\ServiceProfiles\LocalService\AppData\Local\FontCache"],
        },
        {
            "id": "shader_cache",
            "name": "着色器缓存",
            "group": "系统",
            "risk": "safe",
            "desc": "DirectX / D3D 着色器缓存，游戏首次运行会重新编译",
            "admin": False,
            "paths": [p for p in (os.path.join(local, "D3DSCache"),
                                  os.path.join(local, "Microsoft", "DirectX Shader Cache")) if p],
        },
        {
            "id": "browser_edge",
            "name": "Edge / Chrome 缓存",
            "group": "浏览器",
            "risk": "safe",
            "desc": "浏览器缓存文件（不含密码、书签、历史记录）",
            "admin": False,
            "paths": [],
            "paths_multi": [
                os.path.join(local, "Microsoft", "Edge", "User Data", "Default", "Cache"),
                os.path.join(local, "Microsoft", "Edge", "User Data", "Default", "Code Cache"),
                os.path.join(local, "Google", "Chrome", "User Data", "Default", "Cache"),
                os.path.join(local, "Google", "Chrome", "User Data", "Default", "Code Cache"),
            ],
        },
        {
            "id": "browser_firefox",
            "name": "Firefox 缓存",
            "group": "浏览器",
            "risk": "safe",
            "desc": "Firefox 配置目录下的 cache2 缓存",
            "admin": False,
            "paths": [],
            "paths_glob": _glob_profile_dirs(os.path.join(local, "Mozilla", "Firefox"),
                                             ["Profiles", "*", "cache2"]),
        },
        {
            "id": "cbs_log",
            "name": "系统组件日志",
            "group": "系统",
            "risk": "safe",
            "desc": "CBS / DISM 安装日志，仅供排查问题用",
            "admin": True,
            "paths": [os.path.join(win, "Logs", "CBS"),
                      os.path.join(win, "Logs", "DISM")],
        },
        {
            "id": "old_windows",
            "name": "Windows.old 升级残留",
            "group": "高风险",
            "risk": "high",
            "desc": "旧系统文件（升级 Windows 后遗留）。删除后将无法回退到上一版本，请谨慎勾选",
            "admin": True,
            "paths": [os.path.join(_SYSTEM_ROOT, os.sep, "Windows.old")],
        },
    ]

    # 统一补齐 paths_multi 的存在性过滤
    for t in targets:
        paths = [p for p in (t.get("paths") or []) if p and os.path.isdir(p)]
        for p in (t.get("paths_multi") or []):
            if p and os.path.isdir(p):
                paths.append(p)
        for p in (t.get("paths_glob") or []):
            if p and os.path.isdir(p):
                paths.append(p)
        t["paths"] = paths
        t.pop("paths_multi", None)
        t.pop("paths_glob", None)
        t["available"] = bool(paths)
    return targets


#: id -> target 的索引，删除时只认 id，避免接受任意路径
_TARGET_INDEX: dict[str, dict[str, Any]] = {t["id"]: t for t in build_targets()}


def scan_targets(ids: Iterable[str] | None = None,
                 progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """扫描各清理项占用空间。只读操作，绝不删除。"""
    targets = build_targets()
    wanted = set(ids) if ids else None
    rows: list[dict[str, Any]] = []
    total = 0
    for t in targets:
        if wanted and t["id"] not in wanted:
            continue
        size = count = 0
        for p in t["paths"]:
            if progress:
                progress(f"正在统计：{t['name']}")
            if t.get("pattern"):
                import glob as _glob
                try:
                    for f in _glob.glob(os.path.join(p, t["pattern"])):
                        try:
                            size += os.lstat(f).st_size
                            count += 1
                        except Exception:
                            continue
                except Exception:
                    continue
            else:
                info = scan_dir(p)
                size += info["size"]
                count += info["count"]
        total += size
        rows.append({
            "id": t["id"],
            "name": t["name"],
            "group": t["group"],
            "risk": t["risk"],
            "desc": t["desc"],
            "admin": t["admin"],
            "available": t["available"],
            "paths": t["paths"],
            "size": size,
            "count": count,
            "size_text": human_bytes(size),
        })

    rows.sort(key=lambda r: r["size"], reverse=True)
    return {"rows": rows, "total": total, "total_text": human_bytes(total),
            "admin": _is_admin()}


# --------------------------------------------------------------------------- #
# 磁盘清理执行
# --------------------------------------------------------------------------- #

def clean_disk(ids: Iterable[str], dry_run: bool = True,
               progress: Callable[[str], None] | None = None
               ) -> tuple[bool, dict[str, Any]]:
    """按 id 清理。dry_run=True 时只返回预演结果。"""
    import glob as _glob

    picked: list[dict[str, Any]] = []
    unknown: list[str] = []
    for i in ids:
        t = _TARGET_INDEX.get(str(i))
        if not t or not t["paths"]:
            unknown.append(str(i))
            continue
        picked.append(t)

    if not picked:
        return False, {"message": "没有可清理的项目（可能路径不存在或 id 无效）",
                       "unknown": unknown, "freed": 0, "deleted": 0, "failed": 0,
                       "freed_text": "0 B", "items": [], "dry_run": dry_run}

    items: list[dict[str, Any]] = []
    freed = deleted = failed = 0
    for t in picked:
        if progress:
            progress(f"{'预演' if dry_run else '清理'}：{t['name']}")
        item = {"id": t["id"], "name": t["name"], "risk": t["risk"],
                "freed": 0, "deleted": 0, "failed": 0}
        for root in t["paths"]:
            try:
                if t.get("pattern"):
                    matches = _glob.glob(os.path.join(root, t["pattern"]))
                    for f in matches:
                        try:
                            sz = os.lstat(f).st_size
                            if not dry_run:
                                os.remove(f)
                            item["freed"] += sz
                            item["deleted"] += 1
                        except Exception:
                            item["failed"] += 1
                else:
                    r = (scan_dir(root) if dry_run else purge_dir(root))
                    item["freed"] += r["size"] if dry_run else r["freed"]
                    item["deleted"] += r["count"] if dry_run else r["deleted"]
                    item["failed"] += r.get("failed", 0)
            except Exception:
                item["failed"] += 1
        item["freed_text"] = human_bytes(item["freed"])
        freed += item["freed"]
        deleted += item["deleted"]
        failed += item["failed"]
        items.append(item)

    action = "预计可释放" if dry_run else "已释放"
    return True, {
        "message": f"{action} {human_bytes(freed)}，文件 {deleted} 个"
                   + (f"，失败 {failed} 个" if failed else ""),
        "freed": freed, "deleted": deleted, "failed": failed,
        "freed_text": human_bytes(freed),
        "items": items, "unknown": unknown, "dry_run": dry_run,
    }


# --------------------------------------------------------------------------- #
# 大文件扫描与删除
# --------------------------------------------------------------------------- #

_HIGH_RISK_EXT = {
    ".sys", ".dll", ".exe", ".msi", ".mui", ".manifest", ".cat", ".inf",
    ".pnf", ".drv", ".efi", ".wim", ".esd",
}
_MEDIA_EXT = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".rmvb", ".mp3",
              ".wav", ".flac", ".m4a"}
_PACKAGE_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".iso", ".cab"}
_DISK_EXT = {".vhd", ".vhdx", ".vdi", ".iso", ".wim"}
_CACHE_EXT = {".log", ".tmp", ".temp", ".bak", ".old", ".cache", ".dmp", ".etl"}


def classify_file(path: str, size: int) -> dict[str, str]:
    """给文件打风险标签，帮助用户判断能不能删。"""
    ext = os.path.splitext(path)[1].lower()
    p = _norm(path)

    if _is_system_path(path):
        return {"risk": "high", "category": "系统文件",
                "advice": "位于系统保护目录，禁止删除"}
    if ext in _HIGH_RISK_EXT:
        return {"risk": "high", "category": "程序/系统组件",
                "advice": "删除可能导致程序无法运行"}
    if os.path.join("Windows", "") in p or "\\$Recycle.Bin\\" in p:
        return {"risk": "high", "category": "系统路径",
                "advice": "Windows 目录内文件，不建议删除"}
    if ext in _DISK_EXT:
        return {"risk": "high", "category": "磁盘镜像",
                "advice": "可能是系统备份/安装镜像，确认无用再删"}
    if ext in _MEDIA_EXT:
        return {"risk": "medium", "category": "影音文件", "advice": "个人媒体，需自行确认"}
    if ext in _PACKAGE_EXT:
        return {"risk": "medium", "category": "压缩包", "advice": "可能是安装包备份"}
    if ext in _CACHE_EXT or "\\Temp" in p or "\\temp\\" in p:
        return {"risk": "low", "category": "缓存/日志", "advice": "通常是可安全清理的临时数据"}
    return {"risk": "medium", "category": "其他文件", "advice": "请自行确认是否仍需要"}


def list_drives() -> list[dict[str, Any]]:
    rows = []
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in (part.opts or "").lower():
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
        except Exception:
            continue
        rows.append({"device": part.device, "mountpoint": part.mountpoint,
                     "total": u.total, "used": u.used, "free": u.free,
                     "percent": round(u.percent, 1),
                     "free_text": human_bytes(u.free),
                     "total_text": human_bytes(u.total)})
    rows.sort(key=lambda r: r["mountpoint"])
    return rows


_SKIP_DIR_NAMES = {
    "$Recycle.Bin", "System Volume Information", "Recovery", "Windows",
    "Program Files", "Program Files (x86)", "ProgramData", "PerfLogs",
    ".git", "node_modules", "AppData", "WinSxS", "EFI", "Boot",
}


def scan_large_files(drive: str | None = None, min_mb: float = 100,
                     limit: int = 300, max_seconds: float = 25.0,
                     progress: Callable[[str, int], None] | None = None
                     ) -> dict[str, Any]:
    """扫描大文件。只读操作。

    为了不在全盘扫描上耗死，设置了时间上限；跳过系统目录以缩小范围并提高安全性。
    """
    roots: list[str] = []
    for row in list_drives():
        mp = row["mountpoint"]
        if drive and not mp.upper().startswith(drive.upper()):
            continue
        roots.append(mp)

    threshold = min_mb * 1024 * 1024
    found: list[dict[str, Any]] = []
    started = time.time()
    scanned = 0
    timed_out = False

    for root in roots:
        if time.time() - started > max_seconds:
            timed_out = True
            break
        root_abs = os.path.abspath(root)
        base_depth = root_abs.rstrip(os.sep).count(os.sep)
        try:
            walker = os.walk(root_abs, topdown=True,
                             onerror=lambda e: None, followlinks=False)
        except Exception:
            continue
        try:
            for dirpath, dirnames, filenames in walker:
                if time.time() - started > max_seconds:
                    timed_out = True
                    break
                # 剔除无用目录，speed + 安全双收益；同时剔除 Junction /
                # 硬链接类重解析点（如 Documents and Settings → Users），
                # 否则同一份用户文件会被重复扫一遍
                keep = []
                for d in dirnames:
                    if d in _SKIP_DIR_NAMES:
                        continue
                    try:
                        st = os.lstat(os.path.join(dirpath, d))
                        if hasattr(st, "st_reparse_tag") and st.st_reparse_tag:
                            continue
                    except Exception:
                        continue
                    keep.append(d)
                dirnames[:] = keep
                if progress and scanned % 400 == 0:
                    progress(dirpath, len(found))
                for f in filenames:
                    scanned += 1
                    full = os.path.join(dirpath, f)
                    try:
                        st = os.lstat(full)
                        if st.st_size < threshold:
                            continue
                        info = classify_file(full, st.st_size)
                        found.append({
                            "path": full,
                            "name": f,
                            "size": st.st_size,
                            "size_text": human_bytes(st.st_size),
                            "mtime": time.strftime("%Y-%m-%d %H:%M",
                                                   time.localtime(st.st_mtime)),
                            "age_days": int((time.time() - st.st_mtime) / 86400),
                            **info,
                        })
                    except Exception:
                        continue
        except Exception:
            continue

    found.sort(key=lambda r: r["size"], reverse=True)
    top = found[:limit]
    return {
        "rows": top,
        "total_found": len(found),
        "shown": len(top),
        "scanned_files": scanned,
        "elapsed": round(time.time() - started, 1),
        "timed_out": timed_out,
        "drives": roots,
        "min_mb": min_mb,
    }


def delete_files(paths: Iterable[str], use_recycle: bool = True
                 ) -> tuple[bool, dict[str, Any]]:
    """删除指定文件。默认送入回收站；逐一校验，不合规则直接跳过。"""
    rows: list[dict[str, Any]] = []
    freed = deleted = skipped = failed = 0

    for raw in paths:
        path = os.path.abspath(str(raw))
        if not _safe_path(path) or _is_system_path(path):
            skipped += 1
            rows.append({"path": path, "ok": False, "reason": "非法的或受保护的路径"})
            continue
        try:
            size = os.lstat(path).st_size
        except Exception:
            skipped += 1
            rows.append({"path": path, "ok": False, "reason": "无法读取文件"})
            continue

        ok = _send_to_recycle_bin(path) if use_recycle else True
        if use_recycle and not ok:
            # 回收站失败时退化为永久删除会不会有风险？这里保守地标记为失败
            failed += 1
            rows.append({"path": path, "ok": False, "reason": "送入回收站失败"})
            continue

        if not use_recycle:
            try:
                os.remove(path)
            except Exception as exc:
                failed += 1
                rows.append({"path": path, "ok": False, "reason": str(exc)})
                continue

        freed += size
        deleted += 1
        rows.append({"path": path, "ok": True, "size": size,
                     "size_text": human_bytes(size)})

    return True, {
        "message": f"已删除 {deleted} 个文件，释放 {human_bytes(freed)}"
                   + (f"，跳过 {skipped} 个，失败 {failed} 个" if (skipped or failed) else ""),
        "freed": freed, "deleted": deleted, "skipped": skipped, "failed": failed,
        "freed_text": human_bytes(freed), "recycled": use_recycle, "items": rows,
    }


def _send_to_recycle_bin(path: str) -> bool:
    """用 SHFileOperationW 送入回收站（可还原）。"""
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        op = SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = FO_DELETE
        # pFrom 需要双空字符结尾；单文件也要补两个 \0
        op.pFrom = path + "\0\0"
        op.pTo = None
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
        res = shell32.SHFileOperationW(ctypes.byref(op))
        return res == 0
    except Exception:
        return False


def empty_recycle_bin() -> tuple[bool, str]:
    """清空回收站。"""
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        flags = SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
        rc = shell32.SHEmptyRecycleBinW(None, None, flags)
        return (True, "回收站已清空") if rc == 0 else (False, f"清空失败，返回码 {rc}")
    except Exception as exc:
        return False, f"清空回收站出错：{exc}"


# --------------------------------------------------------------------------- #
# 内存清理
# --------------------------------------------------------------------------- #

def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _enable_privilege(name: str) -> bool:
    """提升当前进程令牌权限，供文件缓存/备用列表清理使用。"""
    try:
        import win32security  # type: ignore # noqa: F401
    except Exception:
        pass
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

        class TOKEN_PRIVILEGES(ctypes.Structure):
            _fields_ = [("PrivilegeCount", wintypes.DWORD),
                        ("Luid", LUID),
                        ("Attributes", wintypes.DWORD)]

        token = wintypes.HANDLE()
        TOKEN_ADJUST_PRIVILEGES = 0x0020
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                         TOKEN_ADJUST_PRIVILEGES,
                                         ctypes.byref(token)):
            return False
        try:
            luid = LUID()
            if not advapi32.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
                return False
            tp = TOKEN_PRIVILEGES()
            tp.PrivilegeCount = 1
            tp.Luid = luid
            tp.Attributes = 0x00000002  # SE_PRIVILEGE_ENABLED
            return bool(advapi32.AdjustTokenPrivileges(
                token, False, ctypes.byref(tp),
                ctypes.sizeof(tp), None, None))
        finally:
            kernel32.CloseHandle(token)
    except Exception:
        return False


def _trim_working_sets(include_self: bool = False) -> dict[str, Any]:
    """调用 EmptyWorkingSet 裁剪各进程工作集 —— 「清理内存」的主体动作。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    OpenProcess = kernel32.OpenProcess
    OpenProcess.restype = wintypes.HANDLE
    desired = PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION

    trimmed = skipped = failed = 0
    own = os.getpid()
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            pid = proc.info["pid"]
            name = (proc.info.get("name") or "").lower()
        except Exception:
            continue
        if pid <= _SKIP_PID_MAX or name in _SKIP_NAMES:
            skipped += 1
            continue
        if pid == own and not include_self:
            skipped += 1
            continue
        handle = OpenProcess(desired, False, pid)
        if not handle:
            failed += 1
            continue
        try:
            if psapi.EmptyWorkingSet(handle):
                trimmed += 1
            else:
                failed += 1
        except Exception:
            failed += 1
        finally:
            kernel32.CloseHandle(handle)
    return {"trimmed": trimmed, "skipped": skipped, "failed": failed}


def _clear_system_file_cache() -> tuple[bool, str]:
    """清空系统文件缓存（Standby 的一部分），需要 SeIncreaseQuotaPrivilege。"""
    try:
        _enable_privilege("SeIncreaseQuotaPrivilege")
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

        class SYSTEM_CACHE_INFORMATION(ctypes.Structure):
            _fields_ = [("MinimumWorkingSet", ctypes.c_size_t),
                        ("MaximumWorkingSet", ctypes.c_size_t),
                        ("Flags", wintypes.ULONG)]

        info = SYSTEM_CACHE_INFORMATION()
        info.MinimumWorkingSet = ctypes.c_size_t(-1)
        info.MaximumWorkingSet = ctypes.c_size_t(-1)
        info.Flags = 0
        SystemFileCacheInformation = 21
        status = ntdll.NtSetSystemInformation(
            SystemFileCacheInformation, ctypes.byref(info), ctypes.sizeof(info))
        if status == 0:
            return True, "系统文件缓存已清理"
        return False, f"NtSetSystemInformation 返回 0x{status & 0xFFFFFFFF:X}"
    except Exception as exc:
        return False, f"{exc}"


def _purge_standby_list() -> tuple[bool, str]:
    """清理备用内存列表（Standby List），需要 SeProfileSingleProcessPrivilege。"""
    try:
        _enable_privilege("SeProfileSingleProcessPrivilege")
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        SystemMemoryListInformation = 80
        MemoryPurgeStandbyList = 4
        cmd = ctypes.c_int(MemoryPurgeStandbyList)
        status = ntdll.NtSetSystemInformation(
            SystemMemoryListInformation, ctypes.byref(cmd), ctypes.sizeof(cmd))
        if status == 0:
            return True, "备用内存列表已清理"
        return False, f"NtSetSystemInformation 返回 0x{status & 0xFFFFFFFF:X}"
    except Exception as exc:
        return False, f"{exc}"


def clean_memory(trim_working_set: bool = True,
                 clear_file_cache: bool = False,
                 purge_standby: bool = False) -> dict[str, Any]:
    """一键内存清理。

    注意：``EmptyWorkingSet`` 只是把进程中暂时不用的物理页换出到备用列表，
    并不会让软件「变快」，但能显著降低任务管理器里看到的已用内存。
    """
    vm_before = psutil.virtual_memory()
    before_pct = round(vm_before.percent, 1)

    detail: dict[str, Any] = {}
    if trim_working_set:
        detail["trim"] = _trim_working_sets()
    if clear_file_cache:
        ok, msg = _clear_system_file_cache()
        detail["file_cache"] = {"ok": ok, "message": msg}
    if purge_standby:
        ok, msg = _purge_standby_list()
        detail["standby"] = {"ok": ok, "message": msg}

    time.sleep(1.2)  # 等待内核完成页回收后再读一次
    vm_after = psutil.virtual_memory()
    after_pct = round(vm_after.percent, 1)

    freed = int(vm_before.used - vm_after.used)
    return {
        "before_percent": before_pct,
        "after_percent": after_pct,
        "before_used": vm_before.used,
        "after_used": vm_after.used,
        "freed": max(freed, 0),
        "freed_text": human_bytes(max(freed, 0)),
        "before_text": human_bytes(vm_before.used),
        "after_text": human_bytes(vm_after.used),
        "total_text": human_bytes(vm_before.total),
        "detail": detail,
        "admin": _is_admin(),
        "tip": "工作集裁剪只把闲置物理页换到备用列表，不会关闭程序，也不会丢失数据",
    }


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def self_check() -> dict[str, Any]:
    targets = scan_targets()
    vm = psutil.virtual_memory()
    return {
        "targets": len(targets["rows"]),
        "cleanable_text": targets["total_text"],
        "top_targets": [(r["name"], r["size_text"]) for r in targets["rows"][:5]],
        "memory_percent": round(vm.percent, 1),
        "drives": [(d["mountpoint"], d["free_text"]) for d in list_drives()],
        "recycle_api": _recycle_api_ok(),
    }


def _recycle_api_ok() -> bool:
    """检查 SHFileOperationW 结构是否可用（避免 ctypes 结构对齐错误）。"""
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        return hasattr(shell32, "SHFileOperationW") and ctypes.sizeof(SHFILEOPSTRUCTW) > 0
    except Exception:
        return False


if __name__ == "__main__":
    import json

    from core import textutil  # noqa: E402

    textutil.fix_console_encoding()
    print(json.dumps(self_check(), ensure_ascii=False, indent=2))
