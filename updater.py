"""独立的更新器程序，用于替换主程序 exe。"""

import argparse
import ctypes
import logging
import os
import shutil
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("updater")


def wait_process_exit(pid: int, timeout: float) -> bool:
    """等待指定 PID 的进程退出，返回 True 表示已退出，False 表示超时或失败。"""
    if pid <= 0:
        log.error("非法 PID: %s", pid)
        return False

    if os.name == "nt":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        SYNCHRONIZE = 0x00100000

        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            err = ctypes.get_last_error()
            if err == 87:  # ERROR_INVALID_PARAMETER，PID 不存在
                return True
            # error 5 = 拒绝访问，进程还在，降级到轮询
            log.warning("OpenProcess 失败 (error=%d)，降级到轮询等待", err)
        else:
            try:
                result = kernel32.WaitForSingleObject(handle, int(timeout * 1000))
                return result == 0  # WAIT_OBJECT_0
            finally:
                kernel32.CloseHandle(handle)

    # 非 Windows，或 Windows 下 OpenProcess 失败的降级方案
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        time.sleep(0.5)
    return False


def retry_file_op(func, retries=5, delay=1.0):
    """带重试的文件操作，对抗杀软文件锁定。"""
    for i in range(retries):
        try:
            return func()
        except OSError as e:
            if i == retries - 1:
                raise
            log.warning("操作失败，%.1f秒后重试 (%s)", delay, e)
            time.sleep(delay)


def replace_file_atomic(target: str, replacement: str, backup: str):
    """使用 Windows ReplaceFileW 原子替换文件。"""
    if not os.path.exists(replacement):
        raise FileNotFoundError(f"新版本文件不存在: {replacement}")

    # 目标不存在时直接安装
    if not os.path.exists(target):
        os.replace(replacement, target)
        return

    # ReplaceFileW 要求 backup 不能已存在
    if os.path.exists(backup):
        os.remove(backup)

    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ok = kernel32.ReplaceFileW(
        target, replacement, backup,
        0x00000002,  # REPLACEFILE_IGNORE_MERGE_ERRORS
        None, None,
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "ReplaceFileW 失败")


def main():
    parser = argparse.ArgumentParser(description="主程序更新器")
    parser.add_argument("--exe-path", required=True, help="主程序 exe 的完整路径")
    parser.add_argument("--new-file", required=True, help="下载的新版本 exe 临时路径")
    parser.add_argument("--pid", required=True, type=int, help="主程序的进程 ID")
    args = parser.parse_args()

    exe_path = os.path.abspath(args.exe_path)
    new_file = os.path.abspath(args.new_file)
    pid = args.pid

    log.info("更新器启动")
    log.info("主程序路径: %s", exe_path)
    log.info("新版本文件: %s", new_file)
    log.info("主程序 PID: %d", pid)

    # 输入校验
    if not os.path.isfile(new_file):
        log.error("新版本文件不存在或不是文件: %s", new_file)
        sys.exit(1)

    exe_dir = os.path.dirname(exe_path)
    if not os.path.isdir(exe_dir):
        log.error("主程序目录不存在: %s", exe_dir)
        sys.exit(1)

    # 等待主程序退出
    log.info("等待主程序 (PID %d) 退出，超时 30 秒...", pid)
    if not wait_process_exit(pid, timeout=30):
        log.error("等待主程序退出超时，更新中止")
        sys.exit(1)
    log.info("主程序已退出")
    time.sleep(0.5)

    # staging 文件：先复制到同目录临时文件，再原子替换
    staging_path = os.path.join(
        exe_dir, f".{os.path.basename(exe_path)}.new.{os.getpid()}.tmp"
    )
    backup_path = exe_path + ".old"
    temp_dir = os.path.dirname(new_file)

    try:
        # 1. 复制新版本到 staging
        log.info("复制新版本到 staging: %s", staging_path)
        retry_file_op(lambda: shutil.copy2(new_file, staging_path))

        # 2. 原子替换主程序
        log.info("替换主程序...")
        if os.name == "nt":
            retry_file_op(lambda: replace_file_atomic(exe_path, staging_path, backup_path))
        else:
            def do_replace():
                if os.path.exists(backup_path):
                    os.remove(backup_path)
                if os.path.exists(exe_path):
                    os.replace(exe_path, backup_path)
                os.replace(staging_path, exe_path)
            retry_file_op(do_replace)

        log.info("已更新主程序: %s", exe_path)

    except Exception as e:
        log.error("更新失败: %s", e)
        # 清理 staging
        if os.path.exists(staging_path):
            try:
                os.remove(staging_path)
            except OSError:
                pass
        # 恢复备份
        if not os.path.exists(exe_path) and os.path.exists(backup_path):
            try:
                os.replace(backup_path, exe_path)
                log.info("已恢复旧版本")
            except OSError:
                log.error("恢复旧版本失败，主程序可能损坏")
        sys.exit(1)

    # 清理
    for path, desc in [(new_file, "原始新版本文件"), (backup_path, "备份文件")]:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                log.warning("清理%s失败，不影响更新", desc)

    if os.path.isdir(temp_dir) and not os.listdir(temp_dir):
        try:
            os.rmdir(temp_dir)
        except OSError:
            pass

    log.info("更新完成，更新器退出")


if __name__ == "__main__":
    main()
