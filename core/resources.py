# -*- coding: utf-8 -*-
"""资源占用控制（潍鲸 - Windows 进程管理工具）

提供两种能力：
  1) 主动占用（负载生成）：让本进程按设定值主动吃掉 CPU / 内存 / 磁盘 / 网络，
     每种资源可独立开关，随时开始 / 停止，并实时回报「实际占用」。
     用途：压测、防休眠、观察系统在负载下的表现。
  2) 限制其它进程：对选中的进程设置 CPU 速率上限与内存上限（Windows Job Object）。
     磁盘 / 网络占用在用户态下无法可靠限制，故不提供。
"""

from __future__ import annotations

import ctypes
import multiprocessing as mp
import os
import socket
import tempfile
import threading
import time

try:
    import psutil
except Exception:  # pragma: no cover - 极少数环境无 psutil
    psutil = None


# --------------------------------------------------------------------------- #
# 1) 主动占用
# --------------------------------------------------------------------------- #

class ResourceOccupier:
    """按设定值主动占用 CPU / 内存 / 磁盘 / 网络。

    每种资源可独立开关；start() 启动所有被启用的资源，stop() 全部停止。
    status() 返回各资源的目标值与实际占用。
    """

    def __init__(self) -> None:
        # 目标值：cpu=百分比, mem=MB, disk=MB/s, net=KB/s
        self.targets = {"cpu": 0.0, "mem": 0, "disk": 0.0, "net": 0.0}
        self.enabled = {"cpu": False, "mem": False, "disk": False, "net": False}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._mem_blocks: list[bytearray] = []
        self._disk_tmp: str | None = None
        self._net_port: int = 0
        self._cpu_duty_mp = None
        self._cpu_stop_mp = None
        self._cpu_mgr = None
        self._cpu_procs: list = []
        # 实际占用统计
        self.actual = {"cpu": 0.0, "mem": 0, "disk": 0.0, "net": 0.0}
        self._lock = threading.Lock()

    # -------------------------- 配置 --------------------------
    def configure(self, *, cpu=None, mem=None, disk=None, net=None,
                  enable_cpu=None, enable_mem=None, enable_disk=None, enable_net=None) -> None:
        if cpu is not None:
            self.targets["cpu"] = float(cpu)
        if mem is not None:
            self.targets["mem"] = float(mem)
        if disk is not None:
            self.targets["disk"] = float(disk)
        if net is not None:
            self.targets["net"] = float(net)
        if enable_cpu is not None:
            self.enabled["cpu"] = bool(enable_cpu)
        if enable_mem is not None:
            self.enabled["mem"] = bool(enable_mem)
        if enable_disk is not None:
            self.enabled["disk"] = bool(enable_disk)
        if enable_net is not None:
            self.enabled["net"] = bool(enable_net)

    # -------------------------- 控制 --------------------------
    def is_running(self) -> bool:
        return bool(self._threads)

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        self.actual = {"cpu": 0.0, "mem": 0, "disk": 0.0, "net": 0.0}

        if self.enabled["cpu"]:
            try:
                ctx = mp.get_context("spawn")
            except Exception:
                ctx = mp
            # Manager 托管的共享对象可跨 spawn 边界正确共享（普通 Value 在 spawn 下会变成副本）
            self._cpu_mgr = ctx.Manager()
            self._cpu_duty_mp = self._cpu_mgr.Value("d", max(0.01, min(1.0, self.targets["cpu"] / 100.0)))
            self._cpu_stop_mp = self._cpu_mgr.Event()
            n = max(1, os.cpu_count() or 1)
            self._cpu_procs = []
            for _ in range(n):
                p = ctx.Process(target=_mp_cpu_worker, args=(self._cpu_duty_mp, self._cpu_stop_mp))
                p.start()
                self._cpu_procs.append(p)
            self._threads.append(threading.Thread(target=self._cpu_monitor_mp, daemon=True))

        if self.enabled["mem"]:
            self._alloc_mem()

        if self.enabled["disk"]:
            self._disk_tmp = os.path.join(
                tempfile.gettempdir(), f"winproc_diskload_{os.getpid()}.tmp")
            self._threads.append(threading.Thread(target=self._disk_worker, daemon=True))

        if self.enabled["net"]:
            srv = threading.Thread(target=self._net_server, daemon=True)
            srv.start()
            # 等待服务器绑定端口
            for _ in range(50):
                if self._net_port:
                    break
                time.sleep(0.02)
            self._threads.append(srv)
            self._threads.append(threading.Thread(target=self._net_worker, daemon=True))

        for t in self._threads:
            if not t.is_alive():
                t.start()

    def stop(self) -> None:
        self._stop.set()
        if self._cpu_stop_mp is not None:
            try:
                self._cpu_stop_mp.set()
            except Exception:
                pass
        for p in self._cpu_procs:
            try:
                p.join(timeout=2)
            except Exception:
                pass
        self._cpu_procs = []
        for t in self._threads:
            try:
                t.join(timeout=2)
            except Exception:
                pass
        self._threads = []
        if self._cpu_mgr is not None:
            try:
                self._cpu_mgr.shutdown()
            except Exception:
                pass
        self._cpu_mgr = None
        self._cpu_stop_mp = None
        self._cpu_duty_mp = None
        self._mem_blocks = []  # 释放内存占用
        if self._disk_tmp and os.path.exists(self._disk_tmp):
            try:
                os.remove(self._disk_tmp)
            except OSError:
                pass
        self._disk_tmp = None
        self.actual = {"cpu": 0.0, "mem": 0, "disk": 0.0, "net": 0.0}

    # -------------------------- CPU（多进程，绕过 GIL 以真正占用多核） --------------------------
    def _cpu_monitor_mp(self) -> None:
        """被动监测：仅记录系统 CPU 实际占用（用于展示），不调整占空比。

        各工作进程按 target/100 的占空比空转，本身是确定性的开环控制，
        不受其它进程负载影响；这里只把真实系统占用回报给 UI。
        """
        stop = self._cpu_stop_mp
        if stop is None:
            return
        while not stop.is_set():
            try:
                self.actual["cpu"] = psutil.cpu_percent(interval=0.5)
            except Exception:
                self.actual["cpu"] = 0.0
            time.sleep(0.5)

    # -------------------------- 内存 --------------------------
    def _alloc_mem(self) -> None:
        target_bytes = int(self.targets["mem"]) * 1024 * 1024
        chunk = 1024 * 1024  # 1MB
        allocated = 0
        self._mem_blocks = []
        try:
            while allocated < target_bytes:
                self._mem_blocks.append(bytearray(chunk))
                allocated += chunk
        except MemoryError:
            pass
        self.actual["mem"] = len(self._mem_blocks) * chunk / (1024 * 1024)

    # -------------------------- 磁盘 --------------------------
    def _disk_worker(self) -> None:
        path = self._disk_tmp
        if not path:
            return
        buf = b"\x00" * (64 * 1024)
        target_bps = self.targets["disk"] * 1024 * 1024
        ring = max(64 * 1024 * 1024, int(target_bps * 2)) if target_bps > 0 else 64 * 1024 * 1024
        written = 0
        pos = 0
        t0 = time.perf_counter()
        last_report = t0
        last_written = 0
        try:
            with open(path, "wb+") as f:
                while not self._stop.is_set():
                    if target_bps <= 0:
                        time.sleep(0.1)
                        continue
                    n = f.write(buf)
                    written += n
                    pos += n
                    if pos >= ring:
                        f.seek(0)
                        pos = 0
                    elapsed = time.perf_counter() - t0
                    expected = target_bps * elapsed
                    if written > expected:
                        sleep_t = (written - expected) / target_bps
                        if sleep_t > 0:
                            time.sleep(min(sleep_t, 0.5))
                    now = time.perf_counter()
                    if now - last_report >= 1.0:
                        self.actual["disk"] = (written - last_written) / (now - last_report) / (1024 * 1024)
                        last_report = now
                        last_written = written
        except OSError:
            pass
        self.actual["disk"] = 0.0

    # -------------------------- 网络（本地回路） --------------------------
    def _net_server(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", 0))
            srv.listen(8)
        except OSError:
            return
        self._net_port = srv.getsockname()[1]
        srv.settimeout(1.0)
        while not self._stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._net_echo, args=(conn,), daemon=True).start()

    @staticmethod
    def _net_echo(conn: socket.socket) -> None:
        try:
            while True:
                d = conn.recv(4096)
                if not d:
                    break
                # 直接丢弃：仅用于制造本地回路网络负载
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _net_worker(self) -> None:
        if not self._net_port:
            return
        target_bps = self.targets["net"] * 1024  # KB/s -> bytes/s
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(("127.0.0.1", self._net_port))
        except OSError:
            return
        buf = b"\x00" * (32 * 1024)
        sent = 0
        t0 = time.perf_counter()
        last_report = t0
        last_sent = 0
        try:
            while not self._stop.is_set():
                if target_bps <= 0:
                    time.sleep(0.1)
                    continue
                try:
                    n = s.send(buf)
                except OSError:
                    break
                sent += n
                elapsed = time.perf_counter() - t0
                expected = target_bps * elapsed
                if sent > expected:
                    sleep_t = (sent - expected) / target_bps
                    if sleep_t > 0:
                        time.sleep(min(sleep_t, 0.5))
                now = time.perf_counter()
                if now - last_report >= 1.0:
                    self.actual["net"] = (sent - last_sent) / (now - last_report) / 1024
                    last_report = now
                    last_sent = sent
        finally:
            try:
                s.close()
            except OSError:
                pass
        self.actual["net"] = 0.0

    # -------------------------- 状态 --------------------------
    def status(self) -> dict:
        return {
            "running": self.is_running(),
            "cpu": {"enabled": self.enabled["cpu"], "target": self.targets["cpu"],
                    "actual": round(self.actual["cpu"], 1)},
            "mem": {"enabled": self.enabled["mem"], "target": self.targets["mem"],
                    "actual": round(self.actual["mem"], 1)},
            "disk": {"enabled": self.enabled["disk"], "target": self.targets["disk"],
                     "actual": round(self.actual["disk"], 2)},
            "net": {"enabled": self.enabled["net"], "target": self.targets["net"],
                    "actual": round(self.actual["net"], 1)},
        }


