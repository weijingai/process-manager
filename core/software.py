# -*- coding: utf-8 -*-
"""文件搜索 与 软件清理（缓存清理 + 已安装软件卸载）。

分成两块能力：

1. **文件搜索**：按名称关键字在全盘（或指定盘符 / 指定根目录）查找文件与文件夹，
   支持只看文件 / 只看文件夹、按大小与时间排序、测算占用、打开文件位置、删除到回收站。
   全程只读遍历，限时 + 限量 + 跳过系统目录，避免拖慢系统。

2. **软件清理**：把「AI 编程工具缓存」「全盘扫描到的软件目录」「系统里已安装的软件」
   三类来源合并成一张清单，支持：
   · 清理缓存 / 残留目录（可再生数据，送回收站）
   · 调用软件自带的卸载程序卸载（只从注册表里取 UninstallString，绝不接受外部命令）

安全约束
--------
* 卸载命令只能来自本机注册表的 UninstallString / QuietUninstallString，
  并用本次扫描生成的内部索引 id 唤起，外部传入的任意命令一律拒绝，防止命令注入。
* 删除一律走 ``cleanup.delete_files``（回收站 + 系统保护路径跳过）。
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any, Iterable

import psutil

from .monitor import human_bytes

try:  # Windows 专有
    import winreg
except Exception:  # pragma: no cover - 非 Windows（如 Linux 版镜像不复用本文件）
    winreg = None  # type: ignore[assignment]

try:
    from .cleanup import _cache_sub_dirs, _dedup_roots, _norm  # noqa: PLC2701
except Exception:  # pragma: no cover
    _cache_sub_dirs = None  # type: ignore[assignment]
    _dedup_roots = None  # type: ignore[assignment]
    _norm = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #

# 遍历时跳过的目录名（小写）：既提速，也避免进系统 / 依赖黑洞
_SEARCH_SKIP = {
    "windows", "$recycle.bin", "system volume information", "recovery",
    "perflogs", "winsxs", "servicing", "boot", "efi", "drivers",
    "node_modules", ".git", ".svn", ".hg", "__pycache__", "site-packages",
    "dist-info", "venv", ".venv", "env", ".gradle", ".m2", ".nuget",
    "system32", "syswow64", "appdata", ".cache", "library", "contents",
    "resources", "locales", "resources.pak", ".wine", "proc", "sys", "dev",
}

# 单个分区默认的遍历深度上限（从盘符根目录算起）
_MAX_DEPTH_DEFAULT = 7


def _lower_keywords(keyword: str) -> list[str]:
    """把关键字串切成小写关键字列表（支持空格分隔的多关键字，任一命中即可）。"""
    parts = [p.strip().lower() for p in (keyword or "").replace("，", " ").split()]
    return [p for p in parts if p]


def _match(name_lower: str, parts: list[str]) -> bool:
    return all(p in name_lower for p in parts)


def list_search_drives() -> list[dict[str, Any]]:
    """可用于搜索的分区列表（排除光驱 / 无挂载点的伪分区）。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        parts = psutil.disk_partitions(all=False)
    except Exception:
        parts = []
    for p in parts:
        mp = (p.mountpoint or "").rstrip("\\/")
        if not mp or mp.lower() in seen:
            continue
        try:
            if "cdrom" in (p.opts or "").lower():
                continue
            if not os.path.isdir(mp):
                continue
        except Exception:
            continue
        seen.add(mp.lower())
        out.append({"value": mp, "label": mp, "fstype": p.fstype or ""})
    if not out:  # 兜底：至少给系统盘
        out.append({"value": os.environ.get("SystemDrive", "C:") or "C:",
                    "label": os.environ.get("SystemDrive", "C:") or "C:", "fstype": ""})
    return out


def _search_roots(drive: str = "", root: str = "") -> list[tuple[str, int]]:
    """返回遍历起点 [(绝对路径, 深度上限)]。

    · root 参数优先（用户指定了起始目录）
    · drive 指定单个分区
    · 都为空则遍历所有可用分区的根
    """
    if root:
        ap = os.path.abspath(root)
        return [(ap, _MAX_DEPTH_DEFAULT)] if os.path.isdir(ap) else []
    roots: list[tuple[str, int]] = []
    if drive:
        mp = drive.rstrip("\\/") or drive
        return [(mp, _MAX_DEPTH_DEFAULT)] if os.path.isdir(mp) else []
    for d in list_search_drives():
        roots.append((d["value"], _MAX_DEPTH_DEFAULT))
    return roots


