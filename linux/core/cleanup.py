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


# --------------------------------------------------------------------------- #
# 微信缓存 / 聊天图片 / 视频 / 文件
# --------------------------------------------------------------------------- #

_WX_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".tiff"}
_WX_VIDEO_EXT = {".mp4", ".avi", ".mov", ".mkv", ".wmv", ".flv", ".rmvb", ".m4v",
                 ".3gp", ".mpg", ".mpeg"}
_WX_KIND_TEXT = {"cache": "缓存", "image": "照片", "video": "视频", "file": "文件"}
_WX_MAIN_KINDS = ("cache", "image", "video")


def _wx_base_dirs() -> list[str]:
    """探测微信数据根目录（Linux 版微信 / Wine 微信）。"""
    cands = [
        os.path.join(_HOME, "Documents", "WeChat Files"),
        os.path.join(_HOME, "WeChat Files"),
        os.path.join(_HOME, "Documents", "xwechat_files"),
        os.path.join(_HOME, ".config", "wechat"),
        os.path.join(_HOME, ".local", "share", "wechat"),
        os.path.join(_HOME, ".local", "share", "WeChat"),
    ]
    # 全盘搜索发现的微信数据目录：装在非默认位置时也能被识别
    for c in _discovered_paths("wechat"):
        if _wx_looks_like_data(c):
            cands.append(c)
    out, seen = [], set()
    for c in cands:
        ap = os.path.abspath(c)
        if ap in seen or not os.path.isdir(ap):
            continue
        seen.add(ap)
        out.append(ap)
    return out


def _wx_filestorage_dirs() -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def add(p: str) -> None:
        ap = os.path.abspath(p)
        if os.path.isdir(ap) and ap not in seen:
            seen.add(ap)
            out.append(ap)

    for base in _wx_base_dirs():
        add(os.path.join(base, "FileStorage"))
        try:
            for e in os.listdir(base):
                add(os.path.join(base, e, "FileStorage"))
        except Exception:
            continue
    return out


def _wx_kind_dirs() -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {"cache": [], "image": [], "video": [], "file": []}
    for fs in _wx_filestorage_dirs():
        for kind, names in (("cache", ("Cache", "Temp", "CacheData", "Sns",
                                       "MiniProgram", "Applet")),
                            ("image", ("Image", "ImageEx", "HeadImage", "Emoji")),
                            ("video", ("Video",)),
                            ("file", ("File", "MsgAttach", "Fav"))):
            for n in names:
                p = os.path.join(fs, n)
                if os.path.isdir(p):
                    groups[kind].append(p)
    for n in ("Cache", "radium", "XPlugin"):
        p = os.path.join(_HOME, ".config", "wechat", n)
        if os.path.isdir(p):
            groups["cache"].append(p)
    for k in list(groups):
        seen, uniq = set(), []
        for p in groups[k]:
            ap = os.path.abspath(p)
            if ap not in seen:
                seen.add(ap)
                uniq.append(ap)
        groups[k] = uniq
    return groups


def _wx_classify(path_lower: str, ext: str) -> str | None:
    """按路径分段判定微信文件类型（避免子串误判 Temp 目录）。"""
    parts = [p for p in path_lower.replace("\\", "/").split("/") if p]
    if "filestorage" in parts:
        parts = parts[parts.index("filestorage") + 1:]
    segs = set(parts)
    if "msgattach" in segs:
        if ext in _WX_IMAGE_EXT or ext == ".dat":
            return "image"
        if ext in _WX_VIDEO_EXT:
            return "video"
        return "file"
    if segs & {"image", "imageex", "headimage", "emoji"}:
        return "image"
    if "video" in segs:
        return "video"
    if segs & {"cache", "temp", "cachedata", "sns", "miniprogram", "applet",
               "radium", "xplugin", "wechatapp"}:
        return "cache"
    if segs & {"file", "fav"}:
        return "file"
    return None


def _wx_iter_files(max_seconds: float = 25.0):
    """遍历微信文件存储目录，产出 (path, name, size, mtime, kind)。"""
    started = time.time()
    roots = list(_wx_filestorage_dirs())
    for n in ("Cache", "radium", "XPlugin", "CacheData"):
        p = os.path.join(_HOME, ".config", "wechat", n)
        if os.path.isdir(p):
            roots.append(p)
    for root in roots:
        if time.time() - started > max_seconds:
            return
        try:
            walker = os.walk(root, topdown=True, onerror=lambda e: None,
                             followlinks=False)
        except Exception:
            continue
        for dirpath, _dirnames, filenames in walker:
            if time.time() - started > max_seconds:
                return
            for f in filenames:
                full = os.path.join(dirpath, f)
                try:
                    st = os.lstat(full)
                    if st.st_size <= 0:
                        continue
                    ext = os.path.splitext(f)[1].lower()
                    k = _wx_classify(full.lower(), ext)
                    if k is None:
                        continue
                    yield full, f, st.st_size, st.st_mtime, k
                except Exception:
                    continue


def wechat_summary(max_seconds: float = 25.0) -> dict[str, Any]:
    """微信可清理项概览：缓存 / 照片 / 视频 / 其他文件 各自占用。只读，绝不删除。"""
    dirs = _wx_kind_dirs()
    names = {"cache": "微信缓存", "image": "微信照片",
             "video": "微信视频", "file": "其他文件"}
    desc = {
        "cache": "微信运行中产生的图片、视频、朋友圈等缓存，删除不影响聊天记录",
        "image": "聊天中收发与查看过的图片（含加密 .dat），删除后聊天记录中的照片将无法查看",
        "video": "聊天中收发的视频，删除后聊天记录中的视频将无法播放",
        "file": "聊天中收发的文档、压缩包等附件，删除后无法恢复",
    }
    agg = {k: {"size": 0, "count": 0} for k in ("cache", "image", "video", "file")}
    for _p, _n, size, _m, kind in _wx_iter_files(max_seconds=max_seconds):
        agg[kind]["size"] += size
        agg[kind]["count"] += 1

    groups: list[dict[str, Any]] = []
    total = 0
    for kind in ("cache", "image", "video", "file"):
        a = agg[kind]
        total += a["size"]
        groups.append({"kind": kind, "name": names[kind], "desc": desc[kind],
                       "main": kind in _WX_MAIN_KINDS,
                       "size": a["size"], "size_text": human_bytes(a["size"]),
                       "count": a["count"], "paths": dirs.get(kind, [])})
    return {"installed": bool(_wx_filestorage_dirs()),
            "roots": _wx_filestorage_dirs(), "groups": groups,
            "total": total, "total_text": human_bytes(total)}


