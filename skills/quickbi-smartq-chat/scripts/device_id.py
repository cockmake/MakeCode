# -*- coding: utf-8 -*-
"""
跨平台设备唯一标识工具。

提供稳定的设备唯一标识获取能力，覆盖 macOS / Windows / Linux / Android / iOS 等平台，
所有方案均失败时以 MAC 地址或持久化 UUID 兜底。

用法：
    from device_id import get_device_id, get_device_account_id, get_device_hostname

    device_id   = get_device_id()       # 原始设备标识字符串
    account_id  = get_device_account_id() # MD5 后的 accountId
    hostname    = get_device_hostname()   # 设备主机名
"""

from __future__ import annotations

import hashlib
import os
import platform
import random
import string
import uuid
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------

def get_device_id() -> str:
    """获取当前设备的稳定唯一标识字符串（未做 hash）。

    按平台依次尝试最可靠的标识源，所有方案均失败时以 MAC 地址兜底。

    平台策略：
      macOS   — IOPlatformUUID（硬件级，全版本通用）
      Windows — PowerShell CIM UUID → wmic UUID → 注册表 MachineGuid
      Linux   — /etc/machine-id → /var/lib/dbus/machine-id
      Android — getprop ro.serialno → settings get secure android_id
      iOS     — 生成 UUID 持久化到本地文件
      兜底    — uuid.getnode() MAC 地址
    """
    sys_name = platform.system()
    device_id: Optional[str] = None

    if sys_name == "Darwin":
        device_id = _read_macos_uuid()
    elif sys_name == "Windows":
        device_id = _read_windows_uuid()
    elif sys_name == "Linux":
        if _is_android():
            device_id = _read_android_id()
        if not device_id:
            device_id = _read_linux_machine_id()

    if not device_id:
        device_id = _read_persisted_device_id()

    if not device_id:
        device_id = _mac_address_fallback()

    return device_id


def get_device_account_id() -> str:
    """获取设备唯一标识的 MD5 值，可直接用作 accountId。"""
    device_id = get_device_id()
    account_id = hashlib.md5(device_id.encode("utf-8")).hexdigest()
    print(
        f"[设备标识] platform={platform.system()}, "
        f"device_id={device_id}, accountId(md5)={account_id}",
        flush=True,
    )
    return account_id


def get_device_hostname() -> str:
    """获取当前设备主机名；获取失败时返回带随机后缀的占位名。"""
    try:
        name = platform.node()
        if name:
            return name
    except Exception:
        pass
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
    return f"host_{suffix}"


# ---------------------------------------------------------------------------
# macOS (Darwin)
# ---------------------------------------------------------------------------

def _read_macos_uuid() -> Optional[str]:
    """通过 ioreg 读取 IOPlatformUUID（硬件级，全 macOS 版本通用）。"""
    import subprocess
    try:
        result = subprocess.run(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if "IOPlatformUUID" in line:
                val = line.split("=")[-1].strip().strip('"')
                if val:
                    return val
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

def _read_windows_uuid() -> Optional[str]:
    """读取 Windows 硬件 UUID，兼容 Win7 ~ Win11 25H2+。

    优先级：
      1. PowerShell Get-CimInstance（Win8+ / PowerShell 3+，Win11 25H2 唯一途径）
      2. wmic csproduct get uuid（Win7~Win10；Win11 25H2 已移除）
      3. 注册表 MachineGuid（所有 Windows 版本，重装系统后变化）
    """
    ps_cmd = (
        "powershell -NoProfile -Command "
        "\"(Get-CimInstance -ClassName Win32_ComputerSystemProduct).UUID\""
    )
    val = _run_cmd_strip(ps_cmd)
    if val and val.lower() not in ("", "none"):
        return val

    wmic_cmd = "wmic csproduct get uuid"
    val = _run_cmd_strip(wmic_cmd)
    if val:
        for line in val.splitlines():
            line = line.strip()
            if line and line.upper() != "UUID":
                return line

    return _read_windows_registry_machine_guid()


def _read_windows_registry_machine_guid() -> Optional[str]:
    """从注册表读取 MachineGuid（所有 Windows 版本可用）。"""
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
        ) as key:
            val, _ = winreg.QueryValueEx(key, "MachineGuid")
            if val:
                return str(val)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------

def _read_linux_machine_id() -> Optional[str]:
    """读取 Linux machine-id（systemd 系统 + 旧 dbus 系统）。"""
    for path_str in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            p = Path(path_str)
            if p.exists():
                content = p.read_text().strip()
                if content:
                    return content
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# Android (Termux)
# ---------------------------------------------------------------------------

def _is_android() -> bool:
    """判断当前是否运行在 Android 环境（Termux 等）。"""
    if "ANDROID_ROOT" in os.environ or "ANDROID_DATA" in os.environ:
        return True
    if Path("/system/build.prop").exists():
        return True
    return False


def _read_android_id() -> Optional[str]:
    """获取 Android 设备标识。

    优先级：
      1. getprop ro.serialno（硬件序列号，Android 9 及以下可用）
      2. settings get secure android_id（每设备每用户唯一，恢复出厂重置后变化）
    """
    for prop in ("ro.serialno", "ro.boot.serialno", "ril.serialnumber"):
        val = _run_cmd_strip(f"getprop {prop}")
        if val and val.lower() not in ("", "unknown"):
            return val

    val = _run_cmd_strip("settings get secure android_id")
    if val and val.lower() not in ("", "null"):
        return val

    return None


# ---------------------------------------------------------------------------
# iOS / 通用持久化兜底
# ---------------------------------------------------------------------------

_DEVICE_ID_FILE = Path.home() / ".qbi_device_id"


def _read_persisted_device_id() -> Optional[str]:
    """从本地持久化文件读取设备 ID（iOS 等无法获取硬件标识的环境）。

    首次调用时不自动创建，由 _mac_address_fallback 中的
    _create_persisted_device_id 负责创建。
    """
    try:
        if _DEVICE_ID_FILE.exists():
            content = _DEVICE_ID_FILE.read_text().strip()
            if content:
                return content
    except Exception:
        pass
    return None


def _create_persisted_device_id() -> str:
    """生成并持久化一个 UUID 作为设备标识。"""
    device_id = str(uuid.uuid4())
    try:
        _DEVICE_ID_FILE.write_text(device_id, encoding="utf-8")
    except Exception:
        pass
    return device_id


# ---------------------------------------------------------------------------
# 兜底
# ---------------------------------------------------------------------------

def _mac_address_fallback() -> str:
    """最终兜底：使用 MAC 地址。若 MAC 获取失败则生成持久化 UUID。"""
    mac_int = uuid.getnode()
    is_random = bool(mac_int & 0x010000000000)
    if is_random:
        return _create_persisted_device_id()
    mac_hex = f"{mac_int:012x}"
    return ":".join(mac_hex[i:i + 2] for i in range(0, 12, 2))


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _run_cmd_strip(cmd: str) -> Optional[str]:
    """执行 shell 命令并返回去空白的 stdout，失败返回 None。"""
    import subprocess
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None
