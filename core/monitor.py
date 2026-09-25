# -*- coding: utf-8 -*-
"""系统资源监控模块：CPU / 内存 / 磁盘 / 网络。

设计要点：
1. 后台常驻采样线程按固定间隔采集，接口只读取最新快照，**不阻塞 HTTP 请求**。
2. 速率类指标（磁盘 IO、网络流量）基于前后两次采样的差值计算。
3. 进程级 CPU 用「两次 cpu_times 差值」而非阻塞式采样；该项开销较大，
   只在每第 N 次采样时计算一次并缓存。
4. 任何单项失败都不影响整体，失败项降级为空值。
"""

from __future__ import annotations

import socket
import threading
import time
from collections import deque
from typing import Any

import psutil

#: 采样间隔（秒）
SAMPLE_INTERVAL = 2.0
#: 历史点上限：2s × 150 ≈ 最近 5 分钟
HISTORY_MAX = 150
#: 每第 N 次采样才统计一次进程级 Top（进程枚举开销较大）
PROC_EVERY = 5
#: 拓扑图取前几名
TOP_N = 8


def _round(v: float | None, n: int = 1) -> float | None:
    return None if v is None else round(v, n)


def human_bytes(n: float | None) -> str:
    if n is None:
        return "-"
    step = 1024.0
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    i = 0
    v = float(n)
    while v >= step and i < len(units) - 1:
        v /= step
        i += 1
    return f"{v:.0f} {units[i]}" if i == 0 else f"{v:.1f} {units[i]}"


def human_speed(n: float | None) -> str:
    if n is None:
        return "-"
    return f"{human_bytes(n)}/s"