def scan_wechat_media(kind: str = "all", sort_by: str = "size",
                      order: str = "desc", limit: int = 500,
                      max_seconds: float = 20.0) -> dict[str, Any]:
    started = time.time()
    rows: list[dict[str, Any]] = []
    scanned = 0
    roots: list[str] = []
    for full, f, size, mtime, k in _wx_iter_files(max_seconds=max_seconds):
        scanned += 1
        if kind != "all" and k != kind:
            continue
        try:
            rows.append({
                "path": full, "name": f, "size": size,
                "size_text": human_bytes(size),
                "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)),
                "mtime_ts": mtime,
                "age_days": int((time.time() - mtime) / 86400),
                "kind": k, "kind_text": _WX_KIND_TEXT.get(k, k),
            })
        except Exception:
            continue

    reverse = str(order).lower() != "asc"
    if sort_by == "time":
        rows.sort(key=lambda r: r["mtime_ts"], reverse=reverse)
    else:
        rows.sort(key=lambda r: r["size"], reverse=reverse)
    top = rows[:limit]
    return {"rows": top, "total_found": len(rows), "shown": len(top),
            "scanned": scanned, "sort_by": sort_by,
            "order": "desc" if reverse else "asc",
            "installed": bool(_wx_filestorage_dirs()),
            "elapsed": round(time.time() - started, 1)}


def _build_wechat_targets() -> list[dict[str, Any]]:
    dirs = _wx_kind_dirs()
    spec = [
        ("wechat_cache", "微信缓存", "safe",
         "微信运行中产生的图片、视频、朋友圈等缓存，删除不影响聊天记录"),
        ("wechat_image", "微信照片", "medium",
         "聊天图片目录中的照片（含加密 .dat）。删除后聊天记录里的照片将无法查看，请先备份重要照片"),
        ("wechat_video", "微信视频", "medium",
         "聊天视频目录中的视频。删除后聊天记录里的视频将无法播放，请先备份重要视频"),
        ("wechat_file", "微信聊天文件", "medium",
         "聊天中收发的文档/压缩包等。删除后无法恢复，请确认不再需要"),
    ]
    out = []
    for tid, name, risk, desc in spec:
        kind = tid.split("_", 1)[1]
        paths = dirs.get(kind, [])
        out.append({"id": tid, "name": name, "group": "微信", "risk": risk,
                    "desc": desc, "admin": False, "paths": paths,
                    "available": bool(paths)})
    return out


# --------------------------------------------------------------------------- #
# AI 编程工具（vibe coding / coding agent）缓存
# --------------------------------------------------------------------------- #
# 只清「缓存 / 日志 / 会话历史」，绝不碰账号凭证、用户配置、技能规则与程序本体。

