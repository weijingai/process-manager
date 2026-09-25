# -*- coding: utf-8 -*-
"""系统扫描模块：采集 Linux 服务（systemd）、进程与端口占用信息。

设计要点（与 Windows 版保持一致的对外数据结构）：
1. 服务优先通过 systemctl 两次批量调用获取（list-units + list-unit-files），
   再用一次 ``systemctl show`` 批量补 MainPID；systemd 不可用时降级为空列表。
2. 进程与端口使用 psutil，对权限不足的项做降级处理，绝不因单项失败中断整体扫描。
3. 端口 psutil 不可用时回退解析 ``ss -tupn`` 输出。
4. CPU 占用率采用增量采样（两次扫描之间求差值），避免阻塞式采样拖慢响应。
"""

from __future__ import annotations

import re
import socket
import subprocess
import time
from collections import defaultdict
from typing import Any

import psutil

from core.textutil import decode_output

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 禁止结束的系统关键进程（小写）；PID ≤ 1 一律保护
PROTECTED_PROCESS_NAMES = {
    "systemd", "init", "kthreadd", "systemd-journald",
    "systemd-udevd", "systemd-logind", "dbus-daemon", "dbus-broker",
}

#: 禁止停止的系统核心服务（小写，不含 .service 后缀），
#: 停止后会导致系统失去总线 / 日志 / 远程登录能力
PROTECTED_SERVICE_NAMES = {
    "dbus", "dbus-broker", "systemd-journald", "ssh", "sshd",
    "systemd-logind", "systemd-udevd", "systemd-journal-remote",
}

STATUS_TEXT = {
    "running": "运行中",
    "active": "运行中",
    "exited": "已退出(一次性)",
    "dead": "已停止",
    "stopped": "已停止",
    "failed": "失败",
    "activating": "启动中",
    "deactivating": "停止中",
    "reload": "重载中",
    "unknown": "未知",
    "not-found": "未安装",
    "masked": "已屏蔽",
}

#: systemctl unit 文件状态 → (start_type, start_type_text)
#: 前端下拉只认 automatic / manual / disabled 三键
UNIT_FILE_STATE_MAP = {
    "enabled": ("automatic", "自动"),
    "enabled-runtime": ("automatic", "自动(运行时)"),
    "linked": ("manual", "手动"),
    "linked-runtime": ("manual", "手动"),
    "generated": ("manual", "生成"),
    "indirect": ("manual", "间接"),
    "transient": ("manual", "临时"),
    "disabled": ("manual", "手动"),
    "static": ("manual", "静态"),
    "masked": ("disabled", "禁用(屏蔽)"),
    "masked-runtime": ("disabled", "禁用(屏蔽)"),
    "alias": ("manual", "别名"),
}

PROC_STATUS_TEXT = {
    "running": "运行",
    "sleeping": "休眠",
    "disk-sleep": "磁盘休眠",
    "stopped": "停止",
    "tracing-stop": "跟踪停止",
    "zombie": "僵尸",
    "dead": "死亡",
    "idle": "空闲",
    "locked": "锁定",
    "waiting": "等待",
}

CONN_STATUS_TEXT = {
    "LISTEN": "监听",
    "LISTENING": "监听",
    "ESTABLISHED": "已连接",
    "TIME_WAIT": "等待关闭",
    "CLOSE_WAIT": "等待关闭",
    "SYN_SENT": "同步已发送",
    "SYN_RECV": "同步已接收",
    "FIN_WAIT1": "结束等待1",
    "FIN_WAIT2": "结束等待2",
    "CLOSING": "关闭中",
    "LAST_ACK": "最后确认",
    "DELETE_TCB": "已删除",
    "NONE": "-",
}


# --------------------------------------------------------------------------- #
# CPU 增量采样器
# --------------------------------------------------------------------------- #