# --------------------------------------------------------------------------- #
# 2) 限制其它进程（Windows Job Object）
# --------------------------------------------------------------------------- #

def list_processes() -> list[str]:
    """返回排序后的 'name (pid)' 列表，供 UI 选择。"""
    items: list[str] = []
    seen: set[str] = set()
    if psutil is None:
        return items
    for p in psutil.process_iter(["pid", "name"]):
        try:
            name = p.info["name"] or "?"
            pid = p.info["pid"]
        except Exception:
            continue
        label = f"{name} ({pid})"
        if label in seen:
            continue
        seen.add(label)
        items.append(label)
    return sorted(items)


if os.name == "nt":
    import ctypes.wintypes as wintypes

    kernel32 = ctypes.windll.kernel32

    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    JOB_OBJECT_CPU_RATE = 0x000D
    JOB_OBJECT_EXTENDED_LIMIT = 0x0009

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMIT),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _CPU_RATE_INFO(ctypes.Structure):
        # JobObjectCpuRateInformation：CpuRate 单位为 1/100 百分比（如 3000 = 30%）
        _fields_ = [
            ("CpuRate", ctypes.c_uint32),
            ("Weight", ctypes.c_uint32),
            ("MinRate", ctypes.c_uint32),
            ("MaxRate", ctypes.c_uint32),
        ]

    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, wintypes.BOOL, ctypes.c_uint32]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL


