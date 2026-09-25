# -*- coding: utf-8 -*-
"""一键清理模块（Linux）：内存清理 / 磁盘空间清理 / 大文件管理。

设计原则（与 Windows 版一致，三层防护）：

1. **白名单 + ID 化**：磁盘清理只能通过预定义的 target id 触发，
   绝不接受外部直接传入的目录路径；大文件删除虽接受路径，但会逐一校验。
2. **默认预演**：``clean_disk(dry_run=True)`` 是默认值，只统计不删除。
3. **删除走回收站**：用户勾选的文件默认经 ``gio trash`` 送入 freedesktop
   回收站（~/.local/share/Trash），可还原；gio 不可用时手动移入回收站目录。

单项失败不影响整体，所有失败都会计入 failed 列表返回。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Any, Callable, Iterable

import psutil

from .monitor import human_bytes

_HOME = os.path.expanduser("~")

# psutil / 系统进程名与 PID，清理时一律跳过
_SKIP_PID_MAX = 1

#: 系统保护路径（前缀匹配），删除/清理一律跳过
_SYSTEM_GUARDS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/lib32",
                  "/etc", "/boot", "/root", "/proc", "/sys", "/dev",
                  "/run", "/var/lib", "/snap")

#: ~/.cache 下划给「浏览器缓存」独立清理项的子目录，
#: 用户缓存清理时会排除它们，避免两项重复统计 / 重复删除
_BROWSER_CACHE_SUBDIRS = (
    "google-chrome", "google-chrome-beta", "google-chrome-unstable",
    "chromium", "microsoft-edge", "microsoft-edge-beta",
    "mozilla", "opera", "brave-browser", "vivaldi",
)


# --------------------------------------------------------------------------- #
# 路径安全工具
# --------------------------------------------------------------------------- #


def _is_link(path: str) -> bool:
    """符号链接 / 硬链接目录会导致越界删除，删除前跳过。"""
    try:
        return os.path.islink(path)
    except Exception:
        return False


def _safe_path(path: str) -> bool:
    """是否为允许删除的普通文件。"""
    try:
        if not path or not os.path.isabs(path):
            return False
        if os.path.islink(path):
            return False
        if not os.path.isfile(path):
            return False
    except Exception:
        return False
    return True


def _norm(path: str) -> str:
    return os.path.normpath(os.path.abspath(path))


def _is_system_path(path: str) -> bool:
    """系统保护路径，禁止删除（即使出现在扫描结果里）。"""
    p = _norm(path)
    for g in _SYSTEM_GUARDS:
        g = _norm(g)
        if p == g or p.startswith(g + os.sep):
            return True
    return False


# --------------------------------------------------------------------------- #
# 目录遍历工具
# --------------------------------------------------------------------------- #


def _iter_files(root: str, max_depth: int = 6,
                exclude_rel: tuple[str, ...] = ()):
    """安全遍历：不跟随符号链接，限制深度，单项错误忽略。

    exclude_rel: 需要跳过的相对路径前缀（如 ``mozilla`` 会跳过 mozilla/ 整棵子树）。
    """
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
            # 过滤符号链接目录与排除目录
            keep = []
            for d in dirnames:
                full = os.path.join(dirpath, d)
                if _is_link(full):
                    continue
                if exclude_rel:
                    rel = os.path.relpath(full, root).replace(os.sep, "/")
                    if any(rel == e or rel.startswith(e + "/") for e in exclude_rel):
                        continue
                keep.append(d)
            dirnames[:] = keep
            for f in filenames:
                yield os.path.join(dirpath, f)
    except Exception:
        return


def scan_dir(root: str, max_depth: int = 6,
             exclude_rel: tuple[str, ...] = ()) -> dict[str, Any]:
    """统计目录总大小与文件数（不删除）。"""
    total = 0
    count = 0
    for f in _iter_files(root, max_depth=max_depth, exclude_rel=exclude_rel):
        try:
            total += os.lstat(f).st_size
            count += 1
        except Exception:
            continue
    return {"size": total, "count": count, "size_text": human_bytes(total)}


def purge_dir(root: str, max_depth: int = 6, older_than_sec: float = 0.0,
              exclude_rel: tuple[str, ...] = ()) -> dict[str, Any]:
    """删除目录下的文件（不清空子目录本身），返回释放量与失败数。"""
    freed = 0
    deleted = 0
    failed = 0
    now = time.time()
    for f in _iter_files(root, max_depth=max_depth, exclude_rel=exclude_rel):
        try:
            st = os.lstat(f)
            if _is_link(f):
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


def _env_dirs(*candidates: str) -> list[str]:
    return [c for c in candidates if c and os.path.isdir(c)]


def build_targets() -> list[dict[str, Any]]:
    """磁盘清理项目清单。每项含一组白名单根目录（Linux 路径）。"""
    trash_dir = os.path.join(_HOME, ".local", "share", "Trash", "files")
    trash_info = os.path.join(_HOME, ".local", "share", "Trash", "info")

    targets: list[dict[str, Any]] = [
        {
            "id": "system_temp",
            "name": "系统临时文件",
            "group": "系统",
            "risk": "safe",
            "desc": "/tmp 与 /var/tmp，程序安装/运行时的中间产物，重启后通常不再需要",
            "admin": False,
            "paths": _env_dirs("/tmp", "/var/tmp"),
        },
        {
            "id": "package_cache",
            "name": "软件包下载缓存",
            "group": "系统",
            "risk": "safe",
            "desc": "apt / dnf / yum 已下载的安装包缓存，删除后不影响已安装的软件",
            "admin": True,
            "paths": _env_dirs("/var/cache/apt/archives", "/var/cache/dnf",
                               "/var/cache/yum", "/var/cache/pacman/pkg",
                               "/var/cache/zypp/packages"),
        },
        {
            "id": "journal_logs",
            "name": "systemd 系统日志",
            "group": "系统",
            "risk": "medium",
            "desc": "journald 持久化日志（/var/log/journal），仅用于排查问题",
            "admin": True,
            "paths": _env_dirs("/var/log/journal"),
        },
        {
            "id": "crash_dumps",
            "name": "崩溃转储",
            "group": "系统",
            "risk": "safe",
            "desc": "/var/crash 下的核心转储文件，已定位过问题即可删除",
            "admin": False,
            "paths": _env_dirs("/var/crash"),
        },
        {
            "id": "user_cache",
            "name": "用户应用缓存",
            "group": "用户",
            "risk": "safe",
            "desc": "~/.cache 下各应用的缓存数据（已排除浏览器缓存项）",
            "admin": False,
            "paths": _env_dirs(os.path.join(_HOME, ".cache")),
            "exclude_rel": _BROWSER_CACHE_SUBDIRS,
        },
        {
            "id": "browser_cache",
            "name": "浏览器缓存",
            "group": "浏览器",
            "risk": "safe",
            "desc": "Chrome / Firefox / Edge 等浏览器缓存文件（不含密码、书签、历史记录）",
            "admin": False,
            "paths": _env_dirs(*[
                os.path.join(_HOME, ".cache", name)
                for name in _BROWSER_CACHE_SUBDIRS
            ]),
        },
        {
            "id": "trash",
            "name": "回收站（垃圾箱）",
            "group": "用户",
            "risk": "safe",
            "desc": "~/.local/share/Trash 里已删除待还原的文件",
            "admin": False,
            "paths": _env_dirs(trash_dir, trash_info),
        },
    ]

    # 统一过滤存在性并补齐字段
    for t in targets:
        t["paths"] = [p for p in t["paths"] if os.path.isdir(p)]
        t.setdefault("exclude_rel", ())
        t["available"] = bool(t["paths"])
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
            info = scan_dir(p, exclude_rel=tuple(t.get("exclude_rel") or ()))
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
        exclude = tuple(t.get("exclude_rel") or ())
        item = {"id": t["id"], "name": t["name"], "risk": t["risk"],
                "freed": 0, "deleted": 0, "failed": 0}
        for root in t["paths"]:
            try:
                r = (scan_dir(root, exclude_rel=exclude) if dry_run
                     else purge_dir(root, exclude_rel=exclude))
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
    ".so", ".o", ".ko", ".deb", ".rpm", ".bin", ".elf", ".appimage",
    ".img",
}
_MEDIA_EXT = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".rmvb", ".mp3",
              ".wav", ".flac", ".m4a"}
_PACKAGE_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".xz", ".zst", ".bz2", ".iso"}
_DISK_EXT = {".iso", ".img", ".vdi", ".vmdk", ".qcow2", ".raw"}
_CACHE_EXT = {".log", ".tmp", ".temp", ".bak", ".old", ".cache", ".dmp", ".core"}


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
    if ext in _DISK_EXT:
        return {"risk": "high", "category": "磁盘镜像",
                "advice": "可能是系统备份/虚拟机磁盘，确认无用再删"}
    if ext in _MEDIA_EXT:
        return {"risk": "medium", "category": "影音文件", "advice": "个人媒体，需自行确认"}
    if ext in _PACKAGE_EXT:
        return {"risk": "medium", "category": "压缩包", "advice": "可能是安装包备份"}
    if ext in _CACHE_EXT or "/tmp/" in p or "/.cache/" in p or "/var/log/" in p:
        return {"risk": "low", "category": "缓存/日志", "advice": "通常是可安全清理的临时数据"}
    return {"risk": "medium", "category": "其他文件", "advice": "请自行确认是否仍需要"}


def list_drives() -> list[dict[str, Any]]:
    """列出真实磁盘分区（剔除 tmpfs / squashfs / loop 等伪设备）。"""
    rows = []
    for part in psutil.disk_partitions(all=False):
        fstype = (part.fstype or "").lower()
        if fstype in ("squashfs", "tmpfs", "devtmpfs", "iso9660", "overlay",
                      "proc", "sysfs", "devfs"):
            continue
        if part.device.startswith("/dev/loop"):
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
    "proc", "sys", "dev", "run", "boot", "usr", "etc", "snap",
    "lost+found", ".git", "node_modules", "__pycache__", "flatpak",
    "Trash", "waldo",
}


def scan_large_files(drive: str | None = None, min_mb: float = 100,
                     limit: int = 300, max_seconds: float = 25.0,
                     progress: Callable[[str, int], None] | None = None
                     ) -> dict[str, Any]:
    """扫描大文件。只读操作。

    为了不在全盘扫描上耗死，设置了时间上限；跳过系统目录以缩小范围并提高安全性。
    drive 参数在 Linux 下为挂载点前缀（如 /、/home）。
    """
    roots: list[str] = []
    for row in list_drives():
        mp = row["mountpoint"]
        if drive and not mp.startswith(drive.rstrip("/") or "/"):
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
                # 剔除无用目录与符号链接目录，speed + 安全双收益
                keep = []
                for d in dirnames:
                    if d in _SKIP_DIR_NAMES:
                        continue
                    if _is_link(os.path.join(dirpath, d)):
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

        if use_recycle:
            ok, reason = _trash_file(path)
            if not ok:
                failed += 1
                rows.append({"path": path, "ok": False, "reason": reason})
                continue
        else:
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


def _trash_file(path: str) -> tuple[bool, str]:
    """把单个文件送入 freedesktop 回收站。优先 gio trash，失败手动移入。"""
    try:
        proc = subprocess.run(["gio", "trash", path],
                              capture_output=True, timeout=15)
        if proc.returncode == 0:
            return True, ""
    except FileNotFoundError:
        pass
    except Exception:
        pass

    # gio 不可用：手动移入 ~/.local/share/Trash/files（不含 info 元数据，
    # 文件管理器仍可识别为回收站内容）
    trash_files = os.path.join(_HOME, ".local", "share", "Trash", "files")
    try:
        os.makedirs(trash_files, exist_ok=True)
        base = os.path.basename(path)
        dest = os.path.join(trash_files, base)
        n = 1
        while os.path.exists(dest):
            stem, ext = os.path.splitext(base)
            dest = os.path.join(trash_files, f"{stem}.{n}{ext}")
            n += 1
        shutil.move(path, dest)
        return True, ""
    except Exception as exc:
        return False, f"送入回收站失败：{exc}"


def empty_recycle_bin() -> tuple[bool, str]:
    """清空回收站（freedesktop Trash）。"""
    # 优先 gio trash --empty（会同时清 info 元数据）
    try:
        proc = subprocess.run(["gio", "trash", "--empty"],
                              capture_output=True, timeout=30)
        if proc.returncode == 0:
            return True, "回收站已清空"
    except FileNotFoundError:
        pass
    except Exception:
        pass

    # 手动清空 files 与 info 目录
    cleared = failed = 0
    for sub in ("files", "info"):
        d = os.path.join(_HOME, ".local", "share", "Trash", sub)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            full = os.path.join(d, name)
            try:
                if os.path.isdir(full) and not os.path.islink(full):
                    shutil.rmtree(full)
                else:
                    os.remove(full)
                cleared += 1
            except Exception:
                failed += 1
    if failed:
        return False, f"清空完成 {cleared} 项，失败 {failed} 项（可能需要检查权限）"
    return True, "回收站已清空"


# --------------------------------------------------------------------------- #
# 内存清理
# --------------------------------------------------------------------------- #


def _is_admin() -> bool:
    try:
        return os.geteuid() == 0  # type: ignore[attr-defined]
    except Exception:
        return False


def _write_drop_caches(level: int) -> tuple[bool, str]:
    """写 /proc/sys/vm/drop_caches 释放页缓存（需要 root）。"""
    if not _is_admin():
        return False, "需要 root 权限（请用 sudo 运行本工具）"
    try:
        with open("/proc/sys/vm/drop_caches", "w") as fh:
            fh.write(str(level))
        return True, "内核缓存已释放"
    except Exception as exc:
        return False, f"释放缓存失败：{exc}"


def _sync_disks() -> None:
    try:
        subprocess.run(["sync"], timeout=30)
    except Exception:
        pass


def clean_memory(trim_working_set: bool = True,
                 clear_file_cache: bool = False,
                 purge_standby: bool = False) -> dict[str, Any]:
    """一键内存清理（Linux 实现：sync + drop_caches）。

    说明：
    - Linux 没有 Windows 的 EmptyWorkingSet 等价接口；trim_working_set 映射为
      sync + 释放页缓存（drop_caches=1）。
    - clear_file_cache 与 trim_working_set 效果相同（drop_caches=1）。
    - purge_standby 映射为 drop_caches=3（页缓存 + dentry + inode）。
    - drop_caches 只释放缓存页，不会杀死进程、不会丢数据；
      释放后首次读文件会变慢属正常现象。
    """
    vm_before = psutil.virtual_memory()
    before_pct = round(vm_before.percent, 1)

    detail: dict[str, Any] = {}
    did_page_cache = False

    if trim_working_set or clear_file_cache:
        _sync_disks()
        ok, msg = _write_drop_caches(1)
        detail["page_cache"] = {"ok": ok, "message": msg}
        did_page_cache = ok

    if purge_standby:
        ok, msg = _write_drop_caches(3)
        detail["dentry_inode"] = {"ok": ok, "message": msg}

    if not detail:
        detail["noop"] = {"ok": True, "message": "未选择任何清理动作"}

    time.sleep(1.2)  # 等待内核完成页回收后再读一次
    vm_after = psutil.virtual_memory()
    after_pct = round(vm_after.percent, 1)

    freed = int(vm_before.used - vm_after.used)
    tip = ("drop_caches 只释放内核缓存页，不会关闭程序、不会丢失数据"
           if did_page_cache else
           "清理缓存需要 root 权限，请用 sudo 运行本工具后重试")
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
        "tip": tip,
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
        "trash_api": _trash_api_ok(),
    }


def _trash_api_ok() -> bool:
    """gio trash 可用性（不可用时手动移入回收站作为回退）。"""
    try:
        import shutil as _sh  # noqa: WPS433

        return _sh.which("gio") is not None
    except Exception:
        return False


if __name__ == "__main__":
    import json

    print(json.dumps(self_check(), ensure_ascii=False, indent=2))
