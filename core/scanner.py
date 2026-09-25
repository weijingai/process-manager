# -*- coding: utf-8 -*-
"""系统扫描模块：采集 Windows 服务、进程与端口占用信息。

设计要点：
1. 服务优先通过 PowerShell CIM 一次性批量获取（快），失败时回退 psutil 逐个查询。
2. 进程与端口使用 psutil，对权限不足的项做降级处理，绝不因单项失败中断整体扫描。
3. CPU 占用率采用增量采样（两次扫描之间求差值），避免阻塞式采样拖慢响应。
"""

from __future__ import annotations

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

#: 禁止结束的系统关键进程（小写）
PROTECTED_PROCESS_NAMES = {
    "system", "system idle process", "registry", "memory compression",
    "secure system", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
    "services.exe", "lsass.exe", "lsaiso.exe", "fontdrvhost.exe", "sihost.exe",
}

#: 禁止停止的系统核心服务（小写），停止后会直接导致系统不稳定或需要重启
PROTECTED_SERVICE_NAMES = {
    "rpcss", "dcomlaunch", "lsm", "samss", "winmgmt", "cryptsvc",
    "eventlog", "power", "profSvc", "usermanager", "audiosrv",
}

STATUS_TEXT = {
    "running": "运行中",
    "stopped": "已停止",
    "start_pending": "启动中",
    "stop_pending": "停止中",
    "continue_pending": "继续中",
    "pause_pending": "暂停中",
    "paused": "已暂停",
    "unknown": "未知",
}

START_TYPE_TEXT = {
    "automatic": "自动",
    "automatic-delayed": "自动(延迟)",
    "manual": "手动",
    "disabled": "禁用",
    "unknown": "未知",
    "Auto": "自动",
    "Manual": "手动",
    "Disabled": "禁用",
}

