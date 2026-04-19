import concurrent.futures
import json
import sys
from typing import Any

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich import box
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.text import Text

from init import WORKDIR, log_error_traceback
from prompts import get_orchestrator_system_prompt
# 导入命令模块
from system.commands import (
    COMMAND_DESCRIPTIONS,
    SlashCommandCompleter,
    CommandHandler,
    CommandAction,
)
from system.ts_validator import init_ts_cache
from utils.common import (
    COMMON_TOOLS,
    COMMON_TOOLS_HANDLERS,
    STARTUP_TERMINAL_SOURCE,
    STARTUP_TERMINAL_TYPE,
    run_edit,
    run_read,
    run_write,
)
from utils.file_access import AgentFileAccess
from utils.llm_client import llm_client
from utils.mcp_manager import GLOBAL_MCP_MANAGER
from utils.memory import (
    THRESHOLD,
    auto_compact,
    estimate_tokens,
    list_checkpoints,
    load_checkpoint,
    micro_compact,
    save_checkpoint,
)
from utils.skills import SKILL_LOADER, SKILL_TOOLS, SKILL_TOOLS_HANDLERS
from utils.tasks import TASK_MANAGER_TOOLS, TASK_MANAGER_TOOLS_HANDLERS
from utils.teams import TEAM_TOOLS, TEAM_TOOLS_HANDLERS

from system.console_render import (
    _stringify_output,
    _extract_message_text,
    _format_readable_ui,
    _render_orchestrator_message,
    _render_tool_call,
    _render_tool_output,
    _render_user_message,
    _render_history,
    _render_token_usage,
    _render_startup_banner,
    _render_env_customization_hint,
    console,
)
STARTUP_TERMINAL_LABEL = STARTUP_TERMINAL_TYPE or "unavailable"

MAKECODE_ASCII = r"""
███╗   ███╗ █████╗ ██╗  ██╗███████╗ ██████╗ ██████╗ ██████╗ ███████╗
████╗ ████║██╔══██╗██║ ██╔╝██╔════╝██╔════╝██╔═══██╗██╔══██╗██╔════╝
██╔████╔██║███████║█████╔╝ █████╗  ██║     ██║   ██║██║  ██║█████╗
██║╚██╔╝██║██╔══██║██╔═██╗ ██╔══╝  ██║     ██║   ██║██║  ██║██╔══╝
██║ ╚═╝ ██║██║  ██║██║  ██╗███████╗╚██████╗╚██████╔╝██████╔╝███████╗
╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝ ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝
"""

USER_SESSION = None


def get_dynamic_system_prompt() -> str:
    return get_orchestrator_system_prompt(
        WORKDIR,
        STARTUP_TERMINAL_LABEL,
        STARTUP_TERMINAL_SOURCE,
    )


def get_current_tools_definition():
    """获取当前可用的工具定义（包含动态加载的 MCP 工具）"""
    return llm_client.format_tools(
        COMMON_TOOLS
        + SKILL_TOOLS
        + TASK_MANAGER_TOOLS
        + TEAM_TOOLS
        + GLOBAL_MCP_MANAGER.get_tools()
    )


BASE_SUPER_TOOLS = llm_client.format_tools(
    COMMON_TOOLS + SKILL_TOOLS + TASK_MANAGER_TOOLS + TEAM_TOOLS
)

orchestrator_access = AgentFileAccess()

BASE_SUPER_TOOLS_HANDLERS = {
    **COMMON_TOOLS_HANDLERS,
    **SKILL_TOOLS_HANDLERS,
    **TASK_MANAGER_TOOLS_HANDLERS,
    **TEAM_TOOLS_HANDLERS,
    "RunRead": lambda path, regions=None, **kwargs: run_read(
        path, regions, orchestrator_access
    ),
    "RunWrite": lambda path, content, **kwargs: run_write(
        path, content, orchestrator_access
    ),
    "RunEdit": lambda path, edits, **kwargs: run_edit(
        path, edits, orchestrator_access
    ),
}



def _parse_arguments(arguments: Any) -> dict:
    if isinstance(arguments, dict):
        return arguments
    if arguments is None:
        return {}
    if isinstance(arguments, str):
        payload = arguments.strip()
        if not payload:
            return {}
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            log_error_traceback("main parse arguments json decode", exc)
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}




