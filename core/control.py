# -*- coding: utf-8 -*-
"""控制模块：Windows 服务的启动 / 停止 / 重启 / 启动类型修改，以及进程终止。

所有服务操作通过 sc.exe 完成；返回值统一为 (ok, message) 结构，便于前端直接提示。
"""

from __future__ import annotations

import ctypes
import subprocess
import time
from typing import Any

import psutil

from .scanner import PROTECTED_PROCESS_NAMES, PROTECTED_SERVICE_NAMES
from .textutil import decode_output


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

#: sc.exe / Windows 常见错误码的中文说明
SC_ERRORS = {
    5: "拒绝访问：需要以管理员身份运行本工具",
    1060: "指定的服务未安装",
    1061: "服务当前无法接受控制消息",
    1062: "服务尚未启动",
    1056: "服务已经在运行",
    1058: "服务已禁用，无法启动（请先改为手动或自动）",
    1053: "服务没有及时响应启动或控制请求",
    1052: "服务请求的控件无效",
    1072: "服务已被标记为删除",
    1073: "服务已存在",
    1077: "本次启动后未对该服务执行过任何操作",
    1079: "此服务的账户与同一进程上运行的其他服务所使用的账户不同",
    1084: "此服务无法在安全模式中启动",
    123: "文件名、目录名或卷标语法不正确",
    2: "系统找不到指定的文件（服务程序路径可能已失效）",
    87: "参数不正确",
    997: "操作重叠，服务可能正在处理上一个请求",
}


def is_admin() -> bool:
    """判断当前进程是否具备管理员权限。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


#: sc.exe 是否被环境策略禁用（例如安全软件黑名单），禁用后自动改用 PowerShell
_SC_AVAILABLE = True
_SC_DISABLED_REASON = ""


def _run_sc(args: list[str], timeout: int = 30) -> tuple[int, str]:
    """执行 sc.exe，返回 (返回码, 输出文本)。返回码 128 表示 sc.exe 不可用。"""
    global _SC_AVAILABLE, _SC_DISABLED_REASON

    if not _SC_AVAILABLE:
        return 128, _SC_DISABLED_REASON or "sc.exe 不可用"

    try:
        proc = subprocess.run(
            ["sc.exe", *args],
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return 1460, "操作超时"
    except FileNotFoundError:
        _SC_AVAILABLE = False
        _SC_DISABLED_REASON = "未找到 sc.exe"
        return 128, _SC_DISABLED_REASON
    except Exception as exc:
        # 被安全策略 / 权限策略拦截时直接切换通道
        _SC_AVAILABLE = False
        _SC_DISABLED_REASON = f"sc.exe 被系统策略阻止（{exc.__class__.__name__}）"
        return 128, _SC_DISABLED_REASON

    text_parts = []
    for raw in (proc.stdout, proc.stderr):
        if not raw:
            continue
        text_parts.append(decode_output(raw))
    text = "".join(text_parts).strip()
    return proc.returncode, _pick_error_line(text)


def _pick_error_line(text: str) -> str:
    """从 sc 输出中提取关键行，避免把整段英文提示全部抛给用户。"""
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in lines:
        if "FAILED" in ln.upper():
            return ln
    return lines[-1] if lines else ""


def _describe(rc: int, output: str) -> tuple[bool, str]:
    if rc == 0:
        return True, "操作成功"
    detail = SC_ERRORS.get(rc, output or f"系统错误码 {rc}")
    return False, f"{detail}（错误码 {rc}）"


# --------------------------------------------------------------------------- #
# 服务操作
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# PowerShell 备用通道（sc.exe 被环境策略禁用时使用）
# --------------------------------------------------------------------------- #

_PS_STATUS_MAP = {
    "running": "running", "stopped": "stopped",
    "startpending": "start_pending", "stoppending": "stop_pending",
    "continuepending": "continue_pending", "pausepending": "pause_pending",
    "paused": "paused",
}


def _run_ps(script: str, timeout: int = 60) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        return 128, f"PowerShell 调用失败：{exc}"

    out = decode_output(proc.stdout).strip()
    err = decode_output(proc.stderr).strip()
    return proc.returncode, out or err


def _friendly_ps_error(msg: str) -> str:
    m = msg.lower()
    if "access is denied" in m or "拒绝访问" in m:
        return "拒绝访问：需要以管理员身份运行本工具"
    if "cannot find any service" in m or "找不到任何服务" in m:
        return "服务不存在"
    if "cannot stop" in m or "无法停止" in m:
        return f"服务无法停止（可能被其他服务依赖）：{msg}"
    if "disabled" in m:
        return f"服务已禁用，无法启动：{msg}"
    return msg or "PowerShell 操作失败"


def _ps_service_action(action: str, name: str) -> tuple[bool, str]:
    safe = (name or "").replace("'", "''")
    cmds = {
        "start": "Start-Service",
        "stop": "Stop-Service -Force",
        "restart": "Restart-Service -Force",
    }
    if action not in cmds:
        return False, f"不支持的操作：{action}"

    script = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try {{ {cmds[action]} -Name '{safe}' -ErrorAction Stop; exit 0 }}
catch {{ Write-Output ("ERR:" + $_.Exception.Message); exit 1 }}
"""
    rc, out = _run_ps(script)
    if rc == 0:
        return True, "操作成功"
    return False, _friendly_ps_error(out.replace("ERR:", "", 1).strip())