PROC_STATUS_TEXT = {
    "running": "运行",
    "sleeping": "休眠",
    "disk-sleep": "磁盘休眠",
    "stopped": "停止",
    "tracing-stop": "跟踪停止",
    "zombie": "僵尸",
    "dead": "死亡",
    "wake-kill": "唤醒终止",
    "waking": "唤醒中",
    "idle": "空闲",
    "locked": "锁定",
    "waiting": "等待",
    "suspended": "挂起",
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
# 服务采集
# --------------------------------------------------------------------------- #

_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$svcs = @(Get-CimInstance Win32_Service | Select-Object `
    Name, DisplayName, State, StartMode, ProcessId, Description, PathName, StartName)
ConvertTo-Json -InputObject $svcs -Compress -Depth 4
"""


def _services_via_powershell() -> list[dict[str, Any]]:
    """用一次 PowerShell 调用批量拉取全部服务（约 1 秒）。"""
    import json

    proc = subprocess.run(
        [
            "powershell", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-Command", _PS_SCRIPT,
        ],
        capture_output=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0:
        raise RuntimeError("powershell 调用失败")

    raw = decode_output(proc.stdout).strip()
    if not raw or raw[0] not in "[{":
        raise RuntimeError("powershell 返回内容无法解析")

    data = json.loads(raw)
    if isinstance(data, dict):
        data = [data]

    result: list[dict[str, Any]] = []
    for item in data:
        state = (item.get("State") or "").strip()
        result.append({
            "name": item.get("Name") or "",
            "display_name": item.get("DisplayName") or item.get("Name") or "",
            "status": state.lower() or "unknown",
            "status_text": STATUS_TEXT.get(state.lower(), state),
            "start_type": _norm_start_type(item.get("StartMode")),
            "start_type_text": START_TYPE_TEXT.get(
                _norm_start_type(item.get("StartMode")),
                item.get("StartMode") or "未知",
            ),
            "pid": int(item.get("ProcessId") or 0) or None,
            "description": (item.get("Description") or "").strip(),
            "binpath": item.get("PathName") or "",
            "username": item.get("StartName") or "",
        })
    return result


def _norm_start_type(value: str | None) -> str:
    v = (value or "").strip().lower()
    if v in ("auto", "automatic"):
        return "automatic"
    if v in ("manual", "demand"):
        return "manual"
    if v in ("disabled",):
        return "disabled"
    return v or "unknown"


def _services_via_psutil() -> list[dict[str, Any]]:
    """PowerShell 不可用时的回退方案：逐个查询服务。"""
    result: list[dict[str, Any]] = []
    for svc in psutil.win_service_iter():
        try:
            d = svc.as_dict()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.Error):
            continue
        status = (d.get("status") or "unknown").lower()
        start_type = (d.get("start_type") or "unknown").lower()
        result.append({
            "name": d.get("name") or "",
            "display_name": d.get("display_name") or d.get("name") or "",
            "status": status,
            "status_text": STATUS_TEXT.get(status, status),
            "start_type": start_type,
            "start_type_text": START_TYPE_TEXT.get(start_type, start_type),
            "pid": d.get("pid") or None,
            "description": (d.get("description") or "").strip(),
            "binpath": d.get("binpath") or "",
            "username": d.get("username") or "",
        })
    return result


def _run_cmd(args: list[str], timeout: int = 30) -> tuple[int, str]:
    """执行系统命令并按中文 Windows 常见编码解码输出。"""
    try:
        proc = subprocess.run(
            args, capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return 1, ""

    return proc.returncode, decode_output(proc.stdout)


#: sc queryex 的 STATE 数字与状态名映射
_SC_STATE_MAP = {
    1: "stopped", 2: "start_pending", 3: "stop_pending", 4: "running",
    5: "continue_pending", 6: "pause_pending", 7: "paused",
}

#: 注册表 Start 值与启动类型映射
_REG_START_MAP = {
    0: ("boot", "引导"),
    1: ("system", "系统"),
    2: ("automatic", "自动"),
    3: ("manual", "手动"),
    4: ("disabled", "禁用"),
}


def _reg_value(key, name: str, default=None):
    try:
        value, vtype = winreg.QueryValueEx(key, name)
    except Exception:
        return default
    if vtype == getattr(winreg, "REG_EXPAND_SZ", 2):
        value = os.path.expandvars(value)
    return value


def _enrich_from_registry(services: list[dict[str, Any]]) -> None:
    """补充启动类型、描述、程序路径与登录身份（读取注册表，开销很小）。"""
    try:
        base = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                              r"SYSTEM\CurrentControlSet\Services")
    except Exception:
        return

    try:
        for svc in services:
            try:
                key = winreg.OpenKey(base, svc["name"])
            except Exception:
                continue
            with key:
                start = _reg_value(key, "Start", 3)
                try:
                    start = int(start)
                except Exception:
                    start = 3
                st_key, st_text = _REG_START_MAP.get(start, ("manual", "手动"))
                delayed = _reg_value(key, "DelayedAutostart", 0)
                if st_key == "automatic" and str(delayed) == "1":
                    st_key, st_text = "automatic-delayed", "自动(延迟)"

                desc = _reg_value(key, "Description", "") or ""
                # 形如 @%SystemRoot%\xxx.dll,-200 的间接字符串无法直接展开，置空
                if desc.startswith("@"):
                    desc = ""

                svc["start_type"] = st_key
                svc["start_type_text"] = st_text
                svc["description"] = desc.strip()
                svc["binpath"] = _reg_value(key, "ImagePath", "") or ""
                svc["username"] = _reg_value(key, "ObjectName", "") or ""
    finally:
        try:
            base.Close()
        except Exception:
            pass


def _services_via_sc() -> list[dict[str, Any]]:
    """通过 sc queryex 一次性获取全部 Win32 服务（毫秒级），再用注册表补全属性。"""
    rc, text = _run_cmd(
        ["sc.exe", "queryex", "type=", "service", "state=", "all"], timeout=40
    )
    if rc != 0 or "SERVICE_NAME" not in text:
        raise RuntimeError("sc queryex 调用失败")

    result: list[dict[str, Any]] = []
    for block in re.split(r"\r?\n(?=SERVICE_NAME:)", text):
        m_name = re.search(r"SERVICE_NAME:\s*(.*)", block)
        if not m_name:
            continue
        name = m_name.group(1).strip()
        if not name:
            continue

        m_disp = re.search(r"DISPLAY_NAME:\s*(.*)", block)
        m_state = re.search(r"STATE\s*:\s*(\d+)", block)
        m_pid = re.search(r"PID\s*:\s*(\d+)", block)

        try:
            status = _SC_STATE_MAP.get(int(m_state.group(1)), "unknown")
        except Exception:
            status = "unknown"
        try:
            pid = int(m_pid.group(1))
        except Exception:
            pid = 0

        display = (m_disp.group(1).strip() if m_disp else "") or name
        result.append({
            "name": name,
            "display_name": display,
            "status": status,
            "status_text": STATUS_TEXT.get(status, status),
            "start_type": "unknown",
            "start_type_text": "未知",
            "pid": pid or None,
            "description": "",
            "binpath": "",
            "username": "",
        })

    if not result:
        raise RuntimeError("未解析到任何服务")

    _enrich_from_registry(result)
    return result


def collect_services() -> tuple[list[dict[str, Any]], str]:
    """返回 (服务列表, 采集方式)，按速度依次尝试 sc → PowerShell → psutil。"""
    try:
        return _services_via_sc(), "sc"
    except Exception:
        pass
    try:
        return _services_via_powershell(), "wmi"
    except Exception:
        try:
            return _services_via_psutil(), "psutil"
        except Exception:
            return [], "failed"


# --------------------------------------------------------------------------- #
# 进程采集
# --------------------------------------------------------------------------- #

# 注意：不要加入 num_threads / status —— 这两个字段在 Windows 上每次调用都会
# 重新执行 NtQuerySystemInformation 全系统快照（约 3 秒/次），是主要性能瓶颈。
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

        rows.append({
            "pid": pid,
            "ppid": info.get("ppid") or 0,
            "name": name,
            "exe": info.get("exe") or "",
            "cmdline": cmdline or "",
            "username": _short_user(info.get("username")),
            # Windows 下活进程状态恒为 running，改用「运行中 / 空闲」更贴近实际
            "status": "running",
            "status_text": "运行中" if cpu_total > 0 else "空闲",
            "cpu": _CPU.percent(pid, cpu_total),
            "memory_mb": mem_mb,
            "create_time": created,
            "protected": name.lower() in PROTECTED_PROCESS_NAMES or pid <= 4,
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
        rows = _ports_via_netstat()

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


def _ports_via_netstat() -> list[dict[str, Any]]:
    """psutil 不可用时的回退：解析 netstat -ano 输出。"""
    rows: list[dict[str, Any]] = []
    for proto_kind, proto_name in (("TCP", "TCP"), ("UDP", "UDP")):
        try:
            proc = subprocess.run(
                ["netstat", "-ano", "-p", proto_kind],
                capture_output=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            out = decode_output(proc.stdout)
        except Exception:
            continue

        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4 or parts[0].upper() not in (proto_name, "TCP", "UDP"):
                continue
            try:
                p = parts[0].upper()
                local_ip, local_port = _rsplit_port(parts[1])
                remote_ip, remote_port = _rsplit_port(parts[2])
                state = parts[3] if len(parts) >= 5 else "NONE"
                pid = int(parts[-1])
            except Exception:
                continue
            rows.append({
                "proto": p,
                "local_ip": local_ip,
                "local_port": local_port,
                "remote_ip": remote_ip,
                "remote_port": remote_port,
                "state": state.upper(),
                "state_text": CONN_STATUS_TEXT.get(state.upper(), state),
                "pid": pid if pid > 0 else None,
            })
    return rows


def _rsplit_port(text: str) -> tuple[str, int | None]:
    try:
        ip, port = text.rsplit(":", 1)
        return ip, int(port)
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
