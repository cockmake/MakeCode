"""
流式输出取消模块。

在 LLM 流式响应期间，通过 TUI Ctrl+C 或 SIGINT 设置取消信号，
通知流式生成器中断。
"""

import signal
import threading

from system.tui_app import TuiRegion, post_tui

# 全局取消信号
stream_cancel_event = threading.Event()
_cancel_requested = False
_response_active = False
_previous_sigint_handler = None


def _handle_sigint(signum, frame):
    if _response_active:
        cancel_current_response()
        return
    if callable(_previous_sigint_handler):
        _previous_sigint_handler(signum, frame)
    elif _previous_sigint_handler == signal.SIG_DFL:
        raise KeyboardInterrupt


def cancel_current_response():
    global _cancel_requested
    if not _response_active:
        return False
    _cancel_requested = True
    stream_cancel_event.set()
    post_tui(TuiRegion.STATUS, "⚠️ 已取消当前响应")
    return True


def start_cancel_listener():
    """启动取消监听。在流式输出前调用。"""
    global _cancel_requested, _response_active, _previous_sigint_handler
    _cancel_requested = False
    _response_active = True
    stream_cancel_event.clear()
    try:
        _previous_sigint_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, _handle_sigint)
    except ValueError:
        _previous_sigint_handler = None


def stop_cancel_listener():
    """停止取消监听。在流式输出完成后调用。"""
    global _response_active, _previous_sigint_handler
    _response_active = False
    if _previous_sigint_handler is not None:
        try:
            signal.signal(signal.SIGINT, _previous_sigint_handler)
        except ValueError:
            pass
        _previous_sigint_handler = None
    if not _cancel_requested:
        stream_cancel_event.clear()


def is_cancelled() -> bool:
    """检查是否已被取消。"""
    return _cancel_requested


def is_response_active() -> bool:
    return _response_active


def reset_cancel():
    """重置取消标志。"""
    global _cancel_requested
    _cancel_requested = False
    stream_cancel_event.clear()