def _agent_spec() -> list[dict[str, Any]]:
    """Vibe Coding / AI 编程工具的可清理目录清单（Linux 路径）。

    只清「缓存 / 日志 / 会话历史 / 遥测」这类可再生数据，
    绝不碰账号凭证、用户配置、技能规则与程序本体。
    """
    def h(*parts: str) -> str:
        return os.path.join(_HOME, *parts)

    def c(*parts: str) -> str:
        return os.path.join(_HOME, ".config", *parts)

    def sh(*parts: str) -> str:
        return os.path.join(_HOME, ".local", "share", *parts)

    ca = os.path.join(_HOME, ".cache")

    raw: list[tuple[str, str, str, str, list[str]]] = [
        # ---- 原本已覆盖的 14 项 ----
        ("agent_claude", "Claude Code", "medium",
         "Claude Code 的会话历史与快照缓存；保留 settings.json 与 skills",
         [h(".claude", "sessions"), h(".claude", "backups"), h(".claude", "ide"),
          h(".claude", "projects"), h(".claude", "shell-snapshots"),
          h(".claude", "statsig"), h(".claude", "file-history"),
          os.path.join(ca, "claude-code")]),
        ("agent_codex", "Codex", "medium",
         "Codex CLI 的会话与临时缓存；保留 auth.json、config.toml、skills、rules",
         [h(".codex", "sessions"), h(".codex", "cache"), h(".codex", "tmp"),
          h(".codex", ".tmp"), h(".codex", "node_repl"),
          h(".codex", "dictation-history")]),
        ("agent_workbuddy", "WorkBuddy / WorkBuddy AI", "medium",
         "WorkBuddy 的日志与缓存；保留程序本体、插件、连接器与配置",
         [h(".workbuddy", "cache"), h(".workbuddy", "logs"), h(".workbuddy", "traces")]),
        ("agent_zcode", "zcode", "medium", "zcode 的工作区与插件工作区缓存",
         [h(".zcode", "workspace"), h(".zcode", "plugin-workspace"), h(".zcode", "v2")]),
        ("agent_doubao", "豆包工作 / MarsCode", "medium",
         "豆包（MarsCode / Trae）本地缓存",
         [h(".doubao"), h(".marscode"), h(".trae"), c("doubao")]),
        ("agent_qoder", "Qoder", "medium", "Qoder 的本地缓存与会话记录", [h(".qoder")]),
        ("agent_cursor", "Cursor 缓存", "safe",
         "Cursor 的渲染/代码缓存与日志，删除后自动重建，不影响配置与插件",
         [c("Cursor", "Cache"), c("Cursor", "CachedData"),
          c("Cursor", "Code Cache"), c("Cursor", "GPUCache"), c("Cursor", "logs")]),
        ("agent_cursor_data", "Cursor 项目与遥测", "medium",
         "Cursor 的项目历史与 AI 使用遥测数据",
         [h(".cursor", "projects"), h(".cursor", "ai-tracking")]),
        ("agent_opencode", "opencode", "medium", "opencode 的本地缓存与会话数据",
         [h(".opencode"), c("opencode"), sh("opencode")]),
        ("agent_github", "GitHub Copilot", "medium",
         "GitHub Copilot / CLI 的日志与会话缓存",
         [h(".copilot", "logs"), h(".copilot", "ide"), c("github-copilot")]),
        ("agent_kimi", "Kimi Code", "medium", "Kimi Code 的本地缓存与会话记录",
         [h(".kimi"), h(".kimi-code")]),
        ("agent_dsh", "DSH", "medium",
         "DSH 的存储缓存；保留 .credentials.yaml 与 settings.yaml",
         [h(".dsh", "storages")]),
        ("agent_cline", "Cline", "medium", "Cline 插件的历史任务与缓存",
         [c("Code", "User", "globalStorage", "saoudrizwan.claude-dev"),
          h(".vscode", "data", "User", "globalStorage", "saoudrizwan.claude-dev")]),
        ("agent_antigravity", "Antigravity", "medium", "Antigravity 的本地缓存与日志",
         [h(".antigravity"), c("antigravity")]),

        # ---- 本轮新增的常用 coding 工具 ----
        ("agent_windsurf", "Windsurf（Codeium）", "safe",
         "Windsurf / Codeium 的渲染缓存与日志；保留插件与账号配置",
         [h(".windsurf", "cache"), h(".codeium", "cache"),
          c("Windsurf", "Cache"), c("Windsurf", "Code Cache"),
          c("Windsurf", "GPUCache"), c("Windsurf", "logs"),
          os.path.join(ca, "Windsurf"), os.path.join(ca, "codeium")]),
        ("agent_trae", "Trae（字节 AI IDE）", "safe",
         "Trae 的缓存与日志；保留插件与工程配置",
         [h(".trae", "cache"), h(".trae", "logs"),
          c("Trae", "Cache"), c("Trae", "logs")]),
        ("agent_aider", "Aider", "medium",
         "Aider 的缓存与分析数据；保留 .aider.conf.yml 等配置",
         [h(".aider", "cache"), h(".aider", "analytics"), c("aider", "cache")]),
        ("agent_continue", "Continue", "medium",
         "Continue 的本地索引缓存与插件历史；保留 config.json 与模型配置",
         [h(".continue", "index"), h(".continue", "cache"), h(".continue", "logs"),
          c("Code", "User", "globalStorage", "continue.continue")]),
        ("agent_roo", "Roo Code / Roo Cline", "medium",
         "Roo Code 的历史任务与缓存；保留 API 密钥等配置",
         [h(".roo", "cache"),
          c("Code", "User", "globalStorage", "roovet.roo-cline")]),
        ("agent_kilo", "Kilo Code", "medium", "Kilo Code 的历史任务与缓存",
         [h(".kilo", "cache"),
          c("Code", "User", "globalStorage", "kilocode.kilo-code")]),
        ("agent_gemini", "Gemini CLI", "medium",
         "Google Gemini CLI 的临时文件与会话缓存；保留 settings.json 与凭据",
         [h(".gemini", "tmp"), h(".gemini", "cache"), h(".gemini", "logs")]),
        ("agent_amazonq", "Amazon Q Developer", "medium",
         "Amazon Q 的本地缓存与日志；保留登录凭据",
         [h(".amazonq", "cache"), h(".amazonq", "logs"), c("amazon-q", "cache")]),
        ("agent_augment", "Augment Code", "medium", "Augment 的本地索引缓存与日志",
         [h(".augment", "cache"), h(".augment", "logs"), c("Augment", "Cache")]),
        ("agent_cody", "Sourcegraph Cody", "medium",
         "Cody 的插件缓存与历史；保留登录状态与配置",
         [h(".cody", "cache"), h(".cody", "logs"),
          c("Code", "User", "globalStorage", "sourcegraph.cody-ai")]),
        ("agent_jetbrains", "JetBrains AI / Junie", "medium",
         "JetBrains 系 IDE（AI Assistant、Junie）的缓存与日志；保留插件与授权",
         [h(".junie", "cache"), h(".junie", "logs"), c("JetBrains", "logs")]),
        ("agent_vscode", "VS Code 缓存", "safe",
         "VS Code（AI 插件宿主）的渲染/代码缓存与旧日志；保留已装插件与设置",
         [c("Code", "Cache"), c("Code", "Code Cache"), c("Code", "GPUCache"),
          c("Code", "Crashpad"), c("Code", "logs"),
          h(".vscode", "data", "Cache"), h(".vscode", "data", "logs")]),
        ("agent_warp", "Warp 终端", "medium", "Warp 终端（内置 AI）的缓存与日志",
         [h(".warp", "cache"), h(".warp", "logs"), c("warp", "Cache")]),
        ("agent_supermaven", "Supermaven", "medium",
         "Supermaven 补全服务的本地缓存与日志",
         [h(".supermaven", "cache"), h(".supermaven", "logs")]),
        ("agent_models", "AI 模型与依赖缓存", "safe",
         "AI 编程工具共用的 pip / npm / uv / HuggingFace 等依赖缓存，删除后按需重新下载",
         [os.path.join(ca, "pip"), os.path.join(ca, "huggingface"),
          os.path.join(ca, "uv"), os.path.join(ca, "yarn"),
          h(".npm", "_cacache"), os.path.join(ca, "ms-playwright")]),
    ]

    out: list[dict[str, Any]] = []
    for tid, name, risk, desc, paths in raw:
        exist: list[str] = []
        seen: set[str] = set()
        for p in paths:
            if not p:
                continue
            ap = os.path.abspath(p)
            if ap in seen or not os.path.isdir(ap):
                continue
            seen.add(ap)
            exist.append(ap)
        # 全盘搜索找到的、装在非默认位置的同类目录：只接管其中的缓存子目录
        for d in _discovered_paths(tid):
            for sub in _cache_sub_dirs(d):
                ap = os.path.abspath(sub)
                if ap not in seen:
                    seen.add(ap)
                    exist.append(ap)
        out.append({"id": tid, "name": name, "risk": risk, "desc": desc,
                    "paths": exist, "available": bool(exist)})
    return out


