# -*- coding: utf-8 -*-
"""Linux进程管理工具（潍鲸 - weijing.co） - 本地服务入口

启动后在 http://127.0.0.1:<port> 提供可视化管理界面：
  GET  /api/scan              扫描服务 / 进程 / 端口
  GET  /api/monitor           CPU / 内存 / 磁盘 / 网络实时快照与历史序列
  GET  /api/cleanup/targets   磁盘可清理项清单（只读预演）
  GET  /api/cleanup/files     大文件扫描（只读）
  POST /api/cleanup/memory    内存清理（sync + drop_caches，需 root）
  POST /api/cleanup/disk      磁盘清理（支持 dry_run）
  POST /api/cleanup/delete-files   删除指定文件（默认送入回收站）
  POST /api/cleanup/empty-recycle-bin  清空回收站
  POST /api/service/action    启动 / 停止 / 重启服务（systemctl）
  POST /api/service/config    修改服务启动类型（enable / disable / mask）
  POST /api/process/kill      结束进程（可选进程树）
  POST /api/elevate           申请 root 权限重新启动（pkexec）
"""

from __future__ import annotations

import argparse
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

# 打包成单文件后，源码与资源会被解包到 sys._MEIPASS 临时目录，这里统一处理两种运行方式
if getattr(sys, "frozen", False):  # PyInstaller 打包后的运行环境
    BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    APP_DIR = BASE_DIR

WEB_DIR = os.path.join(BASE_DIR, "web")
if not os.path.isdir(WEB_DIR):  # 单文件模式下也允许从可执行文件同级目录读取
    WEB_DIR = os.path.join(APP_DIR, "web")
sys.path.insert(0, BASE_DIR)

from core import cleanup, control, monitor, scanner, textutil  # noqa: E402

DEFAULT_PORT = 8765
DEFAULT_HOST = "127.0.0.1"
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
    server_version = "LinuxProcManager/1.0"
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
            # 大文件扫描，同样是只读操作；Linux 下 drive 为挂载点前缀
            drive = str(query.get("drive", [""])[0]).strip()
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


def pick_port(preferred: int, host: str = DEFAULT_HOST) -> int:
    """端口被占用时自动顺延，最多尝试 20 次。"""
    for offset in range(20):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
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
    parser = argparse.ArgumentParser(description="Linux进程管理工具（潍鲸 - weijing.co）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="服务端口")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help="监听地址（默认 127.0.0.1 仅本机访问；0.0.0.0 允许局域网访问）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    textutil.fix_console_encoding()

    host = args.host.strip() or DEFAULT_HOST
    port = pick_port(args.port, host)
    server = ThreadingHTTPServer((host, port), ApiHandler)

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

    url = f"http://{host}:{port}"
    print("=" * 56)
    print("  Linux进程管理工具（潍鲸 - weijing.co） 已启动")
    print(f"  访问地址：{url}")
    root = control.is_admin()
    print(f"  root 权限：{'是' if root else '否（服务管理 / 内存清理可能失败，可用 sudo 运行）'}")
    if host != DEFAULT_HOST:
        print("  警告：服务已对外开放，请确保运行环境网络安全")
    print("  按 Ctrl+C 即可退出工具")
    print("=" * 56)

    if not args.no_browser and host == DEFAULT_HOST:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出…")
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