def _entry_row(full: str, is_dir: bool, parent: str, size: int, mtime: float,
               category: str = "") -> dict[str, Any]:
    ext = "" if is_dir else os.path.splitext(full)[1].lower()
    return {
        "path": full,
        "name": os.path.basename(full.rstrip(os.sep)) or full,
        "parent": parent,
        "is_dir": bool(is_dir),
        "ext": ext,
        "size": size,
        "size_text": human_bytes(size) if size >= 0 else "文件夹",
        "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)),
        "mtime_ts": mtime,
        "age_days": int((time.time() - mtime) / 86400),
        "category": category or ("文件夹" if is_dir else (ext.lstrip(".").upper() or "文件")),
    }


def search_files(keyword: str = "", drive: str = "", root: str = "",
                 mode: str = "all", ext: str = "", min_size_mb: float = 0,
                 sort_by: str = "size", order: str = "desc",
                 limit: int = 400, max_seconds: float = 20.0,
                 max_depth: int = _MAX_DEPTH_DEFAULT,
                 with_dir_size: bool = False) -> dict[str, Any]:
    """按名称关键字搜索文件与文件夹。只读，绝不做任何修改。

    keyword      关键字（空格分隔多关键字，全部命中才算匹配，大小写不敏感）
    drive        限定分区；为空表示全盘
    root         限定起始目录（优先级高于 drive）
    mode         all / file / dir
    ext          扩展名过滤（如 ".log"，留空不过滤）
    min_size_mb  只列出 ≥ 该大小的文件（MB）
    with_dir_size 是否实时统计文件夹体积（很慢，默认关闭）
    """
    started = time.time()
    parts = _lower_keywords(keyword)
    result: dict[str, Any] = {
        "rows": [], "total_found": 0, "shown": 0, "scanned_dirs": 0,
        "scanned_files": 0, "keyword": keyword or "", "mode": mode,
        "drive": drive or "", "root": root or "", "ext": ext or "",
        "sort_by": sort_by, "order": order, "timed_out": False,
        "elapsed": 0.0, "roots": [r[0] for r in _search_roots(drive, root)],
        "hint": "",
    }
    if not parts:
        result["hint"] = "请输入要查找的文件或文件夹名称关键字。"
        return result

    mode = (mode or "all").lower()
    want_file = mode in ("all", "file")
    want_dir = mode in ("all", "dir")
    ext_filter = (ext or "").lower()
    if ext_filter and not ext_filter.startswith("."):
        ext_filter = "." + ext_filter
    min_size = int(float(min_size_mb or 0) * 1024 * 1024)

    rows: list[dict[str, Any]] = []
    timed_out = False
    scanned_dirs = scanned_files = 0
    soft_cap = max(400, int(limit) * 4)

    for root_path, depth_default in _search_roots(drive, root):
        if len(rows) >= soft_cap or time.time() - started > max_seconds:
            timed_out = len(rows) >= soft_cap or time.time() - started > max_seconds
            break
        base_depth = root_path.rstrip(os.sep).count(os.sep)
        depth_limit = min(int(max_depth or _MAX_DEPTH_DEFAULT), depth_default)
        try:
            walker = os.walk(root_path, topdown=True,
                             onerror=lambda e: None, followlinks=False)
        except Exception:
            continue
        try:
            for dirpath, dirnames, filenames in walker:
                if time.time() - started > max_seconds or len(rows) >= soft_cap:
                    timed_out = True
                    break
                scanned_dirs += 1
                depth = dirpath.rstrip(os.sep).count(os.sep) - base_depth

                # --- 目录匹配 ---
                keep: list[str] = []
                for d in dirnames:
                    dl = d.lower()
                    if dl in _SEARCH_SKIP:
                        continue
                    full = os.path.join(dirpath, d)
                    try:
                        st = os.lstat(full)
                        if hasattr(st, "st_reparse_tag") and st.st_reparse_tag:
                            continue  # Junction / 符号链接，避免重复与环路
                    except Exception:
                        continue
                    if want_dir and _match(dl, parts):
                        size = -1
                        if with_dir_size:
                            size, _cnt = _dir_size(full, budget=3.0)
                        rows.append(_entry_row(full, True, dirpath, size, st.st_mtime))
                    if depth + 1 < depth_limit:
                        keep.append(d)
                dirnames[:] = keep

                # --- 文件匹配 ---
                if want_file:
                    for f in filenames:
                        scanned_files += 1
                        fl = f.lower()
                        if not _match(fl, parts):
                            continue
                        if ext_filter and not fl.endswith(ext_filter):
                            continue
                        full = os.path.join(dirpath, f)
                        try:
                            st = os.lstat(full)
                            if st.st_size <= 0:
                                continue
                            if min_size and st.st_size < min_size:
                                continue
                        except Exception:
                            continue
                        rows.append(_entry_row(full, False, dirpath, st.st_size, st.st_mtime))
        except Exception:
            continue

    reverse = str(order).lower() != "asc"
    if str(sort_by).lower() == "time":
        rows.sort(key=lambda r: r["mtime_ts"], reverse=reverse)
    elif str(sort_by).lower() == "name":
        rows.sort(key=lambda r: r["name"].lower(), reverse=not reverse)
    else:
        # 文件夹未测算大小时为 -1，排在有大小的条目之后（升序时也一样放最后）
        rows.sort(key=lambda r: r["size"] if r["size"] >= 0 else -1, reverse=reverse)

    total = len(rows)
    top = rows[:max(1, min(int(limit or 400), 2000))]
    measured = sum(r["size"] for r in rows if r["size"] > 0)
    result.update({
        "rows": top,
        "total_found": total,
        "shown": len(top),
        "scanned_dirs": scanned_dirs,
        "scanned_files": scanned_files,
        "measured_text": human_bytes(measured),
        "sort_by": str(sort_by).lower(),
        "order": "desc" if reverse else "asc",
        "timed_out": timed_out,
        "elapsed": round(time.time() - started, 1),
    })
    if not top:
        result["hint"] = ("没有找到匹配的项目；可提高时间上限、放宽关键字，"
                          "或指定更小的起始目录后重试。")
    elif timed_out:
        result["hint"] = f"结果较多，已提前返回前 {len(top)} 项（共找到 {total} 项）。"
    return result