def _build_agent_targets() -> list[dict[str, Any]]:
    """把 Vibe Coding 工具清单转成磁盘清理项（group = AI 编程工具）。"""
    return [{"id": a["id"], "name": a["name"], "group": "AI 编程工具",
             "risk": a["risk"], "desc": a["desc"], "admin": False,
             "paths": a["paths"], "available": a["available"]}
            for a in _agent_spec()]


def agent_summary(max_seconds: float = 20.0) -> dict[str, Any]:
    """Vibe Coding 工具占用概览：每个工具一张卡片。只读，绝不删除。"""
    started = time.time()
    items: list[dict[str, Any]] = []
    for a in _agent_spec():
        size = count = 0
        if a["available"]:
            for p in a["paths"]:
                if time.time() - started > max_seconds:
                    break
                info = scan_dir(p)
                size += info["size"]
                count += info["count"]
        items.append({"id": a["id"], "name": a["name"], "risk": a["risk"],
                      "desc": a["desc"], "size": size,
                      "size_text": human_bytes(size), "count": count,
                      "available": a["available"], "paths": a["paths"]})
    items.sort(key=lambda x: x["size"], reverse=True)
    used = [i for i in items if i["available"]]
    return {"items": items, "total": sum(i["size"] for i in items),
            "total_text": human_bytes(sum(i["size"] for i in items)),
            "detected": len(used), "detected_text": f"检测到 {len(used)} 款工具",
            "elapsed": round(time.time() - started, 1)}


def scan_agent_files(tool_id: str = "", sort_by: str = "size",
                     order: str = "desc", limit: int = 500,
                     max_seconds: float = 20.0) -> dict[str, Any]:
    """列出某个（或多个）Vibe Coding 工具缓存下的文件，支持按大小 / 时间排序。只读。"""
    started = time.time()
    spec = {a["id"]: a for a in _agent_spec()}
    if tool_id and tool_id in spec:
        picked = [spec[tool_id]]
    elif tool_id:
        picked = [a for a in _agent_spec() if a["id"] == tool_id]
    else:
        picked = [a for a in _agent_spec() if a["available"]]

    roots: list[str] = []
    tool_name = ""
    for a in picked:
        roots.extend(a["paths"])
        tool_name = a["name"]
    roots = _dedup_roots(roots)
    if len(picked) > 1:
        tool_name = f"{len(picked)} 款工具"

    rows: list[dict[str, Any]] = []
    scanned = 0
    for root in roots:
        if time.time() - started > max_seconds:
            break
        try:
            walker = os.walk(root, topdown=True, onerror=lambda e: None,
                             followlinks=False)
        except Exception:
            continue
        for dirpath, _dirnames, filenames in walker:
            if time.time() - started > max_seconds:
                break
            for f in filenames:
                scanned += 1
                full = os.path.join(dirpath, f)
                try:
                    st = os.lstat(full)
                    if st.st_size <= 0:
                        continue
                    rows.append({
                        "path": full, "name": f, "size": st.st_size,
                        "size_text": human_bytes(st.st_size),
                        "mtime": time.strftime("%Y-%m-%d %H:%M",
                                               time.localtime(st.st_mtime)),
                        "mtime_ts": st.st_mtime,
                        "age_days": int((time.time() - st.st_mtime) / 86400),
                        "tool": tool_name,
                    })
                except Exception:
                    continue

    reverse = str(order).lower() != "asc"
    if sort_by == "time":
        rows.sort(key=lambda r: r["mtime_ts"], reverse=reverse)
    else:
        rows.sort(key=lambda r: r["size"], reverse=reverse)
    top = rows[:limit]
    return {"rows": top, "total_found": len(rows), "shown": len(top),
            "scanned": scanned, "tool": tool_id, "tool_name": tool_name,
            "sort_by": sort_by, "order": "desc" if reverse else "asc",
            "elapsed": round(time.time() - started, 1)}


# --------------------------------------------------------------------------- #
# 全盘软件目录搜索
# --------------------------------------------------------------------------- #
# 目的：不依赖固定默认路径，自动在本机所有分区里找出各类软件的安装 / 数据目录，
# 尤其是把「装在非默认位置」的 AI 编程工具与微信找出来，回填给对应清理能力：
#   · 命中 agent_*  → 接管该目录下的缓存子目录，加入 Vibe Coding 清理
#   · 命中 wechat   → 加入微信数据根目录，微信清理即可识别
#
# 只读扫描，绝不删除；删除统一走 delete_files，受系统保护路径校验。

# 全盘搜索发现的目录（key: 签名 id，value: 绝对路径集合）
_DISCOVERED: dict[str, set[str]] = {}


def _discovered_paths(key: str) -> list[str]:
    return sorted(_DISCOVERED.get(key, set()))


def _remember_discovered(sig_id: str, path: str) -> None:
    ap = os.path.abspath(path)
    _DISCOVERED.setdefault(sig_id, set()).add(ap)


# 缓存 / 日志类子目录名：全盘搜到软件目录后，默认只列出这些子目录里的文件，
# 避免一上来就把程序本体列出来供人误删。
_CACHE_SUB_NAMES = {
    "cache", "cache2", "cachedata", "code cache", "gpucache", "gpu cache",
    "crashpad", "crashdumps", "crashes", "logs", "log", "temp", "tmp",
    "cachestorage", "service worker", "videodecodestats", "blob_storage",
    "session storage", "local storage", "indexeddb", "webstore",
    "cachedprofiles", "dawncache", "dawnwebgpu", "shadercache",
    "gpu-code-cache", "shared_dictionary", "local state",
}

# 跳过这些目录，既提速也避免进入系统 / 依赖黑洞
_SOFT_SKIP = {
    "windows", "$recycle.bin", "system volume information", "recovery",
    "perflogs", "winsxs", "node_modules", ".git", ".svn", ".hg",
    "__pycache__", "site-packages", "dist-info", "venv", ".venv", "env",
    ".gradle", ".m2", ".nuget", "appdata", "system32", "syswow64",
    "drivers", "servicing", "boot", "efi", ".cache", "library", "contents",
    "resources", "locales", "resources.pak",
}