class CpuSampler:
    """基于前后两次扫描差值的 CPU 占用率计算，避免阻塞等待。"""

    def __init__(self) -> None:
        self._prev: dict[int, float] = {}
        self._prev_ts: float = 0.0

    def begin(self) -> float:
        now = time.time()
        self._prev_ts = self._prev_ts or now
        return now

    def percent(self, pid: int, cpu_total: float) -> float | None:
        prev = self._prev.get(pid)
        self._prev[pid] = cpu_total
        if prev is None:
            return None
        delta_t = time.time() - self._prev_ts
        if delta_t <= 0:
            return None
        value = (cpu_total - prev) / delta_t * 100
        return round(max(value, 0.0), 1)

    def finish(self, alive_pids: set[int]) -> None:
        self._prev = {p: v for p, v in self._prev.items() if p in alive_pids}
        self._prev_ts = time.time()


_CPU = CpuSampler()


# --------------------------------------------------------------------------- #
# 命令执行工具
# --------------------------------------------------------------------------- #


def _run_cmd(args: list[str], timeout: int = 30) -> tuple[int, str]:
    """执行系统命令并按 UTF-8 解码输出。"""
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout)
    except Exception:
        return 1, ""
    text = decode_output(proc.stdout)
    if proc.returncode != 0 and not text:
        text = decode_output(proc.stderr)
    return proc.returncode, text


# --------------------------------------------------------------------------- #
# 服务采集（systemd）
# --------------------------------------------------------------------------- #


def _strip_unit_suffix(unit: str) -> tuple[str, str]:
    """'sshd.service' → ('sshd', 'service')。"""
    base, dot, suffix = unit.partition(".")
    return base, (suffix if dot else "service")


def _services_via_systemctl() -> list[dict[str, Any]]:
    """systemd 环境下批量拉取全部服务单元（三次调用，合计约 1 秒）。"""
    # 1. 已加载的单元：状态 + 描述
    rc, text = _run_cmd(
        ["systemctl", "list-units", "--type=service", "--all",
         "--no-legend", "--no-pager", "--plain"], timeout=40)
    if rc != 0 and not text.strip():
        raise RuntimeError("systemctl list-units 调用失败")

    loaded: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        line = line.strip().lstrip("●").strip()
        if not line:
            continue
        # UNIT LOAD ACTIVE SUB DESCRIPTION（描述含空格，最多切 4 刀）
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        unit = parts[0]
        if not unit.endswith(".service"):
            continue
        active, sub = parts[2].lower(), parts[3].lower()
        desc = parts[4].strip() if len(parts) > 4 else ""
        loaded[unit] = {
            "active": active, "sub": sub, "desc": desc,
        }

    # 2. 单元文件状态：启动类型
    rc2, text2 = _run_cmd(
        ["systemctl", "list-unit-files", "--type=service",
         "--no-legend", "--no-pager", "--plain"], timeout=40)
    file_state: dict[str, str] = {}
    if text2.strip():
        for line in text2.splitlines():
            line = line.strip().lstrip("●").strip()
            parts = line.split()
            if len(parts) >= 2 and parts[0].endswith(".service"):
                file_state[parts[0]] = parts[1].lower()

    # 3. 批量补 MainPID（systemctl show 支持一次传多个单元，分批防参数过长）
    main_pids = _batch_main_pids(list(loaded.keys()) or list(file_state.keys()))

    # 汇总：已加载单元 ∪ 单元文件（含未加载的 enabled 服务）
    all_units = dict(loaded)
    for unit, state in file_state.items():
        if unit not in all_units:
            all_units[unit] = {"active": "inactive", "sub": "dead", "desc": ""}

    result: list[dict[str, Any]] = []
    for unit, info in all_units.items():
        active = info["active"]
        sub = info["sub"]
        if active == "active" and sub in ("running", "exited", "plugged"):
            status = "running" if sub == "running" else "running"
            status_text = STATUS_TEXT.get(sub, "运行中") if sub == "running" else "运行中(一次性)"
        elif active == "active":
            status = "running"
            status_text = STATUS_TEXT.get(sub, "运行中")
        elif active == "activating":
            status, status_text = "start_pending", "启动中"
        elif active == "deactivating":
            status, status_text = "stop_pending", "停止中"
        elif active == "failed":
            status, status_text = "failed", "失败"
        else:
            status, status_text = "stopped", "已停止"

        st_key, st_text = UNIT_FILE_STATE_MAP.get(
            file_state.get(unit, ""), ("manual", "未知"))
        name = unit[:-len(".service")] if unit.endswith(".service") else unit

        result.append({
            "name": name,
            "unit": unit,
            "display_name": info["desc"] or name,
            "status": status,
            "status_text": status_text,
            "start_type": st_key,
            "start_type_text": st_text,
            "pid": main_pids.get(unit) or None,
            "description": info["desc"],
            "binpath": "",
            "username": "root",
        })

    if not result:
        raise RuntimeError("未解析到任何服务")
    return result