def _dir_size(path: str, budget: float = 10.0) -> tuple[int, int]:
    """统计目录体积（限时）。返回 (字节数, 文件数)。"""
    started = time.time()
    size = count = 0
    try:
        walker = os.walk(path, topdown=True, onerror=lambda e: None,
                         followlinks=False)
    except Exception:
        return 0, 0
    try:
        for dirpath, _dirnames, filenames in walker:
            if time.time() - started > budget:
                break
            for f in filenames:
                full = os.path.join(dirpath, f)
                try:
                    size += os.lstat(full).st_size
                    count += 1
                except Exception:
                    continue
    except Exception:
        pass
    return size, count


def measure_paths(paths: Iterable[str], max_seconds: float = 20.0) -> dict[str, Any]:
    """测算给定文件 / 文件夹的占用（用于搜索结果里补算文件夹体积）。只读。"""
    started = time.time()
    items: list[dict[str, Any]] = []
    for p in paths:
        p = (p or "").strip()
        if not p:
            continue
        try:
            st = os.lstat(p)
            if os.path.isdir(p) and not os.path.islink(p):
                size, count = _dir_size(p, budget=max(2.0, max_seconds / max(1, len(paths))))
            else:
                size, count = st.st_size, 1
        except Exception:
            size, count = -1, 0
        items.append({
            "path": p,
            "size": size,
            "size_text": human_bytes(size) if size >= 0 else "—",
            "count": count,
        })
        if time.time() - started > max_seconds:
            break
    total = sum(i["size"] for i in items if i["size"] > 0)
    return {"items": items, "total": total, "total_text": human_bytes(total),
            "elapsed": round(time.time() - started, 1)}


def reveal_path(path: str) -> tuple[bool, str]:
    """在资源管理器中打开文件所在位置 / 打开文件夹。"""
    p = (path or "").strip()
    if not p or not os.path.exists(p):
        return False, f"路径不存在：{p}"
    try:
        if os.path.isdir(p):
            subprocess.Popen(["explorer", os.path.abspath(p)])
        else:
            subprocess.Popen(["explorer", "/select,", os.path.abspath(p)])
        return True, "已在资源管理器中打开"
    except Exception as exc:
        return False, f"打开失败：{exc}"


# --------------------------------------------------------------------------- #
# 已安装软件（注册表卸载项）
# --------------------------------------------------------------------------- #