class Monitor:
    """后台采样器。start() 幂等，重复调用只会存在一个线程。"""

    def __init__(self, interval: float = SAMPLE_INTERVAL) -> None:
        self.interval = interval
        self._lock = threading.RLock()
        self._history: deque[dict[str, Any]] = deque(maxlen=HISTORY_MAX)
        self._latest: dict[str, Any] = {}
        self._top: dict[str, list[dict[str, Any]]] = {"cpu": [], "mem": []}
        self._tick = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self._prev_net: Any = None
        self._prev_disk: Any = None
        self._prev_ts: float = 0.0
        self._prev_cpu: dict[str, Any] = {}
        self._prev_proc: dict[int, tuple[float, float]] = {}
        self._prev_proc_ts: float = 0.0
        self._self_proc_cpu: tuple[float, float] = (0.0, 0.0)

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        # 预热费率类指标：首次采样没有上一点，速率会缺值
        with self._lock:
            self._prev_net = self._safe(psutil.net_io_counters)
            self._prev_disk = self._safe(psutil.disk_io_counters)
            self._prev_ts = time.time()
        # CPU 基线预热：interval=None 依赖上一次调用，先空采一次避免首帧为 0
        self._safe(psutil.cpu_percent, percpu=True)
        self.sample()  # 首帧立即出数据，避免接口返回空
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.sample()
            except Exception:
                continue

    @staticmethod
    def _safe(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            return None

    # ---------------- 采样 ----------------

    def sample(self) -> dict[str, Any]:
        now = time.time()
        self._tick += 1

        cpu, cpu_per = self._cpu_percent(now)
        freq = self._safe(psutil.cpu_freq)
        vm = self._safe(psutil.virtual_memory)
        sm = self._safe(psutil.swap_memory)

        # ---- 网络 ----
        net = self._safe(psutil.net_io_counters)
        dt = max(now - self._prev_ts, 0.001)
        net_sent = net_recv = None
        if net and self._prev_net:
            net_sent = max((net.bytes_sent - self._prev_net.bytes_sent) / dt, 0)
            net_recv = max((net.bytes_recv - self._prev_net.bytes_recv) / dt, 0)
        net_total = {"sent": net.bytes_sent if net else None,
                     "recv": net.bytes_recv if net else None}

        # ---- 磁盘 IO ----
        disk = self._safe(psutil.disk_io_counters)
        read_speed = write_speed = None
        if disk and self._prev_disk:
            read_speed = max((disk.read_bytes - self._prev_disk.read_bytes) / dt, 0)
            write_speed = max((disk.write_bytes - self._prev_disk.write_bytes) / dt, 0)
        disk_total = {"read": disk.read_bytes if disk else None,
                      "write": disk.write_bytes if disk else None}

        with self._lock:
            self._prev_net, self._prev_disk, self._prev_ts = net, disk, now

        partitions = self._safe(self._collect_disks) or []
        ifaces = self._safe(self._collect_ifaces) or []

        point: dict[str, Any] = {
            "ts": now,
            "time": time.strftime("%H:%M:%S", time.localtime(now)),
            "cpu": round(cpu, 1),
            "mem": round(vm.percent, 1) if vm else None,
            "mem_used": vm.used if vm else None,
            "net_sent": round(net_sent or 0, 0),
            "net_recv": round(net_recv or 0, 0),
            "disk_read": round(read_speed or 0, 0),
            "disk_write": round(write_speed or 0, 0),
        }

        # ---- 进程 Top（降频执行）----
        if self._tick % PROC_EVERY == 1 or not self._top["cpu"]:
            top = self._safe(self._top_processes) or {"cpu": [], "mem": []}
            self._top = top

        snapshot: dict[str, Any] = {
            "time": point["time"],
            "timestamp": now,
            "cpu": {
                "percent": point["cpu"],
                "per_core": [round(x, 1) for x in cpu_per],
                "cores": psutil.cpu_count(logical=True) or len(cpu_per) or 1,
                "physical_cores": psutil.cpu_count(logical=False) or 0,
                "freq_mhz": round(freq.current) if freq else None,
                "name": self._cpu_name(),
            },
            "memory": {
                "total": vm.total if vm else None,
                "available": vm.available if vm else None,
                "used": vm.used if vm else None,
                "percent": point["mem"],
                "total_text": human_bytes(vm.total) if vm else "-",
                "available_text": human_bytes(vm.available) if vm else "-",
                "used_text": human_bytes(vm.used) if vm else "-",
                "swap_total": sm.total if sm else None,
                "swap_used": sm.used if sm else None,
                "swap_percent": round(sm.percent, 1) if sm else None,
                "swap_used_text": human_bytes(sm.used) if sm else "-",
            },
            "disk": {
                "partitions": partitions,
                "read_speed": round(read_speed, 0) if read_speed is not None else None,
                "write_speed": round(write_speed, 0) if write_speed is not None else None,
                "read_speed_text": human_speed(read_speed),
                "write_speed_text": human_speed(write_speed),
                "read_total": disk_total["read"],
                "write_total": disk_total["write"],
                "read_total_text": human_bytes(disk_total["read"]),
                "write_total_text": human_bytes(disk_total["write"]),
            },
            "network": {
                "sent_speed": round(net_sent, 0) if net_sent is not None else 0,
                "recv_speed": round(net_recv, 0) if net_recv is not None else 0,
                "sent_speed_text": human_speed(net_sent),
                "recv_speed_text": human_speed(net_recv),
                "sent_total": net_total["sent"],
                "recv_total": net_total["recv"],
                "sent_total_text": human_bytes(net_total["sent"]),
                "recv_total_text": human_bytes(net_total["recv"]),
                "interfaces": ifaces,
                "hostname": socket.gethostname(),
            },
            "top_cpu": self._top.get("cpu", []),
            "top_mem": self._top.get("mem", []),
            "uptime_text": self._uptime(),
            "boot_time": time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.localtime(psutil.boot_time())),
        }

        with self._lock:
            self._history.append(point)
            self._latest = snapshot
        return snapshot

    # ---------------- 子项采集 ----------------

    def _cpu_percent(self, now: float) -> tuple[float, list[float]]:
        """非阻塞式 CPU 占用。

        ``psutil.cpu_percent(interval=None)`` 内部保留了上一次调用的时间戳与计数器，
        返回的是两次调用之间的真实平均占用，不会像 ``interval=1`` 那样阻塞整个采样线程。
        首次调用没有基线，会返回全 0，因此 :meth:`start` 里先空采一次预热。
        """
        per = self._safe(psutil.cpu_percent, percpu=True) or []
        if not per:
            # 极端情况下退化为一次短阻塞采样，保证有数值
            per = self._safe(psutil.cpu_percent, percpu=True, interval=0.15) or []
        if not per:
            return 0.0, []
        total = round(sum(per) / len(per), 1)
        self._prev_cpu = {"ts": now, "per": per}
        return total, per

    @staticmethod
    def _cpu_name() -> str:
        try:
            import winreg  # noqa: WPS433 Windows 专有模块

            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            value, _ = winreg.QueryValueEx(key, "ProcessorNameString")
            winreg.CloseKey(key)
            return str(value).strip()
        except Exception:
            return "-"

    @staticmethod
    def _uptime() -> str:
        try:
            secs = int(time.time() - psutil.boot_time())
            d, rem = divmod(secs, 86400)
            h, rem = divmod(rem, 3600)
            m, _ = divmod(rem, 60)
            parts = []
            if d:
                parts.append(f"{d} 天")
            if h:
                parts.append(f"{h} 小时")
            parts.append(f"{m} 分钟")
            return " ".join(parts)
        except Exception:
            return "-"

    @staticmethod
    def _collect_disks() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for part in psutil.disk_partitions(all=False):
            if "cdrom" in (part.opts or "").lower():
                continue
            key = part.device.upper()
            if key in seen:
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except Exception:
                continue
            seen.add(key)
            rows.append({
                "device": part.device,
                "mountpoint": part.mountpoint,
                "fstype": part.fstype,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent": round(usage.percent, 1),
                "total_text": human_bytes(usage.total),
                "used_text": human_bytes(usage.used),
                "free_text": human_bytes(usage.free),
            })
        rows.sort(key=lambda r: r["total"], reverse=True)
        return rows

    @staticmethod
    def _collect_ifaces() -> list[dict[str, Any]]:
        addrs = psutil.net_if_addrs()
        io = psutil.net_io_counters(pernic=True) or {}
        is_up = psutil.net_if_stats()
        rows: list[dict[str, Any]] = []
        for name, stat in is_up.items():
            ipv4 = next((a.address for a in addrs.get(name, [])
                         if getattr(a, "family", None) == getattr(socket, "AF_INET", 2)), "")
            counters = io.get(name)
            rows.append({
                "name": name,
                "up": bool(stat.isup),
                "speed_mbps": stat.speed,
                "ipv4": ipv4,
                "sent": counters.bytes_sent if counters else None,
                "recv": counters.bytes_recv if counters else None,
                "sent_text": human_bytes(counters.bytes_sent) if counters else "-",
                "recv_text": human_bytes(counters.bytes_recv) if counters else "-",
            })
        rows.sort(key=lambda r: (not r["up"], r["name"]))
        return rows

    def _top_processes(self) -> dict[str, list[dict[str, Any]]]:
        """基于两次 cpu_times 差值算 CPU，避免阻塞式采样。"""
        rows: list[dict[str, Any]] = []
        alive: set[int] = set()
        now = time.time()
        dt = max(now - self._prev_proc_ts, 0.001) if self._prev_proc_ts else 1.0
        prev = self._prev_proc
        cur: dict[int, tuple[float, float]] = {}

        it = psutil.process_iter(["pid", "name", "cpu_times", "memory_info", "username"])
        for p in it:
            try:
                info = p.info  # type: ignore[attr-defined]
                pid = info["pid"]
                ct = info.get("cpu_times")
                mem = info.get("memory_info")
                if not ct:
                    continue
                total = (ct.user or 0) + (ct.system or 0)
                cur[pid] = (total, time.time())
                alive.add(pid)
                cpu_pct = None
                old = prev.get(pid)
                if old is not None:
                    cpu_pct = round(max((total - old[0]) / dt * 100, 0), 1)
                rows.append({
                    "pid": pid,
                    "name": info.get("name") or "-",
                    "cpu": cpu_pct,
                    "memory": mem.rss if mem else 0,
                    "memory_text": human_bytes(mem.rss) if mem else "-",
                    "username": (info.get("username") or "-").split("\\")[-1],
                })
            except Exception:
                continue

        self._prev_proc = {p: v for p, v in cur.items() if p in alive}
        self._prev_proc_ts = now

        top_cpu = sorted((r for r in rows if r["cpu"] is not None),
                         key=lambda r: r["cpu"], reverse=True)[:TOP_N]
        top_mem = sorted(rows, key=lambda r: r["memory"], reverse=True)[:TOP_N]
        return {"cpu": top_cpu, "mem": top_mem}

    # ---------------- 对外读取 ----------------

    def snapshot(self, include_history: bool = True) -> dict[str, Any]:
        with self._lock:
            data = dict(self._latest) if self._latest else {}
            history = list(self._history)
        if not data:  # 线程尚未产出首帧时同步采一次
            data = self.sample()
        if include_history:
            data["history"] = history
            data["history_len"] = len(history)
        return data


