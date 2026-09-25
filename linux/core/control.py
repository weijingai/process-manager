# -*- coding: utf-8 -*-
"""控制模块（Linux）：systemd 服务的启动 / 停止 / 重启 / 启动类型修改，以及进程终止。

服务操作通过 systemctl 完成；返回值统一为 (ok, message) 结构，便于前端直接提示。
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any

import psutil

from core.textutil import decode_output
from .scanner import PROTECTED_PROCESS_NAMES, PROTECTED_SERVICE_NAMES


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

#: systemctl 常见错误的中文说明
_SYSTEMD_ERRORS = {
    1: "操作失败（权限不足或单元状态异常）",
    4: "单元不存在或未加载",
    5: "操作被拒绝：需要 root 权限（可用 sudo 运行本工具）",
}


def is_admin() -> bool:
    """判断当前进程是否具备 root 权限。"""
    try:
        return os.geteuid() == 0  # type: ignore[attr-defined]
    except Exception:
        return False


def _unit_name(name: str) -> str:
    """把 'sshd' 规范为 'sshd.service'；已带后缀的原样返回。"""
    name = (name or "").strip()
    if not name:
        return ""
    if "." in name:
        return name
    return name + ".service"


def _base_name(name: str) -> str:
    """'sshd.service' → 'sshd'（用于保护名单比对）。"""
    return (name or "").split(".", 1)[0].lower()


def _run_systemctl(args: list[str], timeout: int = 30) -> tuple[int, str]:
    """执行 systemctl，返回 (返回码, 输出文本)。"""
    try:
        proc = subprocess.run(
            ["systemctl", *args], capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1460, "操作超时"
    except FileNotFoundError:
        return 128, "未找到 systemctl（本机可能未使用 systemd）"
    except Exception as exc:
        return 128, f"systemctl 调用失败：{exc}"

    text_parts = []
    for raw in (proc.stdout, proc.stderr):
        if not raw:
            continue
        text_parts.append(decode_output(raw))
    text = "".join(text_parts).strip()
    return proc.returncode, _pick_error_line(text)


def _pick_error_line(text: str) -> str:
    """提取关键错误行，避免整段输出直接抛给用户。"""
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in lines:
        if "failed" in ln.lower() or "denied" in ln.lower():
            return ln
    return lines[-1] if lines else ""


def _describe(rc: int, output: str) -> tuple[bool, str]:
    if rc == 0:
        return True, "操作成功"
    detail = _SYSTEMD_ERRORS.get(rc, output or f"systemctl 返回码 {rc}")
    return False, f"{detail}（错误码 {rc}）"


# --------------------------------------------------------------------------- #
# 服务操作
# --------------------------------------------------------------------------- #


def service_exists(name: str) -> bool:
    rc, _ = _run_systemctl(["status", _unit_name(name), "--no-pager"], timeout=15)
    return rc in (0, 3)  # 0=运行中 3=已停止，都说明单元存在


def get_service_status(name: str) -> str:
    """返回服务的英文状态，如 running / stopped / failed。"""
    rc, _ = _run_systemctl(["is-active", _unit_name(name)], timeout=15)
    if rc == 0:
        return "running"
    if rc == 3:
        return "stopped"
    return "unknown"


def start_service(name: str) -> tuple[bool, str]:
    rc, out = _run_systemctl(["start", _unit_name(name)])
    return _describe(rc, out)


def stop_service(name: str) -> tuple[bool, str]:
    if _base_name(name) in PROTECTED_SERVICE_NAMES:
        return False, "该服务为系统核心服务，已被工具保护，禁止停止"
    rc, out = _run_systemctl(["stop", _unit_name(name)])
    return _describe(rc, out)


def restart_service(name: str) -> tuple[bool, str]:
    if _base_name(name) in PROTECTED_SERVICE_NAMES:
        return False, "该服务为系统核心服务，已被工具保护，禁止重启"
    rc, out = _run_systemctl(["restart", _unit_name(name)])
    return _describe(rc, out)


def set_start_type(name: str, start_type: str) -> tuple[bool, str]:
    """修改服务启动类型（映射到 systemctl enable / disable / mask）。

    automatic(自动) → enable   开机自启
    manual(手动)    → disable  不自启、可手动拉起
    disabled(禁用)  → mask     彻底屏蔽
    """
    action = {
        "automatic": "enable", "auto": "enable", "enabled": "enable",
        "manual": "disable", "demand": "disable",
        "disabled": "mask", "masked": "mask",
    }.get((start_type or "").lower())
    if not action:
        return False, f"不支持的启动类型：{start_type}"

    unit = _unit_name(name)
    if action == "enable":
        # 先解除屏蔽再启用，否则 enable 会失败
        _run_systemctl(["unmask", unit])
    rc, out = _run_systemctl([action, unit])
    ok, msg = _describe(rc, out)
    if not ok:
        return ok, msg
    texts = {"enable": "启动类型已更新：开机自启",
             "disable": "启动类型已更新：不自启（可手动启动）",
             "mask": "启动类型已更新：已屏蔽禁用"}
    return True, texts.get(action, "启动类型已更新")


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

    if pid <= 1:
        return False, "系统核心进程（PID ≤ 1）不允许结束"

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

    # 等待退出，仍未退出则强制结束（Linux 下 SIGKILL 必达，除僵死 D 状态外）
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
    return False, last_error or "结束失败，可能需要 root 权限"


def elevate(port: int | None = None) -> tuple[bool, str]:
    """以 root 权限重新启动本工具。

    优先 pkexec（图形环境会弹密码框）；没有 pkexec 时提示用 sudo 重新运行。
    """
    import sys

    if is_admin():
        return False, "当前已经是 root 权限"

    target = sys.executable
    script = sys.argv[0]
    cmd = [target, script]
    if port:
        cmd += ["--port", str(port)]

    # pkexec：Polkit 图形认证（桌面环境可用）
    try:
        subprocess.Popen(
            ["pkexec", *cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True, "已发起 root 权限请求，请在认证弹窗中确认"
    except FileNotFoundError:
        pass
    except Exception:
        pass

    return False, "未找到 pkexec，请在终端中用 sudo 重新运行本工具以获得 root 权限"
