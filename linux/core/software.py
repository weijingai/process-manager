# -*- coding: utf-8 -*-
"""Linux 版：文件搜索 与 软件清理（缓存清理 + 已安装软件卸载）。

接口与 Windows 版 core/software.py 完全同名，便于 Web 前端三端共用：
    list_search_drives / search_files / measure_paths / reveal_path
    installed_software / uninstall_software
    software_cleanup_summary / software_cache_files

差异：
  · Windows 用注册表读卸载项，Linux 用发行版自带的包管理器（dpkg / rpm / pacman）
  · 打开位置用 xdg-open
  · 卸载需要 root：优先 pkexec（图形提权），失败回退 sudo -n
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Any, Iterable

import psutil

from .monitor import human_bytes

try:
    from .cleanup import _cache_sub_dirs, _dedup_roots, _norm  # noqa: PLC2701
except Exception:  # pragma: no cover
    _cache_sub_dirs = None  # type: ignore[assignment]
    _dedup_roots = None  # type: ignore[assignment]
    _norm = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# 文件搜索
# --------------------------------------------------------------------------- #

_SEARCH_SKIP = {
    "proc", "sys", "dev", "run", "snap", ".git", ".svn", ".hg", "__pycache__",
    "node_modules", "site-packages", "dist-info", "venv", ".venv", ".cache",
    ".gradle", ".m2", ".nuget", "lost+found", ".wine", ".steam", "flatpak",
    "apparmor.d", "systemd", "udev", "cgroup", "tmp", "var",
}

# 虚拟 / 伪文件系统：不参与全盘搜索
_SKIP_FSTYPES = {
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2",
    "pstore", "securityfs", "debugfs", "tracefs", "configfs", "selinuxfs",
    "bpf", "autofs", "mqueue", "hugetlbfs", "fusectl", "binfmt_misc",
}

_MAX_DEPTH_DEFAULT = 7


def _lower_keywords(keyword: str) -> list[str]:
    parts = [p.strip().lower() for p in (keyword or "").replace("，", " ").split()]
    return [p for p in parts if p]


def _match(name_lower: str, parts: list[str]) -> bool:
    return all(p in name_lower for p in parts)


def list_search_drives() -> list[dict[str, Any]]:
    """可搜索的挂载点列表（排除虚拟文件系统）。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        parts = psutil.disk_partitions(all=False)
    except Exception:
        parts = []
    for p in parts:
        fstype = (p.fstype or "").lower()
        mp = p.mountpoint or ""
        if fstype in _SKIP_FSTYPES or not mp:
            continue
        # /boot /efi 之类的小分区通常无需搜索
        if any(part in ("boot", "efi") for part in mp.strip("/").split("/")):
            continue
        if mp in seen:
            continue
        try:
            if not os.path.isdir(mp):
                continue
        except Exception:
            continue
        seen.add(mp)
        label = mp
        try:
            usage = psutil.disk_usage(mp)
            label = f"{mp}（可用 {human_bytes(usage.free)}）"
        except Exception:
            pass
        out.append({"value": mp, "label": label, "fstype": fstype})
    if not out:
        home = os.path.expanduser("~")
        out.append({"value": home, "label": home, "fstype": ""})
    return out


def _search_roots(drive: str = "", root: str = "") -> list[tuple[str, int]]:
    if root:
        ap = os.path.abspath(os.path.expanduser(root))
        return [(ap, _MAX_DEPTH_DEFAULT)] if os.path.isdir(ap) else []
    if drive:
        mp = drive.rstrip("/") or "/"
        return [(mp, _MAX_DEPTH_DEFAULT)] if os.path.isdir(mp) else []
    return [(d["value"], _MAX_DEPTH_DEFAULT) for d in list_search_drives()]


def _entry_row(full: str, is_dir: bool, parent: str, size: int,
               mtime: float) -> dict[str, Any]:
    ext = "" if is_dir else os.path.splitext(full)[1].lower()
    return {
        "path": full,
        "name": os.path.basename(full.rstrip("/")) or full,
        "parent": parent,
        "is_dir": bool(is_dir),
        "ext": ext,
        "size": size,
        "size_text": human_bytes(size) if size >= 0 else "文件夹",
        "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)),
        "mtime_ts": mtime,
        "age_days": int((time.time() - mtime) / 86400),
        "category": ("文件夹" if is_dir else (ext.lstrip(".").upper() or "文件")),
    }


