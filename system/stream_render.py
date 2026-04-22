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
    思考过程与正文均采用『接力 Live (不使用 transient)』渲染 Markdown。
    """

    def __init__(self, console: Console = None, update_interval: float = 0.05):
        self.console = console or Console()
        self.update_interval = update_interval

    def render(self, stream_generator: Iterator[dict], agent_name: str = "Orchestrator") -> Tuple[str, List, Any]:
        self._print_header(agent_name)
        start_time = time.perf_counter()

        text_content = ""
        live_buffer = ""
        tool_calls = []
        raw_message = None

        reasoning_started = False
        text_started = False
        live = None
        last_update_time = 0

        # reasoning 也采用 Markdown Live 渲染
        reasoning_content = ""
        reasoning_buffer = ""
        reasoning_live = None
        reasoning_last_update_time = 0

        # ================= 完美的居中加载动画 =================
        # 1. 创建 Spinner 动画对象
        spinner_obj = Spinner("bouncingBar", text=f"[bold yellow]Awakening {agent_name}...[/bold yellow]")
        # 2. 将其整体居中对齐
        centered_spinner = Align.center(spinner_obj)
        # 3. 放入一个独立的 Live 容器中（transient=True 保证 stop() 时自动擦除）
        waiting_live = Live(centered_spinner, console=self.console, transient=True, refresh_per_second=15)
        waiting_live.start()
        # ======================================================

        try:
            for event in stream_generator:
                event_type = event.get("type")

                if event_type == "reasoning":
                    if not reasoning_started:
                        waiting_live.stop()  # 收到思考 token，立刻无痕擦除居中动画
                    reasoning_live, reasoning_content, reasoning_buffer, reasoning_last_update_time, reasoning_started = self._handle_reasoning(
                        event["content"], reasoning_live, reasoning_content, reasoning_buffer, reasoning_last_update_time, reasoning_started
                    )

                elif event_type == "text":
                    if not text_started:
                        waiting_live.stop()  # 兜底擦除
                        # 结束 reasoning 渲染，准备开始 text
                        if reasoning_live is not None:
                            self._safe_cleanup(reasoning_live, reasoning_buffer)
                            reasoning_live = None
                        live = self._start_text_section(reasoning_started)
                        text_started = True

                    chunk = event["content"]
                    text_content += chunk
                    live_buffer += chunk

                    live, live_buffer = self._process_block_commit(live, text_content, live_buffer)
                    last_update_time = self._throttled_update(live, live_buffer, last_update_time)

                elif event_type == "done":
                    text_content_done, tool_calls, raw_message = event["content"]
                    if text_content_done:
                        text_content = text_content_done
                    break

        finally:
            waiting_live.stop()  # 绝对兜底：防止流断开导致加载动画一直闪
            self._safe_cleanup(reasoning_live, reasoning_buffer)
            self._safe_cleanup(live, live_buffer)

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

    def _handle_reasoning(self, content: str, reasoning_live, reasoning_content: str, reasoning_buffer: str, reasoning_last_update_time: float, is_started: bool):
        if not is_started:
            self.console.print("[bold cyan]💭 Reasoning...[/bold cyan]\n")
            reasoning_live = Live(Markdown(""), console=self.console, auto_refresh=False)
            reasoning_live.start()

        reasoning_content += content
        reasoning_buffer += content

        reasoning_live, reasoning_buffer = self._process_block_commit(reasoning_live, reasoning_content, reasoning_buffer)
        reasoning_last_update_time = self._throttled_update(reasoning_live, reasoning_buffer, reasoning_last_update_time)

        return reasoning_live, reasoning_content, reasoning_buffer, reasoning_last_update_time, True

    def _start_text_section(self, reasoning_started: bool) -> Live:
        if reasoning_started:
            self.console.print()
            self.console.print(Padding(Rule(style="dim")))

        self.console.print("[bold green]✍️ Content:[/bold green]\n")

        live = Live(Markdown(""), console=self.console, auto_refresh=False)
        live.start()
        return live

    def _process_block_commit(self, live: Live, full_text: str, current_buffer: str) -> Tuple[Live, str]:
        """
        接力渲染逻辑：
        如果段落结束，将当前 Live 定格，并开启一个新的 Live 继续渲染下一段。
        """
        in_code_block = full_text.count("```") % 2 != 0

        if not in_code_block and "\n\n" in current_buffer:
            parts = current_buffer.rsplit("\n\n", 1)
            if len(parts) == 2:
                complete_blocks, remaining_buffer = parts

                # 1. 把当前 Live 强制更新为完整的段落，确保排版正确
                live.update(Markdown(complete_blocks), refresh=True)

                # 2. 停止当前 Live。因为它没有 transient=True，它会永远留在屏幕上，就像被 print 出来一样
                live.stop()

                # 3. 补充被 split 吞掉的段落间距
                self.console.print()

                # 4. 开启一个新的 Live 组件，接力渲染剩下的 buffer
                new_live = Live(Markdown(remaining_buffer), console=self.console, auto_refresh=False)
                new_live.start()

                return new_live, remaining_buffer

        return live, current_buffer

    def _throttled_update(self, live: Live, buffer: str, last_time: float) -> float:
        """节流刷新 Live 画面"""
        current_time = time.perf_counter()
        if current_time - last_time > self.update_interval:
            live.update(Markdown(buffer), refresh=True)
            return current_time
        return last_time

    def _safe_cleanup(self, live: Live, buffer: str):
        """流结束时的清理工作"""
        if live is not None:
            # 把最后一点 buffer 更新进去并定格
            if buffer.strip():
                live.update(Markdown(buffer), refresh=True)
            live.stop()

    def _handle_fallback(self, content: str, reasoning_started: bool, text_started: bool):
        if not text_started and content:
            if reasoning_started:
                self.console.print()
                self.console.print(Padding(Rule(style="dim"), (1, 0)))
            self.console.print(Markdown(content))
        elif not reasoning_started and not text_started:
            self.console.print()
