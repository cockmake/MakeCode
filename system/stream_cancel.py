"""
流式输出取消模块。

在 LLM 流式响应期间，通过后台线程监听 ESC 键，
检测到后设置取消信号，通知流式生成器中断。
"""

import threading
import time

from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import HTML

# 全局取消信号
stream_cancel_event = threading.Event()

# 监听线程引用
_listener_thread: threading.Thread | None = None


def _listen_for_esc():
    """后台守护线程：监听 ESC 键以取消流式输出。"""
    import msvcrt

    while not stream_cancel_event.is_set():
        if msvcrt.kbhit():
            key = msvcrt.getch()
            if key == b'\x1b':  # ESC
                stream_cancel_event.set()
                print_formatted_text(
                    HTML("\n<ansiyellow>⚠️ 已取消当前响应</ansiyellow>")
                )
                return
        time.sleep(0.05)


def start_cancel_listener():
    """启动 ESC 取消监听线程。在流式输出前调用。"""
    global _listener_thread
    stream_cancel_event.clear()
    print_formatted_text(HTML("\n<ansidim>💡 按 ESC 可取消当前响应</ansidim>"))
    _listener_thread = threading.Thread(target=_listen_for_esc, daemon=True)
    _listener_thread.start()


def stop_cancel_listener():
    """停止 ESC 取消监听线程。在流式输出完成后调用。"""
    global _listener_thread
    stream_cancel_event.set()  # 通知线程退出
    if _listener_thread is not None:
        _listener_thread.join(timeout=0.2)
        _listener_thread = None


def is_cancelled() -> bool:
    """检查是否已被取消。"""
    return stream_cancel_event.is_set()


def reset_cancel():
    """重置取消标志。"""
    stream_cancel_event.clear()