def search_files(keyword: str = "", drive: str = "", root: str = "",
                 mode: str = "all", ext: str = "", min_size_mb: float = 0,
                 sort_by: str = "size", order: str = "desc",
                 limit: int = 400, max_seconds: float = 20.0,
                 max_depth: int = _MAX_DEPTH_DEFAULT,
                 with_dir_size: bool = False) -> dict[str, Any]:
    """按名称关键字搜索文件与文件夹。只读，绝不做任何修改。"""
    started = time.time()
    parts = _lower_keywords(keyword)
    result: dict[str, Any] = {
        "rows": [], "total_found": 0, "shown": 0, "scanned_dirs": 0,
        "scanned_files": 0, "keyword": keyword or "", "mode": mode,
        "drive": drive or "", "root": root or "", "ext": ext or "",
        "sort_by": sort_by, "order": order, "timed_out": False, "elapsed": 0.0,
        "roots": [r[0] for r in _search_roots(drive, root)], "hint": "",
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
            timed_out = True
            break
        base_depth = root_path.rstrip("/").count("/")
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
                depth = dirpath.rstrip("/").count("/") - base_depth
                keep: list[str] = []
                for d in dirnames:
                    dl = d.lower()
                    if dl in _SEARCH_SKIP:
                        continue
                    full = os.path.join(dirpath, d)
                    try:
                        st = os.lstat(full)
                        if os.path.islink(full):
                            continue
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
    sort_by = str(sort_by).lower()
    if sort_by == "time":
        rows.sort(key=lambda r: r["mtime_ts"], reverse=reverse)
    elif sort_by == "name":
        rows.sort(key=lambda r: r["name"].lower(), reverse=not reverse)
    else:
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
        "sort_by": sort_by,
        "order": "desc" if reverse else "asc",
        "timed_out": timed_out,
        "elapsed": round(time.time() - started, 1),
    })
    if not top:
        result["hint"] = "没有找到匹配的项目；可提高时间上限或放宽关键字后重试。"
    elif timed_out:
        result["hint"] = f"结果较多，已提前返回前 {len(top)} 项（共找到 {total} 项）。"
    return result


def _dir_size(path: str, budget: float = 10.0) -> tuple[int, int]:
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
    """测算给定文件 / 文件夹的占用（只读）。"""
    started = time.time()
    items: list[dict[str, Any]] = []
    listed = list(paths or [])
    for p in listed:
        p = (p or "").strip()
        if not p:
            continue
        try:
            st = os.lstat(p)
            if os.path.isdir(p) and not os.path.islink(p):
                budget = max(2.0, max_seconds / max(1, len(listed)))
                size, count = _dir_size(p, budget=budget)
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
    """在文件管理器中打开所在目录。"""
    p = (path or "").strip()
    if not p or not os.path.exists(p):
        return False, f"路径不存在：{p}"
    target = p if os.path.isdir(p) else os.path.dirname(p)
    opener = None
    for cand in ("xdg-open", "nautilus", "dolphin", "thunar", "nemo"):
        found = shutil.which(cand)
        if found:
            opener = found
            break
    if not opener:
        return False, "未找到可用的文件管理器（xdg-open 等），请手动打开该路径。"
    try:
        subprocess.Popen([opener, os.path.abspath(target)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, "已在文件管理器中打开"
    except Exception as exc:
        return False, f"打开失败：{exc}"


# --------------------------------------------------------------------------- #
# 已安装软件（发行版包管理器）
# --------------------------------------------------------------------------- #

_PKG_CACHE: dict[str, Any] = {"items": [], "stamp": 0.0}


def _run(cmd: list[str], timeout: float = 12.0) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              check=False)
    except Exception:
        return ""
    raw = proc.stdout or b""
    return raw.decode("utf-8", errors="replace")


def _has(cmd: str) -> bool:
    return bool(shutil.which(cmd))


def _parse_dpkg() -> list[dict[str, Any]]:
    # Package \t Version \t Installed-Size(KB) \t Maintainer
    text = _run(["dpkg-query", "-W", "-f=${Package}\\t${Version}\\t${Installed-Size}\\t${Status}\\n"])
    items: list[dict[str, Any]] = []
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) < 4:
            continue
        name, version, size_kb, status = cols[0], cols[1], cols[2], cols[3]
        if not name or "install ok installed" not in status:
            continue
        try:
            size = int(size_kb or 0) * 1024
        except Exception:
            size = 0
        items.append({
            "key": "pkg:" + name,
            "ident": name,
            "name": name,
            "source": "已安装",
            "source_key": "installed",
            "version": version or "—",
            "publisher": "dpkg / apt",
            "location": "",
            "size": size,
            "size_text": human_bytes(size) if size > 0 else "—",
            "install_date": "—",
            "can_uninstall": True,
            "cache_size": 0,
            "cache_text": "—",
            "cache_count": 0,
            "paths": [],
        })
    return items