_MONITOR = Monitor()


def get_monitor() -> Monitor:
    return _MONITOR


def start() -> Monitor:
    _MONITOR.start()
    return _MONITOR


def snapshot(include_history: bool = True) -> dict[str, Any]:
    if not _MONITOR._thread or not _MONITOR._thread.is_alive():
        _MONITOR.start()
    return _MONITOR.snapshot(include_history=include_history)


def self_check() -> dict[str, Any]:
    """自检：直接采样一次，返回关键指标文本，供 CLI / 打包验证使用。"""
    m = Monitor()
    m.sample()
    time.sleep(1.0)
    s = m.sample()
    return {
        "cpu_percent": s["cpu"]["percent"],
        "cpu_cores": s["cpu"]["cores"],
        "cpu_name": s["cpu"]["name"],
        "mem_percent": s["memory"]["percent"],
        "mem_text": f"{s['memory']['used_text']} / {s['memory']['total_text']}",
        "disks": [(d["mountpoint"], d["total_text"], d["percent"]) for d in s["disk"]["partitions"]],
        "net": f"↓ {s['network']['recv_speed_text']}  ↑ {s['network']['sent_speed_text']}",
        "disk_io": f"读 {s['disk']['read_speed_text']}  写 {s['disk']['write_speed_text']}",
        "top_mem": [(p["name"], p["memory_text"]) for p in s["top_mem"][:3]],
        "uptime": s["uptime_text"],
    }


if __name__ == "__main__":
    import json
    from core import textutil  # noqa: E402

    textutil.fix_console_encoding()
    print(json.dumps(self_check(), ensure_ascii=False, indent=2))