# 目录名（小写）→ 软件签名。命中的目录会被记录，并不再继续深入。
_SOFT_SIG_RAW: list[tuple[str, str, str, str, tuple[str, ...]]] = [
    # ---------- AI 编程 / coding agent（与 Vibe Coding 清理联动） ----------
    ("agent_claude", "Claude Code", "AI 编程", "medium",
     (".claude", "claude", "claude-code", "claude code")),
    ("agent_codex", "Codex", "AI 编程", "medium",
     (".codex", "codex")),
    ("agent_workbuddy", "WorkBuddy / WorkBuddy AI", "AI 编程", "medium",
     (".workbuddy", "workbuddy")),
    ("agent_zcode", "zcode", "AI 编程", "medium",
     (".zcode", "zcode")),
    ("agent_doubao", "豆包工作 / MarsCode", "AI 编程", "medium",
     (".doubao", "doubao", ".marscode", "marscode")),
    ("agent_qoder", "Qoder", "AI 编程", "medium",
     (".qoder", "qoder")),
    ("agent_cursor", "Cursor", "AI 编程", "medium",
     (".cursor", "cursor")),
    ("agent_opencode", "opencode", "AI 编程", "medium",
     (".opencode", "opencode")),
    ("agent_github", "GitHub Copilot", "AI 编程", "medium",
     (".copilot", "copilot", "github copilot")),
    ("agent_kimi", "Kimi Code", "AI 编程", "medium",
     (".kimi", "kimi", "kimi-code", "kimi code")),
    ("agent_dsh", "DSH", "AI 编程", "medium",
     (".dsh", "dsh")),
    ("agent_cline", "Cline", "AI 编程", "medium",
     ("cline", ".cline")),
    ("agent_antigravity", "Antigravity", "AI 编程", "medium",
     (".antigravity", "antigravity")),
    ("agent_windsurf", "Windsurf（Codeium）", "AI 编程", "medium",
     (".windsurf", "windsurf", ".codeium", "codeium")),
    ("agent_trae", "Trae", "AI 编程", "medium",
     (".trae", "trae")),
    ("agent_aider", "Aider", "AI 编程", "medium",
     (".aider", "aider")),
    ("agent_continue", "Continue", "AI 编程", "medium",
     (".continue", "continue")),
    ("agent_roo", "Roo Code", "AI 编程", "medium",
     (".roo", "roo-code", "roo cline")),
    ("agent_kilo", "Kilo Code", "AI 编程", "medium",
     (".kilo", "kilo-code")),
    ("agent_gemini", "Gemini CLI", "AI 编程", "medium",
     (".gemini", "gemini", "gemini-cli")),
    ("agent_amazonq", "Amazon Q Developer", "AI 编程", "medium",
     (".amazonq", "amazon q", "amazonq")),
    ("agent_augment", "Augment Code", "AI 编程", "medium",
     (".augment", "augment")),
    ("agent_cody", "Sourcegraph Cody", "AI 编程", "medium",
     (".cody", "cody")),
    ("agent_jetbrains", "JetBrains AI / Junie", "AI 编程", "medium",
     (".junie", "junie")),
    ("agent_vscode", "VS Code", "AI 编程", "safe",
     ("microsoft vs code", "vscode", "vs code", "code - insiders")),
    ("agent_warp", "Warp 终端", "AI 编程", "medium",
     (".warp", "warp")),
    ("agent_supermaven", "Supermaven", "AI 编程", "medium",
     (".supermaven", "supermaven")),
    ("agent_ollama", "Ollama 本地大模型", "AI 编程", "medium",
     (".ollama", "ollama")),
    ("agent_lmstudio", "LM Studio", "AI 编程", "medium",
     (".lmstudio", "lm studio", "lm-studio")),
    ("agent_comfyui", "ComfyUI / Stable Diffusion", "AI 编程", "medium",
     ("comfyui", "stable-diffusion-webui", "stable diffusion")),
    ("agent_chatgpt", "ChatGPT 桌面版", "AI 编程", "medium",
     ("chatgpt",)),

    # ---------- 微信 / 企业微信 ----------
    ("wechat", "微信", "微信 / 通讯", "medium",
     ("wechat files", "xwechat_files", "wechat", "weixin", "wechatwin")),
    ("wxwork", "企业微信", "微信 / 通讯", "medium",
     ("wxwork", "企业微信")),
    ("qqnt", "QQ / TIM", "微信 / 通讯", "medium",
     ("qq", "tim", "qqnt")),
    ("dingtalk", "钉钉", "微信 / 通讯", "medium",
     ("dingtalk", "钉钉")),
    ("feishu", "飞书 / Lark", "微信 / 通讯", "medium",
     ("feishu", "lark", "飞书")),
    ("wemeet", "腾讯会议", "微信 / 通讯", "medium",
     ("tencent meeting", "wemeet", "腾讯会议")),
    ("telegram", "Telegram", "微信 / 通讯", "medium",
     ("telegram desktop", "telegram")),
    ("discord", "Discord", "微信 / 通讯", "medium",
     ("discord",)),
    ("slack", "Slack", "微信 / 通讯", "medium",
     ("slack",)),
    ("zoom", "Zoom", "微信 / 通讯", "medium",
     ("zoom",)),
    ("teams", "Microsoft Teams", "微信 / 通讯", "medium",
     ("microsoft teams", "teams")),

    # ---------- 浏览器 ----------
    ("chrome", "Google Chrome", "浏览器", "safe",
     ("chrome", "google chrome", "chromium")),
    ("edge", "Microsoft Edge", "浏览器", "safe",
     ("microsoft edge", "edge", "edge core")),
    ("firefox", "Mozilla Firefox", "浏览器", "safe",
     ("firefox", "mozilla firefox")),
    ("brave", "Brave", "浏览器", "safe", ("brave-browser", "brave")),
    ("opera", "Opera", "浏览器", "safe", ("opera",)),
    ("browser360", "360 浏览器", "浏览器", "safe",
     ("360chrome", "360se", "360browser")),
    ("qqbrowser", "QQ 浏览器", "浏览器", "safe", ("qqbrowser",)),

    # ---------- 开发 / 运行时 ----------
    ("nodejs", "Node.js", "开发 / 运行时", "medium", ("nodejs", "node")),
    ("python", "Python", "开发 / 运行时", "medium",
     ("python", "python3", "python software foundation", "anaconda3",
      "miniconda3", "conda")),
    ("java", "Java / JDK", "开发 / 运行时", "medium",
     ("java", "jdk", "jre", "oracle java")),
    ("golang", "Go", "开发 / 运行时", "medium", ("go",)),
    ("rust", "Rust / Cargo", "开发 / 运行时", "medium", (".cargo", "rustup")),
    ("git", "Git", "开发 / 运行时", "medium", ("git",)),
    ("docker", "Docker", "开发 / 运行时", "medium",
     ("docker", "docker desktop")),
    ("vmware", "VMware", "开发 / 运行时", "medium",
     ("vmware", "vmware workstation")),
    ("virtualbox", "VirtualBox", "开发 / 运行时", "medium", ("virtualbox",)),
    ("android", "Android SDK", "开发 / 运行时", "medium",
     ("android", "android sdk", ".android")),
    ("unity", "Unity", "开发 / 运行时", "medium", ("unity", "unity hub")),
    ("jetbrains", "JetBrains 系列 IDE", "开发 / 运行时", "medium",
     ("jetbrains", ".jetbrains", "intellij idea", "pycharm", "webstorm",
      "goland", "datagrip", "rider")),
    ("maven", "Maven / Gradle 依赖库", "开发 / 运行时", "safe",
     (".gradle", ".m2", ".nuget")),

    # ---------- 设计 / 多媒体 ----------
    ("adobe", "Adobe 系列", "设计 / 多媒体", "medium",
     ("adobe", "adobe acrobat", "adobe photoshop", "adobe premiere pro",
      "adobe after effects", "adobe illustrator")),
    ("jianying", "剪映 / CapCut", "设计 / 多媒体", "medium",
     ("jianyingpro", "capcut", "剪映")),
    ("obs", "OBS Studio", "设计 / 多媒体", "medium", ("obs-studio", "obs")),
    ("blender", "Blender", "设计 / 多媒体", "medium", ("blender",)),
    ("autocad", "AutoCAD", "设计 / 多媒体", "medium", ("autocad",)),
    ("sketchup", "SketchUp", "设计 / 多媒体", "medium", ("sketchup",)),
    ("figma", "Figma", "设计 / 多媒体", "medium", ("figma",)),

    # ---------- 办公 / 笔记 ----------
    ("office", "Microsoft Office", "办公 / 笔记", "medium",
     ("microsoft office", "office", "office16")),
    ("wps", "WPS Office", "办公 / 笔记", "medium",
     ("wps office", "kingsoft", "wps software", "wpsoffice")),
    ("notion", "Notion", "办公 / 笔记", "medium", ("notion",)),
    ("obsidian", "Obsidian", "办公 / 笔记", "medium", ("obsidian",)),
    ("typora", "Typora", "办公 / 笔记", "medium", ("typora",)),

    # ---------- 网盘 / 下载 ----------
    ("baidunetdisk", "百度网盘", "网盘 / 下载", "medium",
     ("baidunetdisk", "baiduyunguanjia", "百度网盘")),
    ("aliyunpan", "阿里云盘", "网盘 / 下载", "medium",
     ("aliyunpan", "阿里云盘", "aDrive")),
    ("thunder", "迅雷", "网盘 / 下载", "medium", ("thunder", "迅雷", "xunlei")),
    ("quark", "夸克网盘", "网盘 / 下载", "medium", ("quark", "夸克")),
    ("onedrive", "OneDrive", "网盘 / 下载", "medium", ("onedrive",)),
    ("dropbox", "Dropbox", "网盘 / 下载", "medium", ("dropbox",)),

    # ---------- 影音 ----------
    ("neteasemusic", "网易云音乐", "影音", "medium",
     ("netease cloud music", "cloudmusic", "网易云音乐")),
    ("qqmusic", "QQ 音乐", "影音", "medium", ("qqmusic", "qq音乐")),
    ("spotify", "Spotify", "影音", "medium", ("spotify",)),
    ("potplayer", "PotPlayer", "影音", "medium", ("potplayer",)),
    ("vlc", "VLC", "影音", "medium", ("videolan", "vlc")),
    ("iqiyi", "爱奇艺", "影音", "medium", ("iqiyi", "爱奇艺")),
    ("bilibili", "哔哩哔哩", "影音", "medium", ("bilibili", "哔哩哔哩")),

    # ---------- 游戏平台 ----------
    ("steam", "Steam", "游戏平台", "medium", ("steam", "steamapps")),
    ("epic", "Epic Games", "游戏平台", "medium", ("epic games", "unreal engine")),
    ("wegame", "WeGame", "游戏平台", "medium", ("wegame",)),
    ("riot", "Riot Games", "游戏平台", "medium", ("riot games",)),
    ("ubisoft", "Ubisoft Connect", "游戏平台", "medium", ("ubisoft",)),
    ("battlenet", "Battle.net", "游戏平台", "medium", ("battle.net",)),

    # ---------- 系统工具 ----------
    ("safe360", "360 安全卫士", "系统工具", "medium", ("360safe", "360安全卫士")),
    ("huorong", "火绒安全", "系统工具", "medium", ("huorong", "火绒")),
    ("qqpcmgr", "腾讯电脑管家", "系统工具", "medium", ("qqpcmgr",)),
    ("zip7", "7-Zip", "系统工具", "medium", ("7-zip",)),
    ("everything", "Everything", "系统工具", "medium", ("everything",)),
    ("notepadpp", "Notepad++", "系统工具", "medium", ("notepad++",)),
    ("postman", "Postman", "系统工具", "medium", ("postman",)),
    ("navicat", "Navicat", "系统工具", "medium", ("navicat",)),
    ("nvidia", "NVIDIA 驱动与工具", "系统工具", "medium",
     ("nvidia", "nvidia corporation", "nvidia gpu computing toolkit")),
]