def _parse_rpm() -> list[dict[str, Any]]:
    text = _run(["rpm", "-qa", "--qf", "%{NAME}\\t%{VERSION}-%{RELEASE}\\t%{SIZE}\\t%{VENDOR}\\n"])
    items: list[dict[str, Any]] = []
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) < 3 or not cols[0]:
            continue
        try:
            size = int(cols[2] or 0)
        except Exception:
            size = 0
        items.append({
            "key": "pkg:" + cols[0],
            "ident": cols[0],
            "name": cols[0],
            "source": "已安装",
            "source_key": "installed",
            "version": cols[1] or "—",
            "publisher": (cols[3] if len(cols) > 3 and cols[3] else "rpm / dnf"),
            "location": "",
            "size": size,
            "size_text": human_bytes(size) if size > 0 else "—",
            "install_date": "—",
            "can_uninstall": True,
            "cache_size": 0,
            "cache_text": "—",
            "cache_count": 0,
            "paths": [],
        })
    return items


def _parse_pacman() -> list[dict[str, Any]]:
    text = _run(["pacman", "-Qi"])
    items: list[dict[str, Any]] = []
    cur: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            if cur:
                item = _pacman_item(cur)
                if item:
                    items.append(item)
            cur = {}
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            cur[k.strip()] = v.strip()
    if cur:
        item = _pacman_item(cur)
        if item:
            items.append(item)
    return items


def _pacman_item(info: dict[str, str]) -> dict[str, Any] | None:
    name = info.get("Name", "")
    if not name:
        return None
    try:
        size = int(float((info.get("Installed Size") or "0").split()[0]) *
                   (1024 if "KiB" in (info.get("Installed Size") or "") else 1))
    except Exception:
        size = 0
    return {
        "key": "pkg:" + name,
        "ident": name,
        "name": name,
        "source": "已安装",
        "source_key": "installed",
        "version": info.get("Version", "—"),
        "publisher": info.get("Packager") or "pacman",
        "location": "",
        "size": size,
        "size_text": human_bytes(size) if size > 0 else "—",
        "install_date": info.get("Install Date", "—"),
        "can_uninstall": True,
        "cache_size": 0,
        "cache_text": "—",
        "cache_count": 0,
        "paths": [],
    }


def installed_software(max_seconds: float = 15.0) -> dict[str, Any]:
    """读取本机已安装软件包清单（dpkg / rpm / pacman）。只读。"""
    started = time.time()
    items: list[dict[str, Any]] = []
    manager = "—"
    if _has("dpkg-query"):
        items = _parse_dpkg()
        manager = "dpkg / apt"
    elif _has("rpm"):
        items = _parse_rpm()
        manager = "rpm / dnf / yum"
    elif _has("pacman"):
        items = _parse_pacman()
        manager = "pacman"
    for i in items:
        i["publisher"] = f"{i['publisher']} · {manager}" if manager != "—" else i["publisher"]
    items.sort(key=lambda x: x["size"], reverse=True)
    _PKG_CACHE["items"] = items
    _PKG_CACHE["stamp"] = time.time()
    total = sum(i["size"] for i in items if i["size"] > 0)
    return {
        "items": items,
        "count": len(items),
        "total": total,
        "total_text": human_bytes(total),
        "manager": manager,
        "hint": "" if items else "未找到支持的包管理器（dpkg / rpm / pacman）。",
        "elapsed": round(time.time() - started, 1),
    }