class ProcessLimiter:
    """用 Windows Job Object 限制进程 CPU 速率上限与内存上限。"""

    def __init__(self) -> None:
        self._job = None
        self._pid = None

    @staticmethod
    def supported() -> bool:
        return os.name == "nt"

    def apply(self, pid: int, cpu_percent: float | None = None,
              mem_mb: float | None = None) -> dict:
        if os.name != "nt":
            return {"ok": False, "message": "限制其它进程仅支持 Windows 系统"}
        if cpu_percent is None and mem_mb is None:
            return {"ok": False, "message": "请至少设置 CPU 或内存上限之一"}
        if cpu_percent is not None and not (0 < cpu_percent <= 100):
            return {"ok": False, "message": "CPU 上限需在 1~100 之间"}

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return {"ok": False, "message": f"创建作业对象失败（错误 {ctypes.GetLastError()}）"}

        # 两类限制相互独立地尝试：某一项在当前系统不支持（如沙箱里 CPU 速率上限）
        # 不应连累另一项，只要至少一项成功即可应用。
        applied: list[str] = []
        failures: list[str] = []
        if cpu_percent is not None:
            rate = int(round(cpu_percent * 100))  # 单位：1/100 百分比
            info = _CPU_RATE_INFO(CpuRate=rate, Weight=0, MinRate=0, MaxRate=0)
            if kernel32.SetInformationJobObject(
                    job, JOB_OBJECT_CPU_RATE, ctypes.byref(info), ctypes.sizeof(info)):
                applied.append(f"CPU {cpu_percent}%")
            else:
                code = ctypes.GetLastError()
                if code == 24:  # ERROR_BAD_LENGTH：当前系统不支持 CPU 速率上限
                    failures.append(f"CPU 速率上限（当前系统不支持，错误 {code}）")
                else:
                    failures.append(f"CPU 速率上限（错误 {code}）")

        if mem_mb is not None:
            limit = int(mem_mb) * 1024 * 1024
            ext = _EXTENDED_LIMIT()
            ext.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_PROCESS_MEMORY
            ext.ProcessMemoryLimit = limit
            if kernel32.SetInformationJobObject(
                    job, JOB_OBJECT_EXTENDED_LIMIT, ctypes.byref(ext), ctypes.sizeof(ext)):
                applied.append(f"内存 {mem_mb}MB")
            else:
                code = ctypes.GetLastError()
                failures.append(f"内存上限（错误 {code}）")

        if not applied:
            kernel32.CloseHandle(job)
            return {"ok": False, "message": "未能应用任何限制：" + "；".join(failures)}

        hproc = kernel32.OpenProcess(
            PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION,
            False, int(pid))
        if not hproc:
            kernel32.CloseHandle(job)
            return {"ok": False,
                    "message": f"打开进程 {pid} 失败（权限不足或进程不存在，错误 {ctypes.GetLastError()}）"}

        if not kernel32.AssignProcessToJobObject(job, hproc):
            err = ctypes.GetLastError()
            kernel32.CloseHandle(hproc)
            kernel32.CloseHandle(job)
            if err == 5:
                return {"ok": False,
                        "message": f"无法限制进程 {pid}：该进程已在其它作业对象中（拒绝访问）"}
            return {"ok": False, "message": f"无法将进程 {pid} 加入作业对象（错误 {err}）"}

        kernel32.CloseHandle(hproc)
        self._job = job
        self._pid = int(pid)
        msg = f"已对进程 {pid} 应用限制（{' / '.join(applied)}）"
        if failures:
            msg += "；注意：" + "；".join(failures)
        return {"ok": True, "message": msg}

    def release(self) -> dict:
        if self._job:
            kernel32.CloseHandle(self._job)
            self._job = None
            pid = self._pid
            self._pid = None
            return {"ok": True, "message": f"已解除对进程 {pid} 的限制"}
        return {"ok": True, "message": "当前没有生效的限制"}


# --------------------------------------------------------------------------- #
# 多进程 CPU 占用工作进程（顶层函数，便于 Windows spawn 序列化）
# --------------------------------------------------------------------------- #

def _mp_cpu_worker(duty, stop) -> None:
    """按 duty 占空比空转，绕过 GIL 真正占用一整个核心的一小部分。"""
    while not stop.is_set():
        d = duty.value
        if d <= 0.01:
            time.sleep(0.05)
            continue
        busy = 0.02
        idle = (busy * (1.0 - d) / d) if d < 1.0 else 0.0
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < busy and not stop.is_set():
            pass
        if idle > 0:
            time.sleep(idle)