def _software_signatures() -> list[dict[str, Any]]:
    """把签名表展开成结构化列表。"""
    out: list[dict[str, Any]] = []
    for sid, name, category, risk, dirs in _SOFT_SIG_RAW:
        out.append({
            "id": sid,
            "name": name,
            "category": category,
            "risk": risk,
            "dirs": tuple(d.lower() for d in dirs),
        })
    return out


def _software_scan_roots(drive: str = "") -> list[tuple[str, int]]:
    """Linux 全盘搜索起点：(根目录, 相对深度上限)。

    跳过 /proc、/sys 这类虚拟文件系统；用户级配置目录层级较深，单独作为起点补充。
    """
    skip = ("/proc", "/sys", "/dev", "/run", "/snap", "/boot", "/tmp")
    roots: list[tuple[str, int]] = []
    seen: set[str] = set()

    def add(p: str, depth: int) -> None:
        if not p:
            return
        ap = os.path.abspath(p)
        if not os.path.isdir(ap):
            return
        if any(ap == s or ap.startswith(s + os.sep) for s in skip):
            return
        key = _norm(ap)
        if key in seen:
            return
        if drive and not ap.startswith(drive):
            return
        seen.add(key)
        roots.append((ap, depth))

    for row in list_drives():
        add(row["mountpoint"], 3)
    add(_HOME, 3)
    add(os.path.join(_HOME, ".config"), 2)
    add(os.path.join(_HOME, ".local", "share"), 2)
    add(os.path.join(_HOME, ".cache"), 2)
    add("/opt", 2)
    add("/usr/local", 2)
    return roots