def uninstall_software(ident: str, quiet: bool = False) -> tuple[bool, str]:
    """调用包管理器卸载指定软件包（需要 root，走 pkexec / sudo -n）。

    ident 必须是 installed_software() 返回的包名，命令由本模块拼装，
    不接受任何外部命令行，避免命令注入。
    """
    name = (ident or "").strip()
    if not name:
        return False, "请指定要卸载的软件包。"
    # 简单白名单：包名只允许常见字符，杜绝把 shell 元字符带进命令行
    if not all(c.isalnum() or c in "._+-" for c in name):
        return False, f"软件包名不合法：{name}"

    if _has("apt-get"):
        action = ["apt-get", "remove", "-y", name]
    elif _has("dnf"):
        action = ["dnf", "remove", "-y", name]
    elif _has("yum"):
        action = ["yum", "remove", "-y", name]
    elif _has("pacman"):
        action = ["pacman", "-Rs", "--noconfirm", name]
    else:
        return False, "未找到支持的包管理器（apt-get / dnf / yum / pacman）。"

    prefix: list[str] = []
    if os.geteuid() != 0 if hasattr(os, "geteuid") else True:
        if shutil.which("pkexec"):
            prefix = ["pkexec"]
        elif shutil.which("sudo"):
            prefix = ["sudo", "-n"]
    cmd = prefix + action
    if quiet and action[0] in ("apt-get", "dnf", "yum"):
        os.environ.setdefault("DEBIAN_FRONTEND", "noninteractive")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except Exception as exc:
        return False, f"启动卸载失败：{exc}"
    return True, (f"已提交卸载请求：{name}（{' '.join(cmd)}）\n"
                  "若在终端运行，请查看工具输出的卸载进度。")


# --------------------------------------------------------------------------- #
# 软件清理：合并清单
# --------------------------------------------------------------------------- #

def _unique_roots(roots: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for p in roots:
        if not p:
            continue
        ap = os.path.abspath(os.path.expanduser(p))
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
    """把系统已安装软件与 AI 编程工具缓存合并为一张清单。"""
    started = time.time()
    items: list[dict[str, Any]] = []
    counts = {"installed": 0, "agents": 0, "discovered": 0}

    if include_installed:
        inst = installed_software(max_seconds=min(12.0, max_seconds))
        items.extend(inst["items"])
        counts["installed"] = inst["count"]

    if include_agents and time.time() - started < max_seconds:
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
    """列出某个软件（AI 工具缓存目录 / 指定路径）下可清理的文件。只读。"""
    started = time.time()
    empty = {"rows": [], "total_found": 0, "shown": 0, "dir_size": 0,
             "dir_size_text": "0 B", "dir_count": 0, "roots": [],
             "label": key, "scope": scope, "timed_out": False,
             "elapsed": 0.0, "hint": "请先选择一项软件。"}
    if not key:
        return empty

    label = key
    roots: list[str] = []
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
            label = os.path.basename(p.rstrip("/")) or p
    elif key.startswith("pkg:"):
        return {**empty, "label": key[4:],
                "hint": "软件包的文件由包管理器维护，请改用「卸载选中」或 `清理选中缓存` 中的包缓存项。"}
    elif os.path.isabs(key) and os.path.isdir(key):
        roots = [os.path.abspath(key)]
        label = os.path.basename(key.rstrip("/")) or key

    if not roots:
        return {**empty, "label": label, "hint": "本机未检测到该软件的目录。"}

    scan_roots: list[str] = []
    fallback = False
    if str(scope).lower() == "cache" and _cache_sub_dirs is not None:
        for r in roots:
            scan_roots.extend(_cache_sub_dirs(r))
        scan_roots = _unique_roots(scan_roots)
        if not scan_roots and not key.startswith("pkg:"):
            scan_roots = _unique_roots(roots)
            scope = "all"
            fallback = True
    else:
        scan_roots = _unique_roots(roots)

    if not scan_roots:
        return {**empty, "label": label, "roots": roots,
                "hint": "没有识别到缓存 / 日志子目录。"}

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
        "hint": "该目录本身就是数据 / 缓存目录，已直接列出其中的全部文件。"
                if fallback else "",
    }
