"""
主动向用户提问工具 — 允许 Agent 在不确定时主动询问用户意见。
"""
import json
import shutil

from openai import pydantic_function_tool
from prompt_toolkit import prompt
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.styles import Style
from typing import Any

from pydantic import BaseModel, Field, field_validator
from system.console_render import console_lock


class Option(BaseModel):
    """A single option presented to the user."""
    content: str = Field(..., description="The text content of this option.")
    is_recommended: bool = Field(
        default=False,
        description="Whether this option is recommended by the agent.",
    )


class AskUser(BaseModel):
    """
    Proactively ask the user a question and wait for their response.

    WHEN TO USE:
    - The requirement is ambiguous and you need clarification
    - There are multiple valid approaches and you need the user to choose
    - A decision requires user preference or domain knowledge

    BEHAVIOR:
    - Presents the question and options in an interactive panel
    - The user can select a listed option
    - Returns a JSON string with the user's choice
    """
    question: str = Field(
        ...,
        description="The question or message to present to the user.",
    )
    options: list[Option] = Field(
        ...,
        min_length=1,
        description="List of options for the user to choose from.",
    )

    @field_validator("options", mode="before")
    @classmethod
    def parse_stringified_options(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                raise ValueError("options must be a non-empty list")
            if v.lower() in {"none", "null"}:
                raise ValueError("options must be a non-empty list")
            if v == "[]":
                raise ValueError("options must contain at least one option")
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return v
        return v


def ask_user(question: str, options: list, **kwargs) -> str:
    """Handler for the AskUser tool."""
    try:
        validated = AskUser.model_validate({"question": question, "options": options})
        question = validated.question
        parsed_options = validated.options
    except Exception as exc:
        return f"Error: Invalid arguments provided to AskUser. {exc}"

    with console_lock:
        # Build display entries from options
        entries: list[tuple[str, str]] = []  # (key, display)
        for i, opt in enumerate(parsed_options):
            label = f"⭐ {opt.content} （推荐）" if opt.is_recommended else opt.content
            entries.append((str(i + 1), label))

        custom_key = str(len(parsed_options) + 1)
        entries.append((custom_key, "✏️ 自定义输入"))

        selected_index = [0]
        scroll_offset = [0]  # 视口偏移，支持列表滚动

        def _calc_max_visible():
            """根据终端高度计算可视区域最大选项数"""
            term_height = shutil.get_terminal_size().lines
            # 问题最多占终端高度的1/3，加上提示文字(1行) + 指示符(最多2行) + 边距(2行)
            question_lines = min(len(question.split('\n')), term_height // 3)
            return max(3, term_height - question_lines - 5)

        def _adjust_scroll():
            """确保选中项始终在视口内"""
            max_vis = _calc_max_visible()
            if selected_index[0] < scroll_offset[0]:
                scroll_offset[0] = selected_index[0]
            elif selected_index[0] >= scroll_offset[0] + max_vis:
                scroll_offset[0] = selected_index[0] - max_vis + 1

        kb = KeyBindings()

        @kb.add("up")
        def _go_up(event):
            if selected_index[0] > 0:
                selected_index[0] -= 1
                _adjust_scroll()

        @kb.add("down")
        def _go_down(event):
            if selected_index[0] < len(entries) - 1:
                selected_index[0] += 1
                _adjust_scroll()

        @kb.add("enter")
        def _confirm(event):
            event.app.exit(result=entries[selected_index[0]][0])

        @kb.add("c-c")
        def _cancel(event):
            event.app.exit(result="abort")

        def get_formatted_text():
            # 限制问题文本显示行数，确保选项总能显示
            term_height = shutil.get_terminal_size().lines
            max_question_lines = max(2, term_height // 3)  # 问题最多占终端高度的1/3
            question_lines = question.split('\n')[:max_question_lines]
            question_display = '\n'.join(question_lines)
            if len(question.split('\n')) > max_question_lines:
                question_display += '...'

            result = [("class:question", f"\n❓ {question_display}\n"), ("class:title", "\n请使用 ↑/↓ 选择，Enter 确认:\n")]
            max_vis = _calc_max_visible()
            start = scroll_offset[0]
            end = min(start + max_vis, len(entries))

            if start > 0:
                result.append(("class:indicator", f"     ⬆ ... 还有 {start} 项未显示\n"))

            for i in range(start, end):
                key, text = entries[i]
                if i == selected_index[0]:
                    result.append(("class:selected", f"👉 [{key}] {text}\n"))
                else:
                    result.append(("class:unselected", f"   [{key}] {text}\n"))

            if end < len(entries):
                remaining = len(entries) - end
                result.append(("class:indicator", f"     ⬇ ... 还有 {remaining} 项未显示\n"))

            return result

        control = FormattedTextControl(get_formatted_text)
        window = Window(content=control, wrap_lines=True)
        layout = Layout(window, focused_element=window)

        style = Style(
            [
                ("question", "fg:ansiyellow bold"),
                ("title", "fg:ansicyan bold"),
                ("selected", "fg:ansigreen bold"),
                ("unselected", "fg:ansigray"),
                ("indicator", "fg:ansiyellow"),
            ]
        )

        app = Application(
            layout=layout,
            key_bindings=kb,
            style=style,
            erase_when_done=True,
        )
        choice = app.run()

    # Handle result
    if choice == "abort":
        return json.dumps({"choice": "<cancelled>"}, ensure_ascii=False)

    # Custom input
    if choice == custom_key:
        try:
            custom_text = prompt("请输入你的回答：").strip()
        except KeyboardInterrupt:
            return json.dumps({"choice": "<cancelled>"}, ensure_ascii=False)
        except EOFError:
            return json.dumps({"choice": "<cancelled>"}, ensure_ascii=False)
        if not custom_text:
            return json.dumps({"choice": "<empty_input>"}, ensure_ascii=False)
        return json.dumps({"choice": custom_text, "custom": True}, ensure_ascii=False)

    # Selected a listed option — find the content
    idx = int(choice) - 1
    if 0 <= idx < len(parsed_options):
        return json.dumps({"choice": parsed_options[idx].content}, ensure_ascii=False)

    return json.dumps({"choice": "<unknown>"}, ensure_ascii=False)


ASK_USER_TOOLS = [
    pydantic_function_tool(AskUser)
]

ASK_USER_TOOLS_HANDLERS = {
    "AskUser": ask_user,
}