def _dedup_roots(roots: list[str]) -> list[str]:
    """去掉互相包含的目录，避免嵌套目录被重复遍历、文件被重复计数。"""
    norm = [os.path.normcase(os.path.abspath(r)).rstrip(os.sep) + os.sep
            for r in roots]
    out: list[str] = []
    for i, pre in enumerate(norm):
        if any(j != i and pre.startswith(other) for j, other in enumerate(norm)):
            continue
        out.append(roots[i])
    return out


def _cache_sub_dirs(root: str, max_depth: int = 4,
                    max_items: int = 200) -> list[str]:
    """软件目录下真实存在的缓存 / 日志类子目录。

    只向下看 max_depth 层：太浅会漏（Cursor 的缓存藏在多层子目录下），
    太深会把程序本体的资源目录也卷进来。跳过 Junction 与依赖黑洞。
    """
    out: list[str] = []
    root_abs = os.path.abspath(root)
    base_depth = root_abs.rstrip(os.sep).count(os.sep)
    skip_extra = {"resources", "bin", "app", "lib", "modules",
                  "extensions", "plugins", "locales", "swiftshader"}
    try:
        walker = os.walk(root_abs, topdown=True,
                         onerror=lambda e: None, followlinks=False)
    except Exception:
        return out
    for dirpath, dirnames, _files in walker:
        if len(out) >= max_items:
            return out
        depth = dirpath.rstrip(os.sep).count(os.sep) - base_depth
        keep: list[str] = []
        for d in dirnames:
            dl = d.lower()
            if dl in _SOFT_SKIP or dl in skip_extra or dl.startswith("app-"):
                continue
            full = os.path.join(dirpath, d)
            try:
                st = os.lstat(full)
                if hasattr(st, "st_reparse_tag") and st.st_reparse_tag:
                    continue
            except Exception:
                continue
            if dl in _CACHE_SUB_NAMES:
                out.append(full)
                continue
            if depth + 1 < max_depth:
                keep.append(d)
        dirnames[:] = keep
    return out


def scan_software_dirs(drive: str = "", query: str = "",
                       category: str = "all", max_seconds: float = 25.0,
                       limit: int = 400) -> dict[str, Any]:
    """全盘搜索已安装软件的目录。只读，绝不删除。

    · drive     指定分区（如 C:），留空表示全盘
    · query     关键词，匹配软件名 / 分类 / 路径
    · category  分类过滤，'all' 表示不过滤
    命中的 AI 编程工具目录会回填给 Vibe Coding 清理，微信目录回填给微信清理，
    因此「装在非默认位置」的软件也能被后续清理识别到。
    """
    started = time.time()
    sigs = _software_signatures()
    by_dir: dict[str, dict[str, Any]] = {}
    for s in sigs:
        for d in s["dirs"]:
            by_dir.setdefault(d, s)

    found: list[dict[str, Any]] = []
    scanned_dirs = 0
    timed_out = False

    walk_budget = max(5.0, min(max_seconds * 0.5, 15.0))
    for root, max_depth in _software_scan_roots(drive):
        if time.time() - started > walk_budget:
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
            for dirpath, dirnames, _files in walker:
                if time.time() - started > walk_budget:
                    timed_out = True
                    break
                depth = dirpath.rstrip(os.sep).count(os.sep) - base_depth
                keep: list[str] = []
                for d in dirnames:
                    scanned_dirs += 1
                    dl = d.lower()
                    if dl in _SOFT_SKIP:
                        continue
                    full = os.path.join(dirpath, d)
                    # 跳过 Junction / 符号链接：否则 Documents and Settings、
                    # Application Data 这类兼容链接会让同一目录重复出现
                    try:
                        st = os.lstat(full)
                        if hasattr(st, "st_reparse_tag") and st.st_reparse_tag:
                            continue
                    except Exception:
                        continue
                    hit = by_dir.get(dl)
                    if hit is not None:
                        # 命中：记录并停止深入（避免扫进软件内部海量目录）
                        _remember_discovered(hit["id"], full)
                        found.append({
                            "id": hit["id"],
                            "name": hit["name"],
                            "category": hit["category"],
                            "risk": hit["risk"],
                            "path": os.path.abspath(full),
                            "size": -1,
                            "size_text": "—",
                            "count": 0,
                        })
                        continue
                    if depth + 1 >= max_depth:
                        continue
                    keep.append(d)
                dirnames[:] = keep
        except Exception:
            continue

    # 去重（同一路径可能被多个起点扫到）
    uniq: list[dict[str, Any]] = []
    seen_path: set[str] = set()
    for f in found:
        key = _norm(f["path"])
        if key in seen_path:
            continue
        seen_path.add(key)
        # 丢弃空壳目录：例如 %TEMP%\\claude 这类残留空目录，列出来只是噪音
        try:
            if not os.listdir(f["path"]):
                continue
        except Exception:
            continue
        uniq.append(f)

    # 过滤
    q = (query or "").strip().lower()
    items: list[dict[str, Any]] = []
    for f in uniq:
        if category and category != "all" and f["category"] != category:
            continue
        if q and q not in f["name"].lower() and q not in f["path"].lower()                 and q not in f["category"].lower():
            continue
        items.append(f)

    # 统计占用：AI 编程 / 微信优先，受时间预算限制；超时项显示「—」
    prio = {"AI 编程": 0, "微信 / 通讯": 1}
    items.sort(key=lambda x: (prio.get(x["category"], 2), x["name"].lower()))
    measured = 0
    for f in items:
        if time.time() - started > max_seconds:
            timed_out = True
            break
        info = scan_dir(f["path"], max_depth=8)
        f["size"] = info["size"]
        f["count"] = info["count"]
        f["size_text"] = info["size_text"]
        measured += 1
    items.sort(key=lambda x: (x["size"] if x["size"] >= 0 else -1), reverse=True)
    items = items[:max(1, min(int(limit or 400), 2000))]

    total = sum(i["size"] for i in items if i["size"] > 0)
    return {
        "items": items,
        "total": total,
        "total_text": human_bytes(total),
        "count": len(items),
        "measured": measured,
        "categories": sorted({i["category"] for i in uniq}),
        "drives": [r[0] for r in _software_scan_roots(drive)],
        "scanned_dirs": scanned_dirs,
        "elapsed": round(time.time() - started, 1),
        "timed_out": timed_out,
        "query": query or "",
        "category": category or "all",
    }