_UNINSTALL_SUB = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
_UNINSTALL_WOW = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"

# ident -> {"hive","flags","subkey"}，仅本次进程内缓存；卸载时按 ident 回查注册表
_UNINSTALL_INDEX: dict[str, dict[str, Any]] = {}


def _reg_val(key, name: str, default: Any = "") -> Any:
    try:
        return winreg.QueryValueEx(key, name)[0]
    except Exception:
        return default


def _iter_uninstall_keys() -> Iterable[tuple[str, int, str]]:
    """遍历三处卸载项注册表位置：HKLM(64) / HKLM(32) / HKCU。"""
    if winreg is None:
        return
    yield ("HKLM", winreg.KEY_WOW64_64KEY, _UNINSTALL_SUB)
    yield ("HKLM32", winreg.KEY_WOW64_32KEY, _UNINSTALL_WOW)
    yield ("HKCU", 0, _UNINSTALL_SUB)


def _clean_reg_str(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text.replace("\x00", "").strip()


def _clean_path(value: str) -> str:
    """去掉注册表路径常见的首尾引号（如 "C:\\Program Files\\Tencent\\Weixin"）。"""
    p = _clean_reg_str(value).strip().strip('"').strip("'").strip()
    return p.rstrip("\\/") + os.sep if len(p) > 2 and p[1] == ":" else p


def installed_software(max_seconds: float = 15.0) -> dict[str, Any]:
    """读取系统里「已安装软件」清单（来自卸载表项）。只读，不执行任何命令。"""
    started = time.time()
    rows: list[dict[str, Any]] = []
    _UNINSTALL_INDEX.clear()
    if winreg is None:
        return {"items": [], "count": 0, "total_text": "0 B",
                "hint": "当前不是 Windows 环境，无法读取已安装软件清单。",
                "elapsed": 0.0}

    seen: dict[str, dict[str, Any]] = {}
    for hive_name, flags, sub in _iter_uninstall_keys():
        hive = winreg.HKEY_LOCAL_MACHINE if hive_name.startswith("HKLM") else winreg.HKEY_CURRENT_USER
        try:
            root = winreg.OpenKey(hive, sub, 0, winreg.KEY_READ | flags)
        except Exception:
            continue
        i = 0
        while True:
            if time.time() - started > max_seconds:
                break
            try:
                sub_name = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                k = winreg.OpenKey(root, sub_name, 0, winreg.KEY_READ | flags)
            except Exception:
                continue
            try:
                name = _clean_reg_str(_reg_val(k, "DisplayName"))
                if not name:
                    continue
                # 补丁 / 子组件 / 系统更新不按「软件」展示
                if _clean_reg_str(_reg_val(k, "ParentKeyName")):
                    continue
                release = _clean_reg_str(_reg_val(k, "ReleaseType"))
                if release.lower() in ("hotfix", "security update", "update rollup"):
                    continue
                if int(_reg_val(k, "SystemComponent", 0) or 0) == 1:
                    continue
                uninst = _clean_reg_str(_reg_val(k, "UninstallString"))
                version = _clean_reg_str(_reg_val(k, "DisplayVersion"))
                publisher = _clean_reg_str(_reg_val(k, "Publisher"))
                location = _clean_path(_reg_val(k, "InstallLocation"))
                try:
                    est = int(_reg_val(k, "EstimatedSize", 0) or 0) * 1024
                except Exception:
                    est = 0
                install_date = _clean_reg_str(_reg_val(k, "InstallDate"))
                if len(install_date) == 8 and install_date.isdigit():
                    install_date = f"{install_date[:4]}-{install_date[4:6]}-{install_date[6:]}"

                ident = f"{hive_name}:{sub_name}"
                _UNINSTALL_INDEX[ident] = {"hive": hive_name, "flags": flags,
                                           "sub": sub, "key": sub_name}
                dedup = (name.lower(), (publisher or "").lower())
                item = {
                    "key": "inst:" + ident,
                    "ident": ident,
                    "name": name,
                    "source": "已安装",
                    "source_key": "installed",
                    "version": version,
                    "publisher": publisher or "—",
                    "location": location,
                    "size": est,
                    "size_text": human_bytes(est) if est > 0 else "—",
                    "install_date": install_date or "—",
                    "can_uninstall": bool(uninst),
                    "cache_size": 0,
                    "cache_text": "—",
                    "cache_count": 0,
                    "paths": [location] if location and os.path.isdir(location) else [],
                }
                old = seen.get(dedup)
                # 同一软件在 HKLM / HKCU / Wow6432Node 可能重复出现，保留占用更大的那条
                if old is None or item["size"] > old["size"]:
                    seen[dedup] = item
            finally:
                try:
                    winreg.CloseKey(k)
                except Exception:
                    pass
        try:
            winreg.CloseKey(root)
        except Exception:
            pass

    rows = sorted(seen.values(), key=lambda x: x["size"], reverse=True)
    total = sum(r["size"] for r in rows if r["size"] > 0)
    return {
        "items": rows,
        "count": len(rows),
        "total": total,
        "total_text": human_bytes(total),
        "hint": "" if rows else "未能读取到已安装软件清单。",
        "elapsed": round(time.time() - started, 1),
    }


def uninstall_software(ident: str, quiet: bool = False) -> tuple[bool, str]:
    """调用软件自带的卸载程序。

    ident 必须是 installed_software() 扫描时生成的内部 id —— 命令只能来自本机注册表，
    不接受任何外部字符串，避免命令注入。

    quiet=True 时优先使用注册表中的 QuietUninstallString；没有则退回原卸载命令。
    """
    if winreg is None:
        return False, "当前不是 Windows 环境，无法卸载软件。"
    info = _UNINSTALL_INDEX.get(ident or "")
    if not info:
        return False, "未找到该软件（请先在软件清理页执行一次扫描）。"
    hive = winreg.HKEY_LOCAL_MACHINE if info["hive"].startswith("HKLM") else winreg.HKEY_CURRENT_USER
    try:
        k = winreg.OpenKey(hive, info["sub"] + "\\" + info["key"], 0,
                           winreg.KEY_READ | info["flags"])
    except Exception as exc:
        return False, f"无法打开卸载表项：{exc}"
    try:
        name = _clean_reg_str(_reg_val(k, "DisplayName")) or "该软件"
        cmd = ""
        if quiet:
            cmd = _clean_reg_str(_reg_val(k, "QuietUninstallString"))
        if not cmd:
            cmd = _clean_reg_str(_reg_val(k, "UninstallString"))
            if quiet and "msiexec" in cmd.lower():
                cmd += " /qb-"
    finally:
        try:
            winreg.CloseKey(k)
        except Exception:
            pass
    if not cmd:
        return False, f"「{name}」没有提供卸载命令，请手动在系统中卸载。"
    try:
        subprocess.Popen(cmd, shell=True, close_fds=True)
    except Exception as exc:
        return False, f"启动卸载程序失败：{exc}"
    return True, f"已启动「{name}」的卸载程序，请在弹出的窗口中完成后续步骤。"


# --------------------------------------------------------------------------- #
# 软件清理：合并清单
# --------------------------------------------------------------------------- #

def _roots_for_item(item: dict[str, Any]) -> list[str]:
    roots = [p for p in (item.get("paths") or []) if p and os.path.isdir(p)]
    if roots and _dedup_roots is not None:
        return [r for r in _dedup_roots(roots)]
    out: list[str] = []
    seen: set[str] = set()
    for p in roots:
        ap = os.path.abspath(p)
        k = ap.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(ap)
    return out


def software_cleanup_summary(include_installed: bool = True,
                             include_agents: bool = True,
                             include_discovered: bool = False,
                             max_seconds: float = 25.0) -> dict[str, Any]:
    """软件清理总览：把三类来源合并成一张清单（默认扫描全部软件）。

    · installed   系统已安装软件（注册表卸载表项），可卸载
    · agents      AI 编程工具的缓存目录，可清理
    · discovered  全盘搜索到的软件目录（较慢，默认关闭）
    """
    started = time.time()
    items: list[dict[str, Any]] = []
    counts = {"installed": 0, "agents": 0, "discovered": 0}

    if include_installed:
        inst = installed_software(max_seconds=min(12.0, max_seconds))
        for r in inst["items"]:
            r["cache_text"] = "—"
            items.append(r)
        counts["installed"] = inst["count"]

    if include_agents and time.time() - started < max_seconds:
        # 给 AI 工具统计留出预算：目录体积测算较慢，单独限时避免拖慢整页
        agent_budget = max(5.0, min(15.0, max_seconds - (time.time() - started)))
        try:
            from . import cleanup as _cln
            ag = _cln.agent_summary(max_seconds=agent_budget)
            for a in ag["items"]:
                if not a.get("available"):
                    continue
                paths = [p for p in (a.get("paths") or []) if p and os.path.isdir(p)]
                items.append({
                    "key": "agent:" + a["id"],
                    "ident": a["id"],
                    "name": a["name"],
                    "source": "AI 工具",
                    "source_key": "agents",
                    "version": "—",
                    "publisher": a.get("desc") or "AI 编程工具",
                    "location": paths[0] if paths else "",
                    "size": a.get("size", 0),
                    "size_text": a.get("size_text", "0 B"),
                    "install_date": "—",
                    "can_uninstall": False,
                    "cache_size": a.get("size", 0),
                    "cache_text": (a.get("size_text") or "—") if a.get("size", 0) > 0 else "—",
                    "cache_count": a.get("count", 0),
                    "paths": paths,
                })
                counts["agents"] += 1
        except Exception:
            pass

    if include_discovered and time.time() - started < max_seconds:
        try:
            from . import cleanup as _cln
            sv = _cln.scan_software_dirs(max_seconds=max(6.0, max_seconds - (time.time() - started)))
            for s in sv["items"]:
                items.append({
                    "key": "dir:" + s["id"],
                    "ident": s["path"],
                    "name": s["name"],
                    "source": "扫描发现",
                    "source_key": "discovered",
                    "version": "—",
                    "publisher": s.get("category") or "目录",
                    "location": s["path"],
                    "size": max(0, s.get("size", 0)),
                    "size_text": s.get("size_text", "—"),
                    "install_date": "—",
                    "can_uninstall": False,
                    "cache_size": max(0, s.get("size", 0)),
                    "cache_text": s.get("size_text", "—"),
                    "cache_count": s.get("count", 0),
                    "paths": [s["path"]],
                })
                counts["discovered"] += 1
        except Exception:
            pass

    total_cache = sum(i.get("cache_size", 0) for i in items)
    items.sort(key=lambda x: (x.get("cache_size", 0), x.get("size", 0)), reverse=True)
    return {
        "items": items,
        "count": len(items),
        "counts": counts,
        "total_cache": total_cache,
        "total_cache_text": human_bytes(total_cache),
        "installed_total": human_bytes(sum(i["size"] for i in items
                                           if i["source_key"] == "installed" and i["size"] > 0)),
        "elapsed": round(time.time() - started, 1),
        "timed_out": time.time() - started > max_seconds,
        "hint": "",
    }


def software_cache_files(key: str = "", scope: str = "cache",
                         sort_by: str = "size", order: str = "desc",
                         limit: int = 400, max_seconds: float = 20.0) -> dict[str, Any]:
    """列出某个软件目录下可清理的文件。

    key 为 software_cleanup_summary() 返回的条目 key（inst:… / agent:… / dir:…），
    也可以直接传绝对路径。scope='cache' 只列缓存 / 日志；'all' 列出全部文件。
    """
    started = time.time()
    if not key:
        return {"rows": [], "total_found": 0, "shown": 0, "dir_size": 0,
                "dir_size_text": "0 B", "dir_count": 0, "roots": [],
                "label": "", "scope": scope, "timed_out": False, "elapsed": 0.0,
                "hint": "请先选择一项软件。"}

    roots: list[str] = []
    label = key
    if key.startswith("agent:"):
        tool_id = key[6:]
        try:
            from . import cleanup as _cln
            spec = {a["id"]: a for a in _cln._agent_spec()}
            a = spec.get(tool_id)
            if a is not None:
                label = a["name"]
                roots = [p for p in a["paths"] if p and os.path.isdir(p)]
        except Exception:
            pass
    elif key.startswith("dir:"):
        p = key[4:]
        if os.path.isdir(p):
            roots = [os.path.abspath(p)]
            label = os.path.basename(p.rstrip(os.sep)) or p
    elif os.path.isabs(key) and os.path.isdir(key):
        roots = [os.path.abspath(key)]
        label = os.path.basename(key.rstrip(os.sep)) or key
    else:  # inst:… 或其它未知 key：交给调用方传路径处理
        # 尝试从注册表 InstallLocation 回填（需要一次 installed_software 扫描）
        if key.startswith("inst:"):
            ident = key[5:]
            info = installed_software().get("items") or []
            for it in info:
                if it.get("ident") == ident:
                    label = it["name"]
                    roots = _roots_for_item(it)
                    break
        if not roots:
            return {"rows": [], "total_found": 0, "shown": 0, "dir_size": 0,
                    "dir_size_text": "0 B", "dir_count": 0, "roots": [],
                    "label": label, "scope": scope, "timed_out": False,
                    "elapsed": round(time.time() - started, 1),
                    "hint": "该软件没有可识别的安装目录，无法列出缓存文件。"}

    if not roots:
        return {"rows": [], "total_found": 0, "shown": 0, "dir_size": 0,
                "dir_size_text": "0 B", "dir_count": 0, "roots": [],
                "label": label, "scope": scope, "timed_out": False,
                "elapsed": round(time.time() - started, 1),
                "hint": "本机未检测到该软件的目录。"}

    scan_roots: list[str] = []
    fallback = False
    if str(scope).lower() == "cache" and _cache_sub_dirs is not None:
        for r in roots:
            scan_roots.extend(_cache_sub_dirs(r))
        if _dedup_roots is not None:
            scan_roots = [r for r in _dedup_roots(scan_roots)]
        if not scan_roots and not key.startswith("inst:"):
            # AI 工具等目录本身就是纯数据 / 缓存目录，没有再下一层的 cache 子目录，
            # 直接以根目录作为扫描起点，避免点开一片空白。
            # 已安装软件的安装目录不在此列（里面是程序本体，不可当成缓存列出）。
            scan_roots = _roots_for_item({"paths": roots})
            scope = "all"
            fallback = True
    else:
        scan_roots = _roots_for_item({"paths": roots})

    if not scan_roots:
        if key.startswith("inst:"):
            return {"rows": [], "total_found": 0, "shown": 0, "dir_size": 0,
                    "dir_size_text": "0 B", "dir_count": 0, "roots": roots,
                    "label": label, "scope": "cache", "timed_out": False,
                    "elapsed": round(time.time() - started, 1),
                    "hint": "该软件的安装目录下没有识别到缓存 / 日志子目录；"
                            "程序本体文件不建议清理，如需腾空间请改用卸载。"}
        return {"rows": [], "total_found": 0, "shown": 0, "dir_size": 0,
                "dir_size_text": "0 B", "dir_count": 0, "roots": roots,
                "label": label, "scope": "cache", "timed_out": False,
                "elapsed": round(time.time() - started, 1),
                "hint": "本机未检测到该软件的缓存文件。"}

    rows: list[dict[str, Any]] = []
    timed_out = False
    for root in scan_roots:
        if time.time() - started > max_seconds:
            timed_out = True
            break
        try:
            walker = os.walk(root, topdown=True, onerror=lambda e: None,
                             followlinks=False)
        except Exception:
            continue
        for dirpath, _dirnames, filenames in walker:
            if time.time() - started > max_seconds:
                timed_out = True
                break
            for f in filenames:
                full = os.path.join(dirpath, f)
                try:
                    st = os.lstat(full)
                    if st.st_size <= 0:
                        continue
                    rows.append({
                        "path": full,
                        "name": f,
                        "size": st.st_size,
                        "size_text": human_bytes(st.st_size),
                        "mtime": time.strftime("%Y-%m-%d %H:%M",
                                               time.localtime(st.st_mtime)),
                        "mtime_ts": st.st_mtime,
                        "age_days": int((time.time() - st.st_mtime) / 86400),
                        "tool": label,
                    })
                except Exception:
                    continue

    reverse = str(order).lower() != "asc"
    if str(sort_by).lower() == "time":
        rows.sort(key=lambda r: r["mtime_ts"], reverse=reverse)
    else:
        rows.sort(key=lambda r: r["size"], reverse=reverse)
    top = rows[:max(1, min(int(limit or 400), 5000))]
    dir_size = sum(r["size"] for r in rows)
    return {
        "rows": top,
        "total_found": len(rows),
        "shown": len(top),
        "dir_size": dir_size,
        "dir_size_text": human_bytes(dir_size),
        "dir_count": len(rows),
        "roots": roots,
        "label": label,
        "scope": "cache" if str(scope).lower() == "cache" else "all",
        "sort_by": str(sort_by).lower(),
        "order": "desc" if reverse else "asc",
        "timed_out": timed_out,
        "elapsed": round(time.time() - started, 1),
        "hint": "该目录本身就是缓存 / 数据目录，已直接列出其中的全部文件。"
                if fallback else "",
    }