def _request_with_progress(messages: list, current_tools: list):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            llm_client.generate,
            messages=messages,
            tools=current_tools,
        )

        with Progress(
                BarColumn(bar_width=30),
                TextColumn("[bold cyan]✨ Orchestrator is thinking..."),
                transient=True,
                console=console,
        ) as progress:
            progress.add_task("", total=None)
            return future.result()


def agent_loop(messages: list):
    """Agent 主循环：与 LLM 交互并执行工具调用"""
    global CURRENT_CHECKPOINT
    micro_compact(messages)
    current_super_tools = get_current_tools_definition()
    current_handlers = {
        **BASE_SUPER_TOOLS_HANDLERS,
        **GLOBAL_MCP_MANAGER.get_handlers(),
    }

    while True:
        _render_token_usage(
            messages,
            tools_definition=get_current_tools_definition(),
            system_prompt=get_dynamic_system_prompt(),
            threshold=THRESHOLD,
            estimate_tokens_fn=estimate_tokens,
        )

        try:
            response = _request_with_progress(messages, current_super_tools)
        except Exception as e:
            log_error_traceback("Orchestrator generation error", e)
            error_msg = f"Error during agent execution: {e}."
            console.print(f"[bold red]⚠️ {error_msg}[/bold red]")
            break

        text_content, tool_calls, raw_message = llm_client.parse_response(response)
        llm_client.append_assistant_message(messages, raw_message)
        has_tool_call = len(tool_calls) > 0

        if text_content:
            _render_orchestrator_message(text_content)

        for tc in tool_calls:
            tool_name = tc["name"]
            tool_id = tc["id"]
            tool_args = tc["arguments"]

            _render_tool_call(tool_name, tool_args)

            try:
                arguments = _parse_arguments(tool_args)
                handler = current_handlers.get(tool_name)
                if handler:
                    output = handler(**arguments)
                else:
                    output = f"Unknown tool: {tool_name}"
            except Exception as e:
                log_error_traceback(
                    f"Orchestrator tool execution error: {tool_name}", e
                )
                output = f"Error executing {tool_name}: {e}."

            _render_tool_output(tool_name, output)

            messages.append(llm_client.format_tool_result(tool_id, tool_name, output))

        CURRENT_CHECKPOINT = save_checkpoint(messages, CURRENT_CHECKPOINT)

        if not has_tool_call:
            break

    current_context_tokens = estimate_tokens(
        messages, tools_definition=current_super_tools, system_prompt=get_dynamic_system_prompt()
    )
    if current_context_tokens > THRESHOLD:
        compact_reason = (
            f"Post agent_loop auto compact triggered: estimated tokens "
            f"{current_context_tokens} exceeded threshold {THRESHOLD}."
        )
        try:
            auto_compact(messages, reason=compact_reason)
            console.print(
                "\n[bold green]✨ 当前对话上下文已成功压缩并保存！[/bold green]"
            )
        except Exception as e:
            log_error_traceback("Orchestrator auto-compact error", e)
            error_msg = f"Error executing auto_compact: {e}."
            console.print(f"[bold red]⚠️ {error_msg}[/bold red]")



def _init_tree_sitter_cache(console: Console):
    """初始化 tree-sitter 语言包缓存"""
    try:
        init_ts_cache()
    except Exception as e:
        console.print(f"[red]⚠️ 语法解析器初始化失败: {e}[/red]")


command_completer = SlashCommandCompleter()


def _init_user_session():
    global USER_SESSION
    if USER_SESSION is not None:
        return
    try:
        user_kb = KeyBindings()

        @user_kb.add(Keys.Enter)
        def _submit_query(event):
            buffer = event.current_buffer
            text = buffer.text.strip()

            if text.startswith("/"):
                if text in COMMAND_DESCRIPTIONS:
                    buffer.validate_and_handle()
                    return

                if buffer.complete_state and buffer.complete_state.completions:
                    if buffer.complete_state.current_completion:
                        buffer.apply_completion(
                            buffer.complete_state.current_completion
                        )
                    else:
                        buffer.apply_completion(buffer.complete_state.completions[0])
                    return

            buffer.validate_and_handle()

        @user_kb.add("c-n")
        def _insert_newline(event):
            event.current_buffer.insert_text("\n")

        def prompt_continuation(width, line_number, is_soft_wrap):
            return " " * (width - 4) + " ┃  "

        custom_style = Style.from_dict(
            {
                "prompt": "bold #00ff00",
                "arrow": "#00ffff bold",
            }
        )

        USER_SESSION = PromptSession(
            multiline=True,
            key_bindings=user_kb,
            prompt_continuation=prompt_continuation,
            style=custom_style,
            completer=command_completer,
            reserve_space_for_menu=5,
            complete_while_typing=True,
        )
    except Exception as exc:
        log_error_traceback("main init user session", exc)
        print_formatted_text(
            HTML(f"\n<ansired>Error initializing prompt session: {exc}</ansired>")
        )
        sys.exit(1)


