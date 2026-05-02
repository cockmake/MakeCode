"""
独立的更新器程序，用于替换主程序 exe 并重启。
设计为被打包成单独的 exe 使用。
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("updater")


def wait_process_exit(pid: int, timeout: float) -> bool:
    """等待指定 PID 的进程退出，返回 True 表示已退出，False 表示超时。"""
    # 不依赖 psutil，使用 os 模块实现
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if handle == 0:
            # 进程已不存在
            return True
        try:
            result = kernel32.WaitForSingleObject(handle, int(timeout * 1000))
            return result == 0  # WAIT_OBJECT_0
        finally:
            kernel32.CloseHandle(handle)
    else:
        # Unix: os.kill(pid, 0) 检测进程是否存在
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                # 进程存在但无权限，视为仍在运行
                pass
            time.sleep(0.5)
        return False


def main():
    parser = argparse.ArgumentParser(description="主程序更新器")
    parser.add_argument("--exe-path", required=True, help="主程序 exe 的完整路径")
    parser.add_argument("--new-file", required=True, help="下载的新版本 exe 临时路径")
    parser.add_argument("--pid", required=True, type=int, help="主程序的进程 ID")
    parser.add_argument("--launch-args", default="", help="重启主程序时传递的参数")
    args = parser.parse_args()

    exe_path = os.path.abspath(args.exe_path)
    new_file = os.path.abspath(args.new_file)
    pid = args.pid
    launch_args = args.launch_args

    log.info("更新器启动")
    log.info("主程序路径: %s", exe_path)
    log.info("新版本文件: %s", new_file)
    log.info("主程序 PID: %d", pid)

    # 1. 等待主程序退出
    log.info("等待主程序 (PID %d) 退出，超时 30 秒...", pid)
    if not wait_process_exit(pid, timeout=30):
        log.error("等待主程序退出超时，更新中止")
        sys.exit(1)
    log.info("主程序已退出")

    # 给系统一点时间释放文件句柄
    time.sleep(0.5)

    # 2. 备份旧 exe
    backup_path = exe_path + ".old"
    try:
        if os.path.exists(backup_path):
            os.remove(backup_path)
            log.info("已删除旧备份: %s", backup_path)
        if os.path.exists(exe_path):
            shutil.move(exe_path, backup_path)
            log.info("已备份旧版本: %s", backup_path)
    except OSError as e:
        log.error("备份旧版本失败: %s", e)
        sys.exit(1)

    # 3. 将新文件移动到主程序位置
    try:
        shutil.move(new_file, exe_path)
        log.info("已更新主程序: %s", exe_path)
    except OSError as e:
        log.error("替换主程序失败: %s，尝试恢复备份...", e)
        try:
            if os.path.exists(backup_path) and not os.path.exists(exe_path):
                shutil.move(backup_path, exe_path)
                log.info("已恢复旧版本")
        except OSError:
            log.error("恢复旧版本也失败，主程序可能损坏")
        sys.exit(1)

    # 4. 启动主程序
    try:
        cmd = [exe_path]
        if launch_args:
            cmd.extend(launch_args.split())
        subprocess.Popen(cmd)
        log.info("已启动主程序")
    except OSError as e:
        log.error("启动主程序失败: %s", e)
        sys.exit(1)

    # 5. 清理
    try:
        if os.path.exists(backup_path):
            os.remove(backup_path)
            log.info("已清理备份文件: %s", backup_path)
    except OSError as e:
        log.warning("清理备份文件失败（不影响更新）: %s", e)

    # 清理临时文件所在目录（如果为空）
    temp_dir = os.path.dirname(new_file)
    try:
        if os.path.isdir(temp_dir) and not os.listdir(temp_dir):
            os.rmdir(temp_dir)
            log.info("已清理临时目录: %s", temp_dir)
    except OSError:
        pass

    log.info("更新完成，更新器退出")


if __name__ == "__main__":
    main()
