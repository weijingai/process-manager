# -*- coding: utf-8 -*-
"""Windows进程管理工具（潍鲸 - weijing.co） - 本地服务入口

启动后在 http://127.0.0.1:<port> 提供可视化管理界面：
  GET  /api/scan              扫描服务 / 进程 / 端口
  GET  /api/monitor           CPU / 内存 / 磁盘 / 网络实时快照与历史序列
  GET  /api/cleanup/targets   磁盘可清理项清单（只读预演）
  GET  /api/cleanup/files     大文件扫描（只读）
  POST /api/cleanup/memory    内存清理（裁剪工作集 / 清文件缓存 / 清备用列表）
  POST /api/cleanup/disk      磁盘清理（支持 dry_run）
  POST /api/cleanup/delete-files   删除指定文件（默认送入回收站）
  POST /api/cleanup/empty-recycle-bin  清空回收站
  POST /api/service/action    启动 / 停止 / 重启服务
  POST /api/service/config    修改服务启动类型
  POST /api/process/kill      结束进程（可选进程树）
  POST /api/elevate           申请管理员权限重新启动
"""

from __future__ import annotations

import argparse
import ctypes
import gzip
import json
import os
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# 打包成 exe 后，源码与资源会被解包到 sys._MEIPASS 临时目录，这里统一处理两种运行方式
if getattr(sys, "frozen", False):  # PyInstaller 打包后的运行环境
    BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    APP_DIR = BASE_DIR

WEB_DIR = os.path.join(BASE_DIR, "web")
if not os.path.isdir(WEB_DIR):  # 单文件模式下也允许从 exe 同级目录读取
    WEB_DIR = os.path.join(APP_DIR, "web")
sys.path.insert(0, BASE_DIR)

from core import cleanup, control, monitor, scanner, textutil  # noqa: E402
from core import APP_VERSION, app_version, software  # noqa: E402

DEFAULT_PORT = 8765
APP_VERSION_CURRENT = app_version()
MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


# --------------------------------------------------------------------------- #
# 请求处理
# --------------------------------------------------------------------------- #


