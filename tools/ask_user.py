"""
主动向用户提问工具 — 允许 Agent 在不确定时主动询问用户意见。
"""
import json

from openai import pydantic_function_tool
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.styles import Style
from typing import Any

from pydantic import BaseModel, Field, field_validator
from rich.panel import Panel
from rich.text import Text

from system.console_render import console_lock, console


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

        # Render info panel first (like HITL does), so context is visible
        panel_text = Text()
        panel_text.append(f"❓ {question}")
        if parsed_options:
            panel_text.append("\n\n")
            for i, opt in enumerate(parsed_options):
                marker = "⭐ " if opt.is_recommended else ""
                panel_text.append(f"  {marker}{opt.content}")
                if opt.is_recommended:
                    panel_text.append(" （推荐）", style="bold green")
                panel_text.append("\n")
        console.print(Panel(
            panel_text,
            title="❓ Agent 请你做出选择",
            border_style="cyan",
            expand=False,
        ))

        selected_index = [0]

        kb = KeyBindings()

        @kb.add("up")
        def _go_up(event):
            selected_index[0] = max(0, selected_index[0] - 1)

        @kb.add("down")
        def _go_down(event):
            selected_index[0] = min(len(entries) - 1, selected_index[0] + 1)

        @kb.add("enter")
        def _confirm(event):
            event.app.exit(result=entries[selected_index[0]][0])

        @kb.add("c-c")
        def _cancel(event):
            event.app.exit(result="abort")

        def get_formatted_text():
            result = [("class:title", "\n请使用 ↑/↓ 选择，Enter 确认:\n")]
            for i, (key, text) in enumerate(entries):
                if i == selected_index[0]:
                    result.append(("class:selected", f"👉 [{key}] {text}\n"))
                else:
                    result.append(("class:unselected", f"   [{key}] {text}\n"))
            return result

        control = FormattedTextControl(get_formatted_text)
        window = Window(content=control, height=len(entries) + 2)
        layout = Layout(window)

        style = Style(
            [
                ("title", "fg:ansicyan bold"),
                ("selected", "fg:ansigreen bold"),
                ("unselected", "fg:ansigray"),
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
