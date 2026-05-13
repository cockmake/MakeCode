import json
import sys
import threading
from typing import Any

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.formatted_text import HTML, ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown

from init import WORKDIR, log_error_traceback, STARTUP_TERMINAL_SOURCE, STARTUP_TERMINAL_TYPE
from prompts import get_orchestrator_system_prompt, get_title_generation_system_prompt
# 导入命令模块
from system.commands import (
    COMMAND_DESCRIPTIONS,
    SlashCommandCompleter,
    CommandHandler,
    CommandAction,
)
from system.console_render import (
    _render_tool_call,
    _render_tool_output,
    _render_history,
    _render_token_usage,
    _render_startup_banner,
    _render_env_customization_hint,
    render_current_task_plan,
    format_runtime_info,
    console,
)
from system.updater import check_update
from utils.hitl import get_hitl_status
from system.models import get_current_model_config
from utils.plan_mode import (
    is_plan_mode,
    is_plan_mode_command_allowed,
    PLAN_MODE_BLOCKLIST,
    PLAN_MODE_ALLOWED_COMMANDS,
)
from system.stream_render import StreamRenderer
from system.ts_validator import init_ts_cache
from system.tui_app import MakeCodeTuiApp, post_tui, TuiRegion, set_agent_loop_active
from utils.common import (
    COMMON_TOOLS,
    COMMON_TOOLS_HANDLERS,
    file_edit,
    file_read,
    file_create,
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
    rename_checkpoint_with_title,
    save_checkpoint,
)
from utils.skills import SKILL_LOADER, SKILL_TOOLS, SKILL_TOOLS_HANDLERS
import utils.tasks as _tasks_module
from utils.tasks import TASK_MANAGER_TOOLS, TASK_MANAGER_TOOLS_HANDLERS
from utils.teams import TEAM, TEAM_TOOLS, TEAM_TOOLS_HANDLERS
from tools.ask_user import ASK_USER_TOOLS, ASK_USER_TOOLS_HANDLERS

STARTUP_TERMINAL_LABEL = STARTUP_TERMINAL_TYPE or "unavailable"

USER_SESSION = None


def get_dynamic_system_prompt() -> str:
    return get_orchestrator_system_prompt(
        WORKDIR,
        STARTUP_TERMINAL_LABEL,
        STARTUP_TERMINAL_SOURCE,
        plan_mode=is_plan_mode(),
    )


def get_current_tools_definition():
    """获取当前可用的工具定义（包含动态加载的 MCP 工具）"""
    all_tools = _get_all_tools_definition()
    if is_plan_mode():
        # Plan Mode: 黑名单过滤，禁止写入/执行/委托工具
        filtered = [t for t in all_tools if t["function"]["name"] not in PLAN_MODE_BLOCKLIST]
        return filtered
    return all_tools


def _get_all_tools_definition():
    """获取全部工具定义（不考虑 Plan Mode 过滤）"""
    try:
        return llm_client.format_tools(
            COMMON_TOOLS
            + SKILL_TOOLS
            + TASK_MANAGER_TOOLS
            + TEAM_TOOLS
            + ASK_USER_TOOLS
            + GLOBAL_MCP_MANAGER.get_tools()
        )
    except RuntimeError as exc:
        if "No model configured" in str(exc):
            return []
        raise



orchestrator_access = AgentFileAccess()