class ApiHandler(BaseHTTPRequestHandler):
    server_version = f"WinProcManager/{app_version()}"
    protocol_version = "HTTP/1.1"  # keep-alive 复用连接，降低轮询开销

    # ---------------- 基础 ----------------

    def log_message(self, fmt: str, *args) -> None:  # 静默访问日志
        pass

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        # 大响应启用 gzip：扫描/监控结果可达数百 KB，压缩比通常 >8x
        if len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or "").lower():
            body = gzip.compress(body, compresslevel=6)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, rel_path: str) -> None:
        rel_path = rel_path.strip("/") or "index.html"
        full = os.path.abspath(os.path.join(WEB_DIR, rel_path))
        if not full.startswith(os.path.abspath(WEB_DIR)) or not os.path.isfile(full):
            self.send_error(404, "Not Found")
            return

        ctype = MIME_TYPES.get(os.path.splitext(full)[1].lower(),
                               "application/octet-stream")
        with open(full, "rb") as fh:
            body = fh.read()
        # ETag 协商缓存：文件未变化时返回 304，省去重复传输
        etag = 'W/"%d-%d"' % (int(os.path.getmtime(full)), len(body))
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # ---------------- GET ----------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

        if path == "/api/scan":
            force = query.get("force", ["0"])[0] in ("1", "true", "yes")
            quick = query.get("quick", ["0"])[0] in ("1", "true", "yes")
            try:
                data = scanner.full_scan(force=force, quick=quick)
                self._send_json({"ok": True, "data": data})
            except Exception as exc:  # pragma: no cover
                self._send_json({"ok": False, "error": f"扫描失败：{exc}"}, 500)
            return

        if path == "/api/status":
            self._send_json({
                "ok": True,
                "data": {
                    "version": app_version(),
                    "admin": control.is_admin(),
                    "hostname": socket.gethostname(),
                    "python": sys.version.split()[0],
                    "pid": os.getpid(),
                    "port": self.server.server_address[1],
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
            })
            return

        if path == "/api/monitor":
            # 监控数据由后台采样线程产出，这里只读快照，不阻塞请求
            history = query.get("history", ["1"])[0] in ("1", "true", "yes")
            try:
                self._send_json({"ok": True, "data": monitor.snapshot(include_history=history)})
            except Exception as exc:
                self._send_json({"ok": False, "error": f"读取监控数据失败：{exc}"}, 500)
            return

        if path == "/api/cleanup/targets":
            # 磁盘清理项清单 + 占用大小（预演，不删除任何东西）。
            # 目录体积统计可能耗时数十秒：优先返回缓存，后台线程异步刷新；
            # refresh=1（前端显式点「扫描占用」或清理完成后）才同步重算。
            force = query.get("refresh", ["0"])[0] in ("1", "true", "yes")
            try:
                if force:
                    data = cleanup.scan_targets()
                    with _targets_cache_lock:
                        _targets_cache["data"] = data
                        _targets_cache["ts"] = time.time()
                    self._send_json({"ok": True, "data": data, "cached": False})
                else:
                    with _targets_cache_lock:
                        cached = _targets_cache["data"]
                        age = time.time() - _targets_cache["ts"]
                    if cached is not None:
                        self._send_json({"ok": True, "data": cached, "cached": True})
                        if age > 120:
                            threading.Thread(target=_targets_refresh_worker, daemon=True).start()
                    else:
                        data = cleanup.scan_targets()
                        with _targets_cache_lock:
                            _targets_cache["data"] = data
                            _targets_cache["ts"] = time.time()
                        self._send_json({"ok": True, "data": data, "cached": False})
            except Exception as exc:
                self._send_json({"ok": False, "error": f"扫描可清理项失败：{exc}"}, 500)
            return

        if path == "/api/cleanup/drives":
            try:
                self._send_json({"ok": True, "data": cleanup.list_drives()})
            except Exception as exc:
                self._send_json({"ok": False, "error": f"读取磁盘列表失败：{exc}"}, 500)
            return

        if path == "/api/cleanup/files":
            # 大文件扫描，同样是只读操作
            drive = str(query.get("drive", [""] or [""])[0]).strip()[:2].upper()
            min_mb = query.get("min_mb", ["100"])[0]
            limit = query.get("limit", ["300"])[0]
            try:
                rows = cleanup.scan_large_files(drive or None,
                                                min_mb=float(min_mb or 100),
                                                limit=int(limit or 300))
                self._send_json({"ok": True, "data": rows})
            except Exception as exc:
                self._send_json({"ok": False, "error": f"扫描大文件失败：{exc}"}, 500)
            return

        if path == "/api/cleanup/wechat":
            # 微信可清理项概览：缓存 / 聊天图片 / 聊天视频 / 聊天文件（只读）
            try:
                self._send_json({"ok": True, "data": cleanup.wechat_summary()})
            except Exception as exc:
                self._send_json({"ok": False, "error": f"扫描微信缓存失败：{exc}"}, 500)
            return

        if path == "/api/cleanup/wechat-files":
            # 微信聊天图片/视频/缓存/文件清单，支持按大小或时间排序（只读）
            kind = str(query.get("kind", ["all"])[0]).strip().lower()
            sort_by = str(query.get("sort", ["size"])[0]).strip().lower()
            order = str(query.get("order", ["desc"])[0]).strip().lower()
            limit = query.get("limit", ["500"])[0]
            if kind not in ("all", "cache", "image", "video", "file"):
                kind = "all"
            if sort_by not in ("size", "time"):
                sort_by = "size"
            try:
                data = cleanup.scan_wechat_media(
                    kind=kind, sort_by=sort_by, order=order,
                    limit=max(1, min(int(limit or 500), 5000)))
                self._send_json({"ok": True, "data": data})
            except Exception as exc:
                self._send_json({"ok": False, "error": f"扫描微信文件失败：{exc}"}, 500)
            return

        if path == "/api/cleanup/agents":
            # Vibe Coding / AI 编程工具占用概览：每个工具一张卡片（只读）
            try:
                self._send_json({"ok": True, "data": cleanup.agent_summary()})
            except Exception as exc:
                self._send_json({"ok": False, "error": f"扫描编程工具缓存失败：{exc}"}, 500)
            return

        if path == "/api/cleanup/agent-files":
            # 指定编程工具（或全部已检出工具）的缓存文件清单，支持按大小 / 时间排序（只读）
            tool = str(query.get("tool", [""])[0]).strip()
            sort_by = str(query.get("sort", ["size"])[0]).strip().lower()
            order = str(query.get("order", ["desc"])[0]).strip().lower()
            limit = query.get("limit", ["500"])[0]
            if sort_by not in ("size", "time"):
                sort_by = "size"
            try:
                data = cleanup.scan_agent_files(
                    tool_id=tool, sort_by=sort_by, order=order,
                    limit=max(1, min(int(limit or 500), 5000)))
                self._send_json({"ok": True, "data": data})
            except Exception as exc:
                self._send_json({"ok": False, "error": f"扫描编程工具缓存文件失败：{exc}"}, 500)
            return

        if path == "/api/cleanup/software":
            # 全盘搜索软件目录（只读），命中结果会回填给 AI 编程工具 / 微信清理
            drive = str(query.get("drive", [""])[0]).strip()
            q = str(query.get("q", [""])[0]).strip()
            cat = str(query.get("category", ["all"])[0]).strip() or "all"
            secs = query.get("max_seconds", ["25"])[0]
            try:
                data = cleanup.scan_software_dirs(
                    drive=drive, query=q, category=cat,
                    max_seconds=max(5.0, min(float(secs or 25), 120.0)))
                self._send_json({"ok": True, "data": data})
            except Exception as exc:
                self._send_json({"ok": False, "error": f"全盘搜索软件目录失败：{exc}"}, 500)
            return

        if path == "/api/cleanup/software-files":
            # 某个软件目录下的文件清单，支持按大小 / 时间排序；默认只看缓存 / 日志子目录
            target = str(query.get("target", [""])[0]).strip()
            scope = str(query.get("scope", ["cache"])[0]).strip().lower()
            sort_by = str(query.get("sort", ["size"])[0]).strip().lower()
            order = str(query.get("order", ["desc"])[0]).strip().lower()
            limit = query.get("limit", ["500"])[0]
            if scope not in ("cache", "all"):
                scope = "cache"
            if sort_by not in ("size", "time"):
                sort_by = "size"
            try:
                data = cleanup.scan_software_files(
                    target=target, scope=scope, sort_by=sort_by, order=order,
                    limit=max(1, min(int(limit or 500), 5000)))
                self._send_json({"ok": True, "data": data})
            except Exception as exc:
                self._send_json({"ok": False, "error": f"读取软件目录文件失败：{exc}"}, 500)
            return

        # ---- 软件清理（已安装软件 + AI 工具缓存，合并展示） ----

        if path == "/api/cleanup/software-list":
            # 合并清单：系统已安装软件（可卸载）+ AI 编程工具缓存（可清理）
            inc_dir = str(query.get("discovered", ["0"])[0]) in ("1", "true", "yes")
            secs = query.get("max_seconds", ["25"])[0]
            try:
                data = software.software_cleanup_summary(
                    include_discovered=inc_dir,
                    max_seconds=max(5.0, min(float(secs or 25), 120.0)))
                self._send_json({"ok": True, "data": data})
            except Exception as exc:
                self._send_json({"ok": False, "error": f"扫描软件清单失败：{exc}"}, 500)
            return

        if path == "/api/cleanup/software-cache-files":
            # 某个软件的可清理文件清单，支持按大小 / 时间排序（只读）
            key = str(query.get("key", [""])[0]).strip()
            scope = str(query.get("scope", ["cache"])[0]).strip().lower()
            sort_by = str(query.get("sort", ["size"])[0]).strip().lower()
            order = str(query.get("order", ["desc"])[0]).strip().lower()
            limit = query.get("limit", ["400"])[0]
            if scope not in ("cache", "all"):
                scope = "cache"
            if sort_by not in ("size", "time"):
                sort_by = "size"
            try:
                data = software.software_cache_files(
                    key=key, scope=scope, sort_by=sort_by, order=order,
                    limit=max(1, min(int(limit or 400), 5000)))
                self._send_json({"ok": True, "data": data})
            except Exception as exc:
                self._send_json({"ok": False, "error": f"读取软件缓存文件失败：{exc}"}, 500)
            return

        # ---- 文件查找（顶层菜单：查找文件与文件夹） ----

        if path == "/api/search/drives":
            try:
                self._send_json({"ok": True, "data": software.list_search_drives()})
            except Exception as exc:
                self._send_json({"ok": False, "error": f"读取分区列表失败：{exc}"}, 500)
            return

        if path == "/api/search/files":
            kw = str(query.get("q", [""])[0]).strip()
            drive = str(query.get("drive", [""])[0]).strip()
            root = str(query.get("root", [""])[0]).strip()
            mode = str(query.get("mode", ["all"])[0]).strip().lower()
            ext = str(query.get("ext", [""])[0]).strip()
            min_mb = query.get("min_mb", ["0"])[0]
            sort_by = str(query.get("sort", ["size"])[0]).strip().lower()
            order = str(query.get("order", ["desc"])[0]).strip().lower()
            limit = query.get("limit", ["400"])[0]
            secs = query.get("max_seconds", ["20"])[0]
            depth = query.get("depth", ["7"])[0]
            with_size = str(query.get("dir_size", ["0"])[0]) in ("1", "true", "yes")
            if mode not in ("all", "file", "dir"):
                mode = "all"
            if sort_by not in ("size", "time", "name"):
                sort_by = "size"
            try:
                data = software.search_files(
                    keyword=kw, drive=drive, root=root, mode=mode, ext=ext,
                    min_size_mb=max(0.0, float(min_mb or 0)),
                    sort_by=sort_by, order=order,
                    limit=max(1, min(int(limit or 400), 2000)),
                    max_seconds=max(3.0, min(float(secs or 20), 120.0)),
                    max_depth=max(1, min(int(depth or 7), 16)),
                    with_dir_size=with_size)
                self._send_json({"ok": True, "data": data})
            except Exception as exc:
                self._send_json({"ok": False, "error": f"文件查找失败：{exc}"}, 500)
            return

        if path.startswith("/api/"):
            self._send_json({"ok": False, "error": "接口不存在"}, 404)
            return

        self._send_file(path if path not in ("", "/") else "/index.html")

    # ---------------- POST ----------------

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = self._read_body()

        if path == "/api/service/action":
            name = str(body.get("name", "")).strip()
            action = str(body.get("action", "")).strip()
            ok, msg = control.service_action(name, action)
            self._send_json({"ok": ok, "message": msg})
            return

        if path == "/api/service/config":
            name = str(body.get("name", "")).strip()
            start_type = str(body.get("start_type", "")).strip()
            ok, msg = control.set_start_type(name, start_type)
            self._send_json({"ok": ok, "message": msg})
            return

        if path == "/api/process/kill":
            pid = body.get("pid")
            tree = bool(body.get("tree", False))
            ok, msg = control.kill_process(pid, tree=tree)
            self._send_json({"ok": ok, "message": msg})
            return

        if path == "/api/elevate":
            ok, msg = control.elevate(self.server.server_address[1])
            self._send_json({"ok": ok, "message": msg})
            return

        # ---- 清理：磁盘清理默认 dry_run 预演，删除只作用于白名单 / 用户显式勾选 ----

        if path == "/api/cleanup/memory":
            try:
                opts = {
                    "trim_working_set": bool(body.get("trim_working_set", True)),
                    "clear_file_cache": bool(body.get("clear_file_cache", False)),
                    "purge_standby": bool(body.get("purge_standby", False)),
                }
                self._send_json({"ok": True, "data": cleanup.clean_memory(**opts)})
            except Exception as exc:
                self._send_json({"ok": False, "error": f"内存清理失败：{exc}"}, 500)
            return

        if path == "/api/cleanup/disk":
            ids = body.get("ids") or []
            dry = bool(body.get("dry_run", True))
            ok, result = cleanup.clean_disk(ids, dry_run=dry)
            self._send_json({"ok": ok, "message": result.pop("message", ""), "data": result})
            return

        if path == "/api/search/measure":
            # 测算搜索结果里文件 / 文件夹的真实占用（只读）
            paths = [str(p) for p in (body.get("paths") or [])]
            secs = body.get("max_seconds", 20)
            try:
                data = software.measure_paths(
                    paths, max_seconds=max(3.0, min(float(secs or 20), 120.0)))
                self._send_json({"ok": True, "data": data})
            except Exception as exc:
                self._send_json({"ok": False, "error": f"测算占用失败：{exc}"}, 500)
            return

        if path == "/api/search/reveal":
            # 在资源管理器中打开文件所在位置 / 打开文件夹
            ok, msg = software.reveal_path(str(body.get("path", "")).strip())
            self._send_json({"ok": ok, "message": msg})
            return

        if path == "/api/cleanup/uninstall":
            # 卸载软件：只允许使用注册表里登记的卸载命令，外部命令一律拒绝
            ident = str(body.get("ident", "")).strip()
            quiet = bool(body.get("quiet", False))
            try:
                ok, msg = software.uninstall_software(ident, quiet=quiet)
                self._send_json({"ok": ok, "message": msg})
            except Exception as exc:
                self._send_json({"ok": False, "error": f"卸载失败：{exc}"}, 500)
            return

        if path == "/api/cleanup/delete-files":
            paths = [str(p) for p in (body.get("paths") or [])]
            recycle = bool(body.get("recycle", True))
            ok, result = cleanup.delete_files(paths, use_recycle=recycle)
            self._send_json({"ok": ok, "message": result.pop("message", ""), "data": result})
            return

        if path == "/api/cleanup/empty-recycle-bin":
            ok, msg = cleanup.empty_recycle_bin()
            self._send_json({"ok": ok, "message": msg})
            return

        self._send_json({"ok": False, "error": "接口不存在"}, 404)


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #


def _fix_console_encoding() -> None:
    """让提示信息在中文 Windows 控制台（GBK）下也能正常显示。

    某些环境下 Python 处于 UTF-8 模式，直接向 GBK 控制台输出会出现乱码；
    这里在交互终端上改用控制台自身的代码页，重定向到管道时仍保持 UTF-8。
    """
    textutil.fix_console_encoding()


def pick_port(preferred: int) -> int:
    """端口被占用时自动顺延，最多尝试 20 次。"""
    for offset in range(20):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return preferred


_targets_cache: dict = {"data": None, "ts": 0.0}
_targets_cache_lock = threading.Lock()


def _targets_refresh_worker() -> None:
    """后台刷新清理项缓存，失败时保留旧缓存。"""
    try:
        data = cleanup.scan_targets()
    except Exception:
        return
    with _targets_cache_lock:
        _targets_cache["data"] = data
        _targets_cache["ts"] = time.time()


def main() -> int:
    parser = argparse.ArgumentParser(description="Windows进程管理工具（潍鲸 - weijing.co）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="服务端口")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    _fix_console_encoding()

    port = pick_port(args.port)
    server = ThreadingHTTPServer(("127.0.0.1", port), ApiHandler)

    # 后台启动监控采样线程；预热 CPU 采样让进程 CPU 首屏就有数值
    def warmup() -> None:
        time.sleep(1.5)
        try:
            scanner.collect_processes()
        except Exception:
            pass

    threading.Thread(target=warmup, daemon=True).start()
    try:
        monitor.start()
    except Exception:
        pass

    url = f"http://127.0.0.1:{port}"
    print("=" * 56)
    print("  Windows进程管理工具（潍鲸 - weijing.co） 已启动")
    print(f"  版本：v{app_version()}")
    print(f"  访问地址：{url}")
    print(f"  管理员权限：{'是' if control.is_admin() else '否（停止系统服务可能失败）'}")
    print("  关闭此窗口即可退出工具")
    print("=" * 56)

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出…")
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