def _read_user_query(messages: list = None) -> str:
    _init_user_session()

    console.print(
        "\n[dim]💡 Tip: Press [bold]Enter[/bold] to send, [bold]Ctrl+N[/bold] for newline.[/dim]"
    )

    # 将 rprompt 变量名改为 bottom_toolbar
    bottom_toolbar_content = None
    if messages is not None:
        tokens = estimate_tokens(
            messages,
            tools_definition=get_current_tools_definition(),
            system_prompt=get_dynamic_system_prompt(),
        )
        pct = (tokens / THRESHOLD) * 100
        color = "ansigreen" if pct < 70 else "ansiyellow" if pct < 90 else "ansired"
        # 组装 toolbar 内容
        bottom_toolbar_content = [(f"fg:{color}", f"📈 Tokens: {tokens}/{THRESHOLD} ({pct:.1f}%) ")]

    try:
        with patch_stdout():
            return USER_SESSION.prompt(
                [
                    ("class:prompt", "🤖 User "),
                    ("class:arrow", "❯❯ "),
                ],
                # 原来的 rprompt=rprompt 替换为 bottom_toolbar
                bottom_toolbar=bottom_toolbar_content,
            )
    except Exception as exc:
        log_error_traceback("main user input prompt failure", exc)
        raise


CURRENT_CHECKPOINT = None

if __name__ == "__main__":
    subtitle = f"Terminal Environment: [bold]{STARTUP_TERMINAL_LABEL}[/bold] (source={STARTUP_TERMINAL_SOURCE})"
    _render_startup_banner(subtitle)
    _render_env_customization_hint()

    # 初始化 tree-sitter 语言包缓存
    _init_tree_sitter_cache(console)

    GLOBAL_MCP_MANAGER.initialize(console=console)
    GLOBAL_MCP_MANAGER.start_background()

    history = [{"role": "system", "content": get_dynamic_system_prompt()}]

    command_handler = CommandHandler(
        console=console,
        mcp_manager=GLOBAL_MCP_MANAGER,
        skill_loader=SKILL_LOADER,
        get_system_prompt_fn=get_dynamic_system_prompt,
        save_checkpoint_fn=save_checkpoint,
        load_checkpoint_fn=load_checkpoint,
        list_checkpoints_fn=list_checkpoints,
        auto_compact_fn=auto_compact,
    )

    try:
        while True:
            try:
                query = _read_user_query(history)
            except (EOFError, KeyboardInterrupt) as exc:
                log_error_traceback("main user input interrupted", exc)
                console.print(
                    "\n[bold yellow]👋 Exiting MakeCode Agent. Goodbye![/bold yellow]"
                )
                break

            query = query.strip()
            if not query:
                continue

            # 处理命令
            command_result = command_handler.process_command(
                query=query,
                history=history,
                current_checkpoint=CURRENT_CHECKPOINT,
                render_banner_fn=_render_startup_banner,
                render_hint_fn=_render_env_customization_hint,
                render_history_fn=_render_history,
            )

            if command_result.action == CommandAction.EXIT:
                break
            elif command_result.action == CommandAction.CONTINUE:
                continue
            elif command_result.action == CommandAction.RUN_AGENT:
                history.append({"role": "user", "content": command_result.payload})
                agent_loop(history)
            elif command_result.action == CommandAction.RESET_CHECKPOINT:
                CURRENT_CHECKPOINT = None
            elif command_result.action == CommandAction.LOAD_HISTORY:
                history, CURRENT_CHECKPOINT = command_result.payload
            elif command_result.action == CommandAction.UPDATE_CHECKPOINT:
                CURRENT_CHECKPOINT = command_result.payload
            elif command_result.action == CommandAction.UPDATE_SYSTEM_PROMPT:
                history[0] = {"role": "system", "content": command_result.payload}
    finally:
        GLOBAL_MCP_MANAGER.stop()