def scan_software_files(target: str = "", scope: str = "cache",
                        sort_by: str = "size", order: str = "desc",
                        limit: int = 500, max_seconds: float = 20.0
                        ) -> dict[str, Any]:
    """列出某个软件目录（或某类软件）下的文件，支持按大小 / 时间排序。只读。

    target 可以是绝对路径，也可以是签名 id（如 agent_cursor / wechat）。
    scope='cache' 只列缓存 / 日志子目录；'all' 列出该目录下全部文件。
    """
    started = time.time()
    roots: list[str] = []
    label = target or ""
    if target and os.path.isabs(target) and os.path.isdir(target):
        roots = [os.path.abspath(target)]
        label = os.path.basename(target.rstrip(os.sep)) or target
    elif target:
        sig = {s["id"]: s for s in _software_signatures()}
        s = sig.get(target)
        if s is not None:
            label = s["name"]
            roots = _discovered_paths(target)
    if not roots:
        return {"rows": [], "total_found": 0, "shown": 0, "dir_size": 0,
                "dir_size_text": "0 B", "dir_count": 0, "roots": [],
                "target": target, "label": label, "scope": scope,
                "timed_out": False, "elapsed": 0.0,
                "hint": "未找到该软件的目录，请先执行一次全盘搜索。"}

    scan_roots: list[str] = []
    if str(scope).lower() == "cache":
        for r in roots:
            subs = _cache_sub_dirs(r)
            if subs:
                scan_roots.extend(subs)
        scan_roots = _dedup_roots(scan_roots)
        if not scan_roots:
            return {"rows": [], "total_found": 0, "shown": 0, "dir_size": 0,
                    "dir_size_text": "0 B", "dir_count": 0, "roots": roots,
                    "target": target, "label": label, "scope": "cache",
                    "timed_out": False, "elapsed": round(time.time() - started, 1),
                    "hint": "该目录下没有识别到缓存 / 日志子目录，可切换到「全部文件」查看。"}
    else:
        scan_roots = _dedup_roots(list(roots))

    rows: list[dict[str, Any]] = []
    timed_out = False
    dir_size = 0
    for root in scan_roots:
        if time.time() - started > max_seconds:
            timed_out = True
            break
        try:
            walker = os.walk(root, topdown=True,
                             onerror=lambda e: None, followlinks=False)
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
                    dir_size += st.st_size
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
    top = rows[:max(1, min(int(limit or 500), 5000))]
    return {
        "rows": top,
        "total_found": len(rows),
        "shown": len(top),
        "dir_size": dir_size,
        "dir_size_text": human_bytes(dir_size),
        "dir_count": len(rows),
        "roots": roots,
        "target": target,
        "label": label,
        "scope": "cache" if str(scope).lower() == "cache" else "all",
        "sort_by": "time" if str(sort_by).lower() == "time" else "size",
        "order": "desc" if reverse else "asc",
        "timed_out": timed_out,
        "elapsed": round(time.time() - started, 1),
        "hint": "",
    }


# Linux 常见软件目录（补充）
_SOFT_SIG_RAW.extend([
    ("linux_chrome", "Google Chrome", "浏览器", "safe",
     ("google-chrome", "google-chrome-stable", "chromium")),
    ("linux_edge", "Microsoft Edge", "浏览器", "safe",
     ("microsoft-edge", "microsoft-edge-stable", "microsoft-edge-dev")),
    ("linux_firefox", "Mozilla Firefox", "浏览器", "safe",
     ("mozilla", "firefox", ".mozilla")),
    ("linux_code", "VS Code / Codium", "开发 / 运行时", "safe",
     ("code", "vscodium", ".vscode", ".vscode-server", "code - insiders")),
    ("linux_snap", "Snap 应用", "系统工具", "medium", ("snap",)),
    ("linux_flatpak", "Flatpak 应用", "系统工具", "medium", ("flatpak", ".var")),
    ("linux_steam", "Steam", "游戏平台", "medium",
     ("steam", ".steam", "steamapps")),
    ("linux_wine", "Wine / Windows 应用", "系统工具", "medium", (".wine", "wine")),
    ("linux_ollama", "Ollama 本地大模型", "AI 编程", "medium",
     ("ollama", ".ollama")),
    ("linux_docker", "Docker", "开发 / 运行时", "medium", ("docker", ".docker")),
    ("linux_jetbrains", "JetBrains 系列 IDE", "开发 / 运行时", "medium",
     (".jetbrains", "jetbrains-toolbox")),
])


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
    targets.extend(_build_wechat_targets())
    targets.extend(_build_agent_targets())

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