def _batch_main_pids(units: list[str], chunk: int = 200) -> dict[str, int]:
    """一次 systemctl show 批量取多个单元的 MainPID。"""
    pids: dict[str, int] = {}
    args_head = ["systemctl", "show", "--no-pager", "--no-legend",
                 "--property=Id", "--property=MainPID"]
    for i in range(0, len(units), chunk):
        batch = units[i:i + chunk]
        rc, text = _run_cmd(args_head + batch, timeout=60)
        if not text:
            continue
        current = ""
        for line in text.splitlines():
            key, _, value = line.partition("=")
            if key == "Id" and value:
                current = value.strip()
            elif key == "MainPID" and current:
                try:
                    pid = int(value.strip())
                except ValueError:
                    pid = 0
                if pid > 0:
                    pids[current] = pid
                current = ""
    return pids


def _sysd_running() -> bool:
    """判断 systemd 是否可用（容器 / chroot 里可能没有）。"""
    try:
        proc = subprocess.run(
            ["systemctl", "is-system-running", "--no-pager"],
            capture_output=True, timeout=8)
        out = decode_output(proc.stdout).strip().lower()
        # running / degraded 都说明 systemd 在跑；offline / 输出为空则不可用
        return out in ("running", "degraded")
    except Exception:
        return False


def collect_services() -> tuple[list[dict[str, Any]], str]:
    """返回 (服务列表, 采集方式)。"""
    if _sysd_running():
        try:
            return _services_via_systemctl(), "systemctl"
        except Exception:
            pass
    return [], "failed"


# --------------------------------------------------------------------------- #
# 进程采集
# --------------------------------------------------------------------------- #

# Linux 下 /proc 直读开销很小，可以把状态字段一并带上
_PROC_ATTRS = [
    "pid", "ppid", "name", "exe", "cmdline", "username",
    "create_time", "memory_info", "cpu_times",
]


def collect_processes() -> list[dict[str, Any]]:
    now = _CPU.begin()
    alive: set[int] = set()
    rows: list[dict[str, Any]] = []

    for proc in psutil.process_iter(_PROC_ATTRS):
        try:
            info = proc.info
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

        pid = info.get("pid") or 0
        if not pid:
            continue
        alive.add(pid)

        name = info.get("name") or ""
        try:
            cpu_total = float(info["cpu_times"].user + info["cpu_times"].system)
        except Exception:
            cpu_total = 0.0

        mem = info.get("memory_info")
        mem_mb = round(mem.rss / 1024 / 1024, 1) if mem else 0.0

        cmdline = info.get("cmdline")
        if isinstance(cmdline, list):
            cmdline = " ".join(cmdline)

        try:
            created = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(info["create_time"])
            )
        except Exception:
            created = ""

        # 内核线程（无 cmdline 的 [xxx]）标注为空闲更贴近实际
        is_kernel_thread = not (info.get("cmdline") or []) and pid > 1 \
            and (info.get("ppid") or 0) in (0, 2)

        rows.append({
            "pid": pid,
            "ppid": info.get("ppid") or 0,
            "name": name,
            "exe": info.get("exe") or "",
            "cmdline": cmdline or "",
            "username": _short_user(info.get("username")),
            "status": "running",
            "status_text": "内核线程" if is_kernel_thread
                           else ("运行中" if cpu_total > 0 else "空闲"),
            "cpu": _CPU.percent(pid, cpu_total),
            "memory_mb": mem_mb,
            "create_time": created,
            "protected": name.lower() in PROTECTED_PROCESS_NAMES or pid <= 1,
        })

    _CPU.finish(alive)
    return sorted(rows, key=lambda r: (r["memory_mb"]), reverse=True)


