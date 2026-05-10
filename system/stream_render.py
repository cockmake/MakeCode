import time
from typing import Iterator, Tuple, List, Any

from rich.align import Align
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.rule import Rule
from rich.padding import Padding
from rich.spinner import Spinner


class StreamRenderer:
    """
    负责处理 LLM 流式输出的终端渲染器。
    思考过程与正文按完整 Markdown 段落增量输出，不使用强制刷新。
    """

    def __init__(self, console: Console = None, update_interval: float = 0.05):
        self.console = console or Console()
        self.update_interval = update_interval

    def render_text_stream(self, stream_generator: Iterator[dict]) -> Tuple[str, List, Any]:
        text_content = ""
        emitted_text = ""
        live_buffer = ""
        tool_calls = []
        raw_message = None

        try:
            for event in stream_generator:
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
                    if not live_buffer and text_content and not emitted_text:
                        live_buffer = text_content
                    break

        finally:
            self._safe_cleanup(live_buffer)

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

        waiting_live = Live(
            Align.center(Spinner("bouncingBar", text=f"[bold yellow]Awakening {agent_name}...[/bold yellow]")),
            console=self.console,
            transient=True,
            refresh_per_second=4,
        )
        waiting_live.start()

        try:
            for event in stream_generator:
                event_type = event.get("type")

                if event_type == "reasoning":
                    waiting_live.stop()
                    reasoning_content, reasoning_buffer, reasoning_started = self._handle_reasoning(
                        event["content"], reasoning_content, reasoning_buffer, reasoning_started
                    )

                elif event_type == "text":
                    if not text_started:
                        waiting_live.stop()
                        if reasoning_buffer:
                            self._safe_cleanup(reasoning_buffer)
                            reasoning_buffer = ""
                        self._start_text_section(reasoning_started)
                        text_started = True

                    chunk = event["content"]
                    text_content += chunk
                    live_buffer += chunk
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
            waiting_live.stop()
            self._safe_cleanup(reasoning_buffer)
            self._safe_cleanup(live_buffer)

        self._handle_fallback(text_content, reasoning_started, text_started)
        self._print_footer(agent_name, start_time)

        return text_content, tool_calls, raw_message
    # ==================== 私有辅助方法 ====================

    def _print_header(self, name: str):
        self.console.print()
        self.console.rule(f"[bold magenta] 🧠 {name} [/bold magenta]", style="magenta")

    def _print_footer(self, name: str, start_time: float):
        elapsed = time.perf_counter() - start_time
        self.console.rule(f"[bold magenta] 🧠 {name} ({elapsed:.2f}s) [/bold magenta]", style="magenta")
        self.console.print()

    def _handle_reasoning(self, content: str, reasoning_content: str, reasoning_buffer: str, is_started: bool):
        if not is_started:
            self.console.print("[bold cyan]💭 Reasoning...[/bold cyan]\n")

        reasoning_content += content
        reasoning_buffer += content
        reasoning_buffer, _ = self._process_block_commit(reasoning_content, reasoning_buffer)

        return reasoning_content, reasoning_buffer, True

    def _start_text_section(self, reasoning_started: bool):
        if reasoning_started:
            self.console.print()
            self.console.print(Padding(Rule(style="dim")))

        self.console.print("[bold green]✍️ Content:[/bold green]\n")

    def _process_block_commit(self, full_text: str, current_buffer: str) -> tuple[str, str]:
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
                self.console.print(Markdown(complete_blocks))
                self.console.print()
                return remaining_buffer, f"{complete_blocks}\n\n"

        return current_buffer, ""

    def _safe_cleanup(self, buffer: str):
        """流结束时输出剩余内容。"""
        if buffer.strip():
            self.console.print(Markdown(buffer))

    def _handle_fallback(self, content: str, reasoning_started: bool, text_started: bool):
        if not text_started and content:
            if reasoning_started:
                self.console.print()
                self.console.print(Padding(Rule(style="dim"), (1, 0)))
            self.console.print(Markdown(content))
        elif not reasoning_started and not text_started:
            self.console.print()