def _ps_status(name: str) -> str:
    safe = (name or "").replace("'", "''")
    script = f"""
$ErrorActionPreference = 'Stop'
try {{ $s = Get-Service -Name '{safe}' -ErrorAction Stop; Write-Output $s.Status; exit 0 }}
catch {{ Write-Output 'unknown'; exit 1 }}
"""
    rc, out = _run_ps(script, timeout=30)
    if rc != 0:
        return "unknown"
    return _PS_STATUS_MAP.get(out.strip().lower(), out.strip().lower() or "unknown")


def _ps_start_type(name: str, start_type: str) -> tuple[bool, str]:
    safe = (name or "").replace("'", "''")
    ps_type = {"automatic": "Automatic", "manual": "Manual",
               "disabled": "Disabled"}.get((start_type or "").lower())
    if not ps_type:
        return False, f"不支持的启动类型：{start_type}"

    script = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try {{ Set-Service -Name '{safe}' -StartupType {ps_type} -ErrorAction Stop; exit 0 }}
catch {{ Write-Output ("ERR:" + $_.Exception.Message); exit 1 }}
"""
    rc, out = _run_ps(script)
    if rc == 0:
        return True, "启动类型已更新"
    return False, _friendly_ps_error(out.replace("ERR:", "", 1).strip())


# --------------------------------------------------------------------------- #
# 对外服务操作
# --------------------------------------------------------------------------- #


def service_exists(name: str) -> bool:
    rc, _ = _run_sc(["query", name], timeout=15)
    if rc == 128:
        return _ps_status(name) != "unknown"
    return rc == 0


def get_service_status(name: str) -> str:
    """返回服务的英文状态，如 running / stopped / start_pending。"""
    rc, out = _run_sc(["query", name], timeout=15)
    if rc == 128:
        return _ps_status(name)
    if rc != 0:
        return "unknown"
    out_l = out.lower()
    for key in ("running", "stopped", "start_pending", "stop_pending",
                "continue_pending", "pause_pending", "paused"):
        if key in out_l:
            return key
    return "unknown"


def start_service(name: str) -> tuple[bool, str]:
    rc, out = _run_sc(["start", name])
    if rc == 128:
        return _ps_service_action("start", name)
    return _describe(rc, out)


def stop_service(name: str) -> tuple[bool, str]:
    if (name or "").lower() in PROTECTED_SERVICE_NAMES:
        return False, "该服务为系统核心服务，已被工具保护，禁止停止"
    rc, out = _run_sc(["stop", name])
    if rc == 128:
        return _ps_service_action("stop", name)
    return _describe(rc, out)


def restart_service(name: str) -> tuple[bool, str]:
    if (name or "").lower() in PROTECTED_SERVICE_NAMES:
        return False, "该服务为系统核心服务，已被工具保护，禁止重启"

    ok, msg = stop_service(name)
    if not ok and "1062" not in msg and "尚未启动" not in msg:
        return False, f"停止阶段失败：{msg}"

    # 等待服务真正停止
    deadline = time.time() + 20
    stopped = False
    while time.time() < deadline:
        status = get_service_status(name)
        if status == "stopped":
            stopped = True
            break
        if status == "unknown":
            break
        time.sleep(0.4)

    if not stopped:
        return False, "服务在指定时间内未能停止，请稍后手动重试"

    ok, msg = start_service(name)
    return (ok, "重启成功" if ok else f"启动阶段失败：{msg}")


def set_start_type(name: str, start_type: str) -> tuple[bool, str]:
    """修改服务启动类型：automatic / manual / disabled。"""
    mapping = {
        "automatic": "auto", "auto": "auto",
        "manual": "demand", "demand": "demand",
        "disabled": "disabled",
    }
    value = mapping.get((start_type or "").lower())
    if not value:
        return False, f"不支持的启动类型：{start_type}"

    rc, out = _run_sc(["config", name, "start=", value])
    if rc == 128:
        return _ps_start_type(name, start_type)

    ok, msg = _describe(rc, out)
    return (True, "启动类型已更新") if ok else (ok, msg)


def service_action(name: str, action: str) -> tuple[bool, str]:
    """统一入口：start / stop / restart。"""
    name = (name or "").strip()
    if not name:
        return False, "服务名为空"

    action = (action or "").lower()
    if action == "start":
        return start_service(name)
    if action == "stop":
        return stop_service(name)
    if action == "restart":
        return restart_service(name)
    return False, f"不支持的操作：{action}"


# --------------------------------------------------------------------------- #
# 进程操作
# --------------------------------------------------------------------------- #


def kill_process(pid: int, tree: bool = False) -> tuple[bool, str]:
    """结束进程，tree=True 时连同子进程一起结束。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False, "PID 无效"

    if pid <= 4:
        return False, "系统核心进程（PID ≤ 4）不允许结束"

    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return False, "进程不存在或已退出"

    try:
        pname = (proc.name() or "").lower()
    except Exception:
        pname = ""

    if pname in PROTECTED_PROCESS_NAMES:
        return False, f"「{pname}」是系统关键进程，已被工具保护，禁止结束"

    targets: list[Any] = []
    if tree:
        try:
            targets = proc.children(recursive=True)
        except Exception:
            targets = []
    targets.append(proc)

    killed, failed = 0, 0
    last_error = ""
    for p in targets:
        try:
            p.terminate()
            killed += 1
        except psutil.AccessDenied:
            try:
                p.kill()
                killed += 1
            except Exception as exc:
                failed += 1
                last_error = str(exc)
        except psutil.NoSuchProcess:
            continue
        except Exception as exc:
            failed += 1
            last_error = str(exc)

    # 等待退出，仍未退出则强制结束
    time.sleep(0.3)
    for p in targets:
        try:
            if p.is_running():
                p.kill()
        except Exception:
            pass

    if killed and not failed:
        suffix = "（含子进程）" if tree else ""
        return True, f"已结束 {killed} 个进程{suffix}"
    if killed:
        return True, f"已结束 {killed} 个进程，{failed} 个失败：{last_error}"
    return False, last_error or "结束失败，可能需要管理员权限"


def elevate(port: int | None = None) -> tuple[bool, str]:
    """以管理员权限重新启动本工具（触发 UAC 弹窗）。"""
    import sys

    if is_admin():
        return False, "当前已经是管理员权限"

    params = f'--port {port}' if port else ""
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable,
            f'"{sys.argv[0]}" {params}'.strip(),
            None, 1,
        )
    except Exception as exc:
        return False, f"提权失败：{exc}"

    if rc <= 32:
        return False, f"提权被取消或失败（代码 {rc}）"
    return True, "已发起管理员权限请求，请在 UAC 弹窗中确认"