def _short_user(username: str | None) -> str:
    if not username:
        return ""
    return username.split("\\")[-1]


# --------------------------------------------------------------------------- #
# 端口采集
# --------------------------------------------------------------------------- #


def collect_ports() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        conns = psutil.net_connections(kind="inet")
    except Exception:
        conns = []

    for c in conns:
        try:
            laddr = c.laddr
            raddr = c.raddr
            proto = "TCP" if c.type == socket.SOCK_STREAM else "UDP"
            if c.family == socket.AF_INET6:
                proto += "6"

            local_ip, local_port = _split_addr(laddr)
            remote_ip, remote_port = _split_addr(raddr) if raddr else ("", None)

            state = (c.status or "NONE").upper()
            rows.append({
                "proto": proto,
                "local_ip": local_ip,
                "local_port": local_port,
                "local_addr": fmt_addr(local_ip, local_port),
                "remote_ip": remote_ip,
                "remote_port": remote_port,
                "remote_addr": fmt_addr(remote_ip, remote_port),
                "state": state,
                "state_text": CONN_STATUS_TEXT.get(state, state),
                "pid": c.pid if c.pid and c.pid > 0 else None,
            })
        except Exception:
            continue

    if not rows:
        rows = _ports_via_ss()

    rows.sort(key=lambda r: (r["local_port"] or 0))
    return rows


def fmt_addr(ip: str, port: int | None) -> str:
    """把 IP 与端口格式化为 0.0.0.0:80 / [::]:80 形式。"""
    if port is None:
        return ip or "*"
    if ":" in (ip or ""):
        return f"[{ip}]:{port}"
    return f"{ip or '*'}:{port}"


def _split_addr(addr) -> tuple[str, int | None]:
    if not addr:
        return "", None
    try:
        ip = addr[0]
        port = addr[1] if len(addr) > 1 else None
        return ip or "", port
    except Exception:
        return "", None


_SS_USERS_RE = re.compile(r"pid=(\d+)")


def _ports_via_ss() -> list[dict[str, Any]]:
    """psutil 不可用时的回退：解析 ss -tupn 输出（含进程名与 PID）。"""
    rows: list[dict[str, Any]] = []
    rc, text = _run_cmd(["ss", "-tupn"], timeout=30)
    if rc != 0:
        rc, text = _run_cmd(["ss", "-tun"], timeout=30)
    if not text:
        return rows

    for line in text.splitlines()[1:]:  # 首行是表头
        parts = line.split()
        if len(parts) < 5 or parts[0].lower() not in ("tcp", "udp"):
            continue
        try:
            proto = parts[0].upper()
            state = parts[1].upper() if proto == "TCP" else "NONE"
            local_ip, local_port = _rsplit_port(parts[4])
            remote_ip, remote_port = (
                _rsplit_port(parts[5]) if len(parts) > 5 else ("", None))
            pid = None
            proc_name = ""
            tail = " ".join(parts[6:])
            m = _SS_USERS_RE.search(tail)
            if m:
                pid = int(m.group(1))
            if pid:
                rows.append({
                    "proto": proto,
                    "local_ip": local_ip,
                    "local_port": local_port,
                    "remote_ip": remote_ip,
                    "remote_port": remote_port,
                    "state": state if state else "NONE",
                    "state_text": CONN_STATUS_TEXT.get(state, state or "NONE"),
                    "pid": pid if pid > 0 else None,
                })
        except Exception:
            continue
    return rows


