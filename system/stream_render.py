import time
from typing import Iterator, Tuple, List, Any

from rich.markdown import Markdown
from rich.text import Text

from system.stream_cancel import is_cancelled
from system.tui_app import TuiRegion, post_tui


class StreamRenderer:
    """
    负责处理 LLM 流式输出的终端渲染器。
    思考过程与正文按完整 Markdown 段落增量输出，不使用强制刷新。
    """

    def __init__(self, console=None, update_interval: float = 0.05):
        self.console = console
        self.update_interval = update_interval

    def render_text_stream(self, stream_generator: Iterator[dict]) -> Tuple[str, List, Any]:
        text_content = ""
        emitted_text = ""
        live_buffer = ""
        tool_calls = []
        raw_message = None

        try:
            for event in stream_generator:
                if is_cancelled():
                    break
                event_type = event.get("type")

                if event_type == "text":
                    chunk = event["content"]
                    text_content += chunk
                    live_buffer += chunk
                    live_buffer, emitted_chunk = self._process_block_commit(text_content, live_buffer)
                    emitted_text += emitted_chunk

                elif event_type == "done":
                    text_content_done, tool_calls, raw_message = event["content"]
                    if text_content_done:
                        text_content = text_content_done
                        if text_content.startswith(emitted_text):
                            live_buffer = text_content[len(emitted_text):]
                    break

        finally:
            if live_buffer and not is_cancelled():
                self._safe_cleanup(live_buffer)

        if is_cancelled():
            return "", [], None

        return text_content, tool_calls, raw_message

    def render(self, stream_generator: Iterator[dict], agent_name: str = "Orchestrator") -> Tuple[str, List, Any]:
        self._print_header(agent_name)
        start_time = time.perf_counter()

        text_content = ""
        emitted_text = ""
        live_buffer = ""
        tool_calls = []
        raw_message = None

        reasoning_started = False
        text_started = False

        reasoning_content = ""
        reasoning_buffer = ""

        post_tui(TuiRegion.STATUS, f"Awakening {agent_name}...")

        try:
            for event in stream_generator:
                if is_cancelled():
                    break
                event_type = event.get("type")

                if event_type == "reasoning":
                    post_tui(TuiRegion.STATUS, f"{agent_name} reasoning")
                    reasoning_content, reasoning_buffer, reasoning_started = self._handle_reasoning(
                        event["content"], reasoning_content, reasoning_buffer, reasoning_started
                    )

                elif event_type == "text":
                    chunk = event["content"]
                    text_content += chunk
                    live_buffer += chunk

                    if not text_started and text_content.strip():
                        if reasoning_buffer:
                            self._safe_cleanup(reasoning_buffer, region=TuiRegion.REASONING)
                            reasoning_buffer = ""
                        self._start_text_section(agent_name, reasoning_started)
                        text_started = True

                    if text_started:
                        live_buffer, emitted_chunk = self._process_block_commit(text_content, live_buffer)
                        emitted_text += emitted_chunk

                elif event_type == "done":
                    text_content_done, tool_calls, raw_message = event["content"]
                    if text_content_done:
                        text_content = text_content_done
                        if text_started and text_content.startswith(emitted_text):
                            live_buffer = text_content[len(emitted_text):]
                    break

        finally:
            self._safe_cleanup(reasoning_buffer, region=TuiRegion.REASONING)
            if live_buffer and text_started and not is_cancelled():
                self._safe_cleanup(live_buffer)

        if is_cancelled():
            self._print_cancelled(agent_name, start_time)
            return "", [], None

        self._handle_fallback(agent_name, text_content, reasoning_started, text_started)
        self._print_footer(agent_name, start_time)

        return text_content, tool_calls, raw_message
    # ==================== 私有辅助方法 ====================

    def _print_header(self, name: str):
        post_tui(TuiRegion.STATUS, f"{name} started")

    def _print_footer(self, name: str, start_time: float):
        elapsed = time.perf_counter() - start_time
        post_tui(TuiRegion.CONTENT, Text(f"✓ {name} completed in {elapsed:.2f}s", style="#aaaaaa"))
        post_tui(TuiRegion.STATUS, f"{name} completed in {elapsed:.2f}s")

    def _print_cancelled(self, name: str, start_time: float):
        elapsed = time.perf_counter() - start_time
        post_tui(TuiRegion.CONTENT, Text(f"⚠ {name} cancelled in {elapsed:.2f}s", style="#f59e0b"))
        post_tui(TuiRegion.STATUS, f"{name} cancelled")

    def _handle_reasoning(self, content: str, reasoning_content: str, reasoning_buffer: str, is_started: bool):
        if not is_started:
            post_tui(TuiRegion.REASONING, "[bold cyan]💭 Reasoning...[/bold cyan]")

        reasoning_content += content
        reasoning_buffer += content
        reasoning_buffer, _ = self._process_block_commit(reasoning_content, reasoning_buffer, region=TuiRegion.REASONING)

        return reasoning_content, reasoning_buffer, True

    def _start_text_section(self, name: str, reasoning_started: bool):
        if reasoning_started:
            post_tui(TuiRegion.CONTENT, "")
        post_tui(TuiRegion.CONTENT, f"[bold cyan]💬 {name} Content...[/bold cyan]")

    def _process_block_commit(self, full_text: str, current_buffer: str, region: TuiRegion = TuiRegion.CONTENT) -> tuple[str, str]:
        """
        增量渲染逻辑：
        如果段落结束，将完整段落输出为 Markdown，并保留未完成的尾部 buffer。
        返回值为 (剩余 buffer, 本次已输出的原始文本片段)。
        """
        in_code_block = full_text.count("```") % 2 != 0

        if not in_code_block and "\n\n" in current_buffer:
            parts = current_buffer.rsplit("\n\n", 1)
            if len(parts) == 2:
                complete_blocks, remaining_buffer = parts
                post_tui(region, Markdown(complete_blocks))
                return remaining_buffer, f"{complete_blocks}\n\n"

        return current_buffer, ""

    def _safe_cleanup(self, buffer: str, region: TuiRegion = TuiRegion.CONTENT):
        """流结束时输出剩余内容。"""
        if buffer.strip():
            post_tui(region, Markdown(buffer))

    def _handle_fallback(self, name: str, content: str, reasoning_started: bool, text_started: bool):
        if not text_started and content.strip():
            self._start_text_section(name, reasoning_started)
            post_tui(TuiRegion.CONTENT, Markdown(content))
        elif not reasoning_started and not text_started:
            post_tui(TuiRegion.CONTENT, "")