BASE_SUPER_TOOLS_HANDLERS = {
    **COMMON_TOOLS_HANDLERS,
    **SKILL_TOOLS_HANDLERS,
    **TASK_MANAGER_TOOLS_HANDLERS,
    **TEAM_TOOLS_HANDLERS,
    **ASK_USER_TOOLS_HANDLERS,
    "FileRead": lambda path, regions, **kwargs: file_read(
        path, regions, orchestrator_access
    ),
    "FileCreate": lambda path, content, **kwargs: file_create(
        path, content, orchestrator_access
    ),
    "FileEdit": lambda path, edits, **kwargs: file_edit(
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
            parsed = json.loads(payload, strict=False)
        except json.JSONDecodeError as exc:
            log_error_traceback("main parse arguments json decode", exc)
            return {"_error": f"Failed to parse tool arguments: {exc}. Raw: {payload[:200]}"}
        if isinstance(parsed, dict):
            return parsed
        log_error_traceback(
            "main parse arguments type mismatch",
            ValueError(f"Expected dict, got {type(parsed).__name__}"),
        )
        return {"_error": f"Tool arguments parsed to {type(parsed).__name__}, expected dict. Raw: {payload[:200]}"}
    log_error_traceback(
        "main parse arguments unexpected type",
        TypeError(f"Unexpected type: {type(arguments).__name__}"),
    )
    return {"_error": f"Unexpected arguments type: {type(arguments).__name__}"}


def generate_title(user_query: str) -> str:
    """Generate a short title for the conversation based on the first user query."""
    try:
        messages = [
            {"role": "system", "content": get_title_generation_system_prompt()},
            {"role": "user", "content": user_query},
        ]
        response = llm_client.generate(messages)
        # Parse response based on client type
        if hasattr(response, 'choices'):  # Chat API
            return response.choices[0].message.content.strip()
        else:  # Response API
            for item in response.output:
                if item.type == "message":
                    return next(
                        (c.text for c in item.content if c.type == "output_text"), ""
                    ).strip()
    except Exception as exc:
        log_error_traceback("Failed to generate title", exc)
    return None


def _stream_with_render(messages: list, current_tools: list):
    """
    流式请求渲染：
    1. reasoning 和正文均交给 StreamRenderer 处理。
    2. StreamRenderer 负责按完整 Markdown 段落增量输出，并返回工具调用信息。
    """
    from system.stream_cancel import start_cancel_listener, stop_cancel_listener, is_cancelled

    renderer = StreamRenderer(console=console, update_interval=0.1)
    start_cancel_listener()
    try:
        stream = llm_client.generate_stream(messages, current_tools)
        text_content, tool_calls, raw_message = renderer.render(stream, agent_name="Orchestrator")
        cancelled = is_cancelled()
    finally:
        stop_cancel_listener()

    return text_content, tool_calls, raw_message, cancelled


def _is_no_model_configured_error(exc: Exception) -> bool:
    return "No model configured" in str(exc)


def agent_loop(messages: list):
    """Agent 主循环：与 LLM 交互并执行工具调用"""
    global CURRENT_CHECKPOINT
    micro_compact(messages)
    current_handlers = {
        **BASE_SUPER_TOOLS_HANDLERS,
        **GLOBAL_MCP_MANAGER.get_handlers(),
    }
    current_super_tools = []

    # Update system prompt to reflect current plan mode state
    messages[0] = {"role": "system", "content": get_dynamic_system_prompt()}

    while True:
        try:
            current_super_tools = get_current_tools_definition()
        except RuntimeError as exc:
            if _is_no_model_configured_error(exc):
                console.print(
                    "[bold yellow]⚠️ 未配置模型。请先使用 /models 命令配置模型。[/bold yellow]"
                )
                break
            raise
        _render_token_usage(
            messages,
            tools_definition=current_super_tools,
            threshold=THRESHOLD,
            estimate_tokens_fn=estimate_tokens,
        )

        try:
            text_content, tool_calls, raw_message, cancelled = _stream_with_render(messages, current_super_tools)
        except Exception as e:
            if _is_no_model_configured_error(e):
                console.print(
                    "[bold yellow]⚠️ 未配置模型。请先使用 /models 命令配置模型。[/bold yellow]"
                )
                break
            log_error_traceback("Orchestrator generation error", e)
            error_msg = f"智能体执行出错: {e}."
            console.print(f"[bold red]⚠️ {error_msg}[/bold red]")
            break

        # 用户取消：丢弃部分模型回复，不执行工具调用，回到输入等待
        if cancelled:
            break

        llm_client.append_assistant_message(messages, raw_message)
        has_tool_call = len(tool_calls) > 0

        for tc in tool_calls:
            tool_name = tc["name"]
            tool_id = tc["id"]
            tool_args = tc["arguments"]

            _render_tool_call(tool_name, tool_args)

            try:
                arguments = _parse_arguments(tool_args)
                # Plan Mode safety net: block write/execute/delegate tools
                if is_plan_mode() and tool_name in PLAN_MODE_BLOCKLIST:
                    output = (
                        f"⛔ Plan Mode active: '{tool_name}' is blocked. "
                        f"Complete your plan first, then exit Plan Mode to execute."
                    )
                elif is_plan_mode() and tool_name == "RunTerminalCommand":
                    cmd = arguments.get("command", "")
                    if is_plan_mode_command_allowed(cmd):
                        handler = current_handlers.get(tool_name)
                        output = handler(**arguments)
                    else:
                        output = (
                            f"⛔ Plan Mode: this command is not allowed. "
                            f"Only {', '.join(PLAN_MODE_ALLOWED_COMMANDS)} commands are permitted in Plan Mode."
                        )
                else:
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
        _apply_pending_title()

        if not has_tool_call:
            break

    current_context_tokens = estimate_tokens(
        messages, tools_definition=current_super_tools
    )
    if current_context_tokens > THRESHOLD:
        compact_reason = (
            f"Post agent_loop auto compact triggered: estimated tokens "
            f"{current_context_tokens} exceeded threshold {THRESHOLD}."
        )
        try:
            auto_compact(messages, reason=compact_reason, system_prompt_fn=get_dynamic_system_prompt)
            CURRENT_CHECKPOINT = save_checkpoint(messages, CURRENT_CHECKPOINT)
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

        @user_kb.add("c-p")
        def _toggle_plan_mode(event):
            from utils.plan_mode import toggle_plan_mode
            current_text = event.current_buffer.text
            new_state = toggle_plan_mode()
            if new_state:
                event.app.exit(result={"type": "plan_mode_toggle", "state": "on", "text": current_text})
            else:
                event.app.exit(result={"type": "plan_mode_toggle", "state": "off", "text": current_text})

        def prompt_continuation(width, line_number, is_soft_wrap):
            return " " * (width - 4) + " │  "

        custom_style = Style.from_dict(
            {
                "prompt": "bold #7dd3fc",
                "mode-plan": "bold #f59e0b",
                "mode-act": "bold #22c55e",
                "arrow": "#a78bfa bold",
                "bottom_toolbar": "bg:#1a1a2e fg:#e0e0e0",
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

        normalizing_buffer = False

        def _normalize_prompt_buffer(buffer):
            nonlocal normalizing_buffer
            if normalizing_buffer:
                return

            text = buffer.text
            sanitized = _sanitize_user_query(text)
            if sanitized == text:
                return

            cursor_position = len(_sanitize_user_query(text[:buffer.cursor_position]))
            normalizing_buffer = True
            try:
                buffer.text = sanitized
                buffer.cursor_position = cursor_position
            finally:
                normalizing_buffer = False

        USER_SESSION.default_buffer.on_text_changed += _normalize_prompt_buffer
    except Exception as exc:
        log_error_traceback("main init user session", exc)
        print_formatted_text(
            HTML(f"\n<ansired>初始化提示会话失败: {exc}</ansired>")
        )
        sys.exit(1)


def _sanitize_user_query(query: str) -> str:
    query = query.encode("utf-16-le", errors="surrogatepass").decode(
        "utf-16-le", errors="surrogatepass"
    )
    return query.encode("utf-8", errors="replace").decode("utf-8")


def _read_user_query(messages: list = None, default_text: str = "") -> str:
    _init_user_session()

    console.print(
        "\n[#aaaaaa]Enter 发送 · Ctrl+N 换行 · Ctrl+P 切换 Plan/Act 模式 · 输入 / 使用命令补全[/#aaaaaa]"
    )

    from utils.plan_mode import is_plan_mode as _is_plan_mode
    border_prefix = "╭─ MakeCode "
    border = border_prefix + "─" * max(1, console.size.width - len(border_prefix))
    console.print(f"[cyan]{border}[/cyan]")

    bottom_toolbar_content = None
    if messages is not None:
        tokens = estimate_tokens(
            messages,
            tools_definition=get_current_tools_definition(),
        )
        pct = (tokens / THRESHOLD) * 100
        color = "ansigreen" if pct < 70 else "ansiyellow" if pct < 90 else "ansired"
        toolbar_bg = "bg:#1a1a2e"
        bottom_toolbar_content = []

        if _is_plan_mode():
            bottom_toolbar_content.append((f"{toolbar_bg} fg:#ff8800 bold", "📋 Plan "))
        else:
            bottom_toolbar_content.append((f"{toolbar_bg} fg:#aaaaaa bold", "🎬 Act "))

        current_model = get_current_model_config()
        if current_model:
            model_text = current_model.get_display_text()
            bottom_toolbar_content.append((f"{toolbar_bg} fg:#e0e0e0 bold", f"🤖 Model: {model_text} "))

        bottom_toolbar_content.append((f"{toolbar_bg} fg:{color} bold", f"📈 Tokens: {tokens}/{THRESHOLD} ({pct:.1f}%) "))

        hitl_on = get_hitl_status()
        hitl_color = "ansigreen" if hitl_on else "ansired"
        hitl_text = "ON" if hitl_on else "OFF"
        bottom_toolbar_content.append((f"{toolbar_bg} fg:{hitl_color} bold", f"🛡️ HITL: {hitl_text}"))

    try:
        with patch_stdout():
            if _is_plan_mode():
                prompt_message = [
                    ("class:prompt", "│ "),
                    ("class:mode-plan", "PLAN "),
                    ("class:arrow", "❯ "),
                ]
            else:
                prompt_message = [
                    ("class:prompt", "│ "),
                    ("class:mode-act", "ACT "),
                    ("class:arrow", "❯ "),
                ]

            query = USER_SESSION.prompt(
                prompt_message,
                bottom_toolbar=bottom_toolbar_content,
                default=default_text,
            )
            if not isinstance(query, str):
                return query
            return _sanitize_user_query(query)
    except Exception as exc:
        log_error_traceback("main user input prompt failure", exc)
        raise


CURRENT_CHECKPOINT = None
_pending_title = None


def _apply_pending_title():
    """Apply a pending title that was generated in the background.

    Called synchronously from the main thread (agent_loop) after each
    save_checkpoint to avoid race conditions with file I/O.
    """
    global _pending_title, CURRENT_CHECKPOINT
    if _pending_title is None or CURRENT_CHECKPOINT is None:
        if _pending_title is not None and CURRENT_CHECKPOINT is None:
            _pending_title = None  # checkpoint was reset — discard pending title
        return
    title = _pending_title
    _pending_title = None
    try:
        new_ckpt = rename_checkpoint_with_title(CURRENT_CHECKPOINT, title)
        if new_ckpt != CURRENT_CHECKPOINT:
            CURRENT_CHECKPOINT = new_ckpt
        _tasks_module.TASK_MANAGER.rename_with_title(title)
        TEAM.rename_history_with_title(title)
    except Exception as exc:
        log_error_traceback("Failed to apply pending title", exc)



def _background_update_check():
    """后台检查更新，有新版本时提示用户（不阻塞启动）。"""
    try:
        version_info = check_update()
        if not version_info:
            return
        new_version = version_info.get('version', '未知')
        release_log = version_info.get('release_log', '')
        post_tui(TuiRegion.BACKGROUND, f"[bold yellow]📢 发现新版本 v{new_version}，输入 /update 查看详情并更新[/bold yellow]")
        if release_log:
            post_tui(TuiRegion.BACKGROUND, Markdown(release_log))
    except Exception:
        pass  # 静默失败，不影响正常使用


def _process_user_query(query: str, history: list, command_handler: CommandHandler) -> str | None:
    global CURRENT_CHECKPOINT, _pending_title

    query = query.strip()
    if not query:
        return None

    command_result = command_handler.process_command(
        query=query,
        history=history,
        current_checkpoint=CURRENT_CHECKPOINT,
        render_banner_fn=_render_startup_banner,
        render_hint_fn=_render_env_customization_hint,
        render_history_fn=_render_history,
    )

    if command_result.action == CommandAction.EXIT:
        return "exit"
    if command_result.action == CommandAction.CONTINUE:
        return None
    if command_result.action == CommandAction.RUN_AGENT:
        history.append({"role": "user", "content": command_result.payload})

        if CURRENT_CHECKPOINT is None and any(msg['role'] == 'user' for msg in history):
            CURRENT_CHECKPOINT = save_checkpoint(history)

            def _title_worker():
                global _pending_title
                post_tui(TuiRegion.BACKGROUND, "[#aaaaaa]🏷️ 正在生成对话标题...[/#aaaaaa]")
                try:
                    title = generate_title(query)
                    if title:
                        _pending_title = title
                        post_tui(TuiRegion.BACKGROUND, f"[bold green]🏷️ 对话标题生成完成：{title}[/bold green]")
                    else:
                        post_tui(TuiRegion.BACKGROUND, "[#aaaaaa]🏷️ 对话标题生成结束：未生成可用标题[/#aaaaaa]")
                except Exception as exc:
                    log_error_traceback("Failed to generate title", exc)
                    post_tui(TuiRegion.BACKGROUND, f"[bold red]🏷️ 对话标题生成失败：{exc}[/bold red]")

            _title_thread = threading.Thread(target=_title_worker, daemon=True)
            _title_thread.start()
        else:
            _title_thread = None

        try:
            set_agent_loop_active(True)
            agent_loop(history)
        except RuntimeError as exc:
            console.print(f"[bold yellow]⚠️ {exc}[/bold yellow]")
        finally:
            set_agent_loop_active(False)
        if _title_thread is not None:
            _title_thread.join(timeout=10)
        _apply_pending_title()
        return None
    if command_result.action == CommandAction.RESET_CHECKPOINT:
        CURRENT_CHECKPOINT = None
        _pending_title = None
    elif command_result.action == CommandAction.LOAD_HISTORY:
        history[:], CURRENT_CHECKPOINT = command_result.payload
        _pending_title = None
    elif command_result.action == CommandAction.UPDATE_CHECKPOINT:
        CURRENT_CHECKPOINT = command_result.payload
    elif command_result.action == CommandAction.UPDATE_SYSTEM_PROMPT:
        history[0] = {"role": "system", "content": command_result.payload}
    return None


def _run_textual_main(history: list, command_handler: CommandHandler) -> None:
    def submit_handler(query: str) -> str | None:
        return _process_user_query(query, history, command_handler)

    def runtime_info_provider() -> str:
        tokens = estimate_tokens(
            history,
            tools_definition=get_current_tools_definition(),
        )
        return format_runtime_info(tokens, THRESHOLD)

    app = MakeCodeTuiApp(
        submit_handler=submit_handler,
        runtime_info_provider=runtime_info_provider,
    )
    app.run()


if __name__ == "__main__":
    _render_startup_banner()
    _render_env_customization_hint()

    # 后台检查更新（仅打包环境）
    if getattr(sys, 'frozen', False):
        threading.Thread(target=_background_update_check, daemon=True).start()

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
        _run_textual_main(history, command_handler)
    finally:
        GLOBAL_MCP_MANAGER.stop()