def _rsplit_port(text: str) -> tuple[str, int | None]:
    try:
        ip, port = text.rsplit(":", 1)
        return ip.strip("[]"), int(port)
    except Exception:
        return text, None


# --------------------------------------------------------------------------- #
# 汇总扫描
# --------------------------------------------------------------------------- #

_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
CACHE_TTL = 6.0

#: 服务列表变化不频繁且采集成本较高，单独缓存 30 秒
_SVC_CACHE: dict[str, Any] = {"ts": 0.0, "data": None, "source": ""}
SVC_CACHE_TTL = 30.0


def _get_services(reload: bool) -> tuple[list[dict[str, Any]], str]:
    if (not reload and _SVC_CACHE["data"]
            and (time.time() - _SVC_CACHE["ts"]) < SVC_CACHE_TTL):
        return _SVC_CACHE["data"], _SVC_CACHE["source"]
    services, source = collect_services()
    _SVC_CACHE.update({"ts": time.time(), "data": services, "source": source})
    return services, source


def full_scan(force: bool = False, quick: bool = False) -> dict[str, Any]:
    """执行扫描。

    force: 忽略整体缓存，强制重采
    quick: 服务复用 30 秒缓存，只刷新进程与端口（自动刷新场景，响应更快）
    """
    if (not force and not quick and _CACHE["data"]
            and (time.time() - _CACHE["ts"]) < CACHE_TTL):
        data = dict(_CACHE["data"])
        data["cached"] = True
        return data

    started = time.time()
    processes = collect_processes()
    ports = collect_ports()
    services, source = _get_services(reload=not quick)

    proc_index = {p["pid"]: p for p in processes}

    # PID -> 端口列表
    pid_ports: dict[int, list[int]] = defaultdict(list)
    for c in ports:
        pid = c.get("pid")
        if pid and c.get("local_port"):
            if c["local_port"] not in pid_ports[pid]:
                pid_ports[pid].append(c["local_port"])

    # 进程附加端口
    for p in processes:
        p["ports"] = sorted(pid_ports.get(p["pid"], []))
        p["port_text"] = ", ".join(str(x) for x in p["ports"][:8]) + (
            " …" if len(p["ports"]) > 8 else ""
        )

    # 服务附加进程名与端口
    running_services = 0
    for s in services:
        pid = s.get("pid")
        proc = proc_index.get(pid) if pid else None
        s["process_name"] = proc["name"] if proc else ""
        s["ports"] = sorted(pid_ports.get(pid, [])) if pid else []
        s["port_text"] = ", ".join(str(x) for x in s["ports"][:8]) + (
            " …" if len(s["ports"]) > 8 else ""
        )
        s["memory_mb"] = proc["memory_mb"] if proc else None
        s["cpu"] = proc["cpu"] if proc else None
        s["protected"] = (s["name"] or "").lower() in PROTECTED_SERVICE_NAMES
        if s["status"] == "running":
            running_services += 1

    # 端口附加进程名
    for c in ports:
        proc = proc_index.get(c.get("pid")) if c.get("pid") else None
        c["process_name"] = proc["name"] if proc else ""

    data = {
        "services": services,
        "processes": processes,
        "ports": ports,
        "cached": False,
        "source": source,
        "elapsed": round(time.time() - started, 2),
        "summary": {
            "services_total": len(services),
            "services_running": running_services,
            "processes_total": len(processes),
            "ports_total": len(ports),
            "ports_listen": sum(
                1 for c in ports if c["state"] in ("LISTEN", "LISTENING")
            ),
            "cpu_percent": psutil.cpu_percent(interval=None),
            "memory_percent": psutil.virtual_memory().percent,
        },
        "scan_time": time.strftime("%H:%M:%S"),
    }

    _CACHE["data"] = data
    _CACHE["ts"] = time.time()
    return data
