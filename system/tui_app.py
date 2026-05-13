from __future__ import annotations

import json
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from enum import StrEnum
from queue import Queue
from typing import Any

from rich.console import RenderableType
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Key, Resize
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, ListItem, ListView, RichLog, Static, TextArea

from init import INSTALL_MAKECODE_DIR
from settings import KEEP_RECENT_TOOL_CALL


class TuiRegion(StrEnum):
    CONTENT = "content"
    REASONING = "reasoning"
    TOOLS = "tools"
    BACKGROUND = "background"
    SUB_AGENT = "sub_agent"
    STATUS = "status"
    RUNTIME_INFO = "runtime_info"


@dataclass(frozen=True)
class TuiEvent:
    region: TuiRegion
    payload: Any
    clear: bool = False
    tool_result_delta: int = 0
    reset_tool_result_count: bool = False
    tail: bool = False
    active: bool | None = None


LAYOUT_CONFIG_FILE = INSTALL_MAKECODE_DIR / "layout_config.json"
LAYOUT_DEFAULT_RATIOS: dict[str, int] = {
    "content": 1,
    "tools": 1,
    "reasoning": 2,
    "background": 2,
    "sub_agent": 1,
}
LAYOUT_LEFT_KEYS = ("content", "tools")
LAYOUT_RIGHT_KEYS = ("reasoning", "background", "sub_agent")


def normalize_layout_ratios(data: Any) -> dict[str, int]:
    source = data if isinstance(data, dict) else {}
    ratios: dict[str, int] = {}
    for key, default in LAYOUT_DEFAULT_RATIOS.items():
        try:
            value = int(source.get(key, default))
        except (TypeError, ValueError):
            value = default
        ratios[key] = min(max(value, 0), 10)
    return ratios


def load_layout_ratios() -> dict[str, int]:
    try:
        data = json.loads(LAYOUT_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(LAYOUT_DEFAULT_RATIOS)
    return normalize_layout_ratios(data)


def save_layout_ratios(ratios: dict[str, int]) -> None:
    LAYOUT_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAYOUT_CONFIG_FILE.write_text(
        json.dumps(normalize_layout_ratios(ratios), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


class TuiBridge:
    def __init__(self) -> None:
        self._app: MakeCodeTuiApp | None = None
        self._app_thread_id: int | None = None
        self._pending: Queue[TuiEvent] = Queue()
        self._lock = threading.Lock()

    def bind(self, app: "MakeCodeTuiApp") -> None:
        with self._lock:
            self._app = app
            self._app_thread_id = threading.get_ident()
            pending: list[TuiEvent] = []
            while not self._pending.empty():
                pending.append(self._pending.get())
        for event in pending:
            self._dispatch_event(app, event)

    def unbind(self, app: "MakeCodeTuiApp") -> None:
        with self._lock:
            if self._app is app:
                self._app = None
                self._app_thread_id = None

    def post(
        self,
        region: TuiRegion | str,
        payload: Any = None,
        *,
        clear: bool = False,
        tool_result_delta: int = 0,
        reset_tool_result_count: bool = False,
        tail: bool = False,
        active: bool | None = None,
    ) -> None:
        event = TuiEvent(TuiRegion(region), payload, clear, tool_result_delta, reset_tool_result_count, tail, active)
        with self._lock:
            app = self._app
            if app is None:
                self._pending.put(event)
                return
        self._dispatch_event(app, event)

    def choose(self, title: str, options: list[str], *, allow_custom: bool = False) -> str:
        with self._lock:
            app = self._app
        if app is None:
            return "<cancelled>"
        future: Future[str] = Future()
        if self._is_app_thread():
            app.open_choice_modal(title, options, allow_custom, future)
        else:
            app.call_from_thread(app.open_choice_modal, title, options, allow_custom, future)
        return future.result()

    def choose_add_model(self) -> dict[str, str] | None:
        with self._lock:
            app = self._app
        if app is None:
            return None
        future: Future[dict[str, str] | None] = Future()
        if self._is_app_thread():
            app.open_add_model_modal(future)
        else:
            app.call_from_thread(app.open_add_model_modal, future)
        return future.result()

    def choose_mcp_switch(self, server_switches: list[dict[str, Any]]) -> str | dict:
        with self._lock:
            app = self._app
        if app is None:
            return {"action": "cancel"}
        future: Future[str | dict] = Future()
        if self._is_app_thread():
            app.open_mcp_switch_modal(server_switches, future)
        else:
            app.call_from_thread(app.open_mcp_switch_modal, server_switches, future)
        return future.result()

    def manage_models(self, model_manager: Any) -> str:
        with self._lock:
            app = self._app
        if app is None:
            return "<cancelled>"
        future: Future[str] = Future()
        if self._is_app_thread():
            app.open_model_manager_modal(model_manager, future)
        else:
            app.call_from_thread(app.open_model_manager_modal, model_manager, future)
        return future.result()

    def manage_layout(self) -> str | dict[str, int]:
        with self._lock:
            app = self._app
        if app is None:
            return "<cancelled>"
        future: Future[str | dict[str, int]] = Future()
        if self._is_app_thread():
            app.open_layout_modal(future)
        else:
            app.call_from_thread(app.open_layout_modal, future)
        return future.result()

    def manage_memories(self, memory_provider: Any) -> list[str]:
        with self._lock:
            app = self._app
        if app is None:
            return []
        future: Future[list[str]] = Future()
        if self._is_app_thread():
            app.open_memory_panel_modal(memory_provider, future)
        else:
            app.call_from_thread(app.open_memory_panel_modal, memory_provider, future)
        return future.result()

    def _dispatch_event(self, app: "MakeCodeTuiApp", event: TuiEvent) -> None:
        if self._is_app_thread():
            app.handle_tui_event(event)
        else:
            app.call_from_thread(app.handle_tui_event, event)

    def _is_app_thread(self) -> bool:
        with self._lock:
            return self._app_thread_id == threading.get_ident()

    def set_agent_loop_active(self, active: bool) -> None:
        with self._lock:
            app = self._app
        if app is None:
            return
        if self._is_app_thread():
            app.set_agent_loop_active(active)
        else:
            app.call_from_thread(app.set_agent_loop_active, active)

    def refresh_status(self) -> None:
        with self._lock:
            app = self._app
        if app is None:
            return
        if self._is_app_thread():
            app.refresh_status()
        else:
            app.call_from_thread(app.refresh_status)


TUI_BRIDGE = TuiBridge()


class ChoiceModal(ModalScreen[str]):
    CSS = """
    ChoiceModal, ModelPanelModal, McpSwitchModal, ModelManagerModal, AddModelModal, LayoutModal, MemoryPanelModal {
        align: center middle;
    }

    #choice-dialog {
        width: 70%;
        height: auto;
        max-height: 80%;
        border: round #f59e0b;
        background: $surface;
        padding: 1 2;
    }

    #layout-dialog {
        width: 76;
        height: auto;
        border: round #f59e0b;
        background: $surface;
        padding: 1 2;
    }

    #memory-dialog {
        width: 88%;
        height: auto;
        max-height: 86%;
        border: round #f59e0b;
        background: $surface;
        padding: 1 2;
    }

    #memory-list {
        height: 12;
        max-height: 12;
    }

    #memory-detail {
        height: 12;
        min-height: 1;
        margin-top: 1;
        border: round #3b82f6;
        padding: 0 1;
    }

    #layout-columns {
        height: auto;
    }

    .layout-column {
        width: 1fr;
        height: auto;
    }

    .layout-button {
        width: 100%;
        margin: 0 1;
    }

    #layout-actions {
        height: 3;
        margin-top: 1;
    }

    .layout-action-button {
        width: 1fr;
        margin: 0 1;
    }

    #choice-title {
        height: auto;
        margin-bottom: 1;
    }

    #choice-list {
        height: auto;
        max-height: 16;
    }

    #custom-input {
        margin-top: 1;
    }

    #custom-actions {
        height: 3;
        margin-top: 1;
    }

    #custom-cancel {
        width: 14;
    }

    #model-form-dialog {
        width: 72;
        height: auto;
        border: round #f59e0b;
        background: $surface;
        padding: 1 2;
    }

    .model-form-label {
        height: 1;
        margin-top: 1;
    }

    .model-form-input {
        margin-top: 0;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("q", "cancel", "Cancel", priority=True),
        Binding("enter", "confirm", "Confirm", priority=True),
    ]

    def __init__(self, title: str, options: list[str], allow_custom: bool = False) -> None:
        super().__init__()
        self._title = title
        self._options = options
        self._allow_custom = allow_custom

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-dialog"):
            yield Label(self._title, id="choice-title")
            if self._options:
                yield ListView(*[ListItem(Label(option)) for option in self._options], id="choice-list")
            if self._allow_custom:
                yield Input(placeholder="自定义输入，Enter 提交", id="custom-input")
                with Horizontal(id="custom-actions"):
                    yield Button("取消", id="custom-cancel", variant="warning")

    def on_mount(self) -> None:
        if self._options:
            choice_list = self.query_one("#choice-list", ListView)
            choice_list.index = 0
            choice_list.focus()
        elif self._allow_custom:
            self.query_one("#custom-input", Input).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(self._options[event.list_view.index or 0])

    def _on_key(self, event: Key) -> None:
        if event.key == "enter":
            self.action_confirm()
            event.stop()
            event.prevent_default()
            return
        if event.key == "escape":
            self.action_cancel()
            event.stop()
            event.prevent_default()
            return
        if event.key == "q" and not isinstance(self.focused, Input):
            self.action_cancel()
            event.stop()
            event.prevent_default()
            return

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self.dismiss(value if value else "<empty_input>")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "custom-cancel":
            self.action_cancel()
            return

    def action_confirm(self) -> None:
        focused = self.focused
        if isinstance(focused, Input):
            value = focused.value.strip()
            self.dismiss(value if value else "<empty_input>")
            return
        if self._options:
            choice_list = self.query_one("#choice-list", ListView)
            index = choice_list.index if choice_list.index is not None else 0
            self.dismiss(self._options[index])
        elif self._allow_custom:
            value = self.query_one("#custom-input", Input).value.strip()
            self.dismiss(value if value else "<empty_input>")

    def action_cancel(self) -> None:
        self.dismiss("<cancelled>")


class McpSwitchModal(ModalScreen[str | dict]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("enter", "confirm_or_toggle", "Toggle/Confirm", priority=True),
        Binding("space", "toggle", "Toggle", priority=True),
    ]

    def __init__(self, server_switches: list[dict[str, Any]]) -> None:
        super().__init__()
        self._server_switches = server_switches
        self._draft_states = {item["name"]: bool(item["disabled"]) for item in server_switches}

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-dialog"):
            yield Label("🔀 MCP 服务开关面板\n选择服务可切换启用/禁用；选择确认应用保存。", id="choice-title")
            yield ListView(*[ListItem(Label(label)) for label in self._labels()], id="choice-list")

    def on_mount(self) -> None:
        choice_list = self.query_one("#choice-list", ListView)
        choice_list.index = 0
        choice_list.focus()

    def _labels(self) -> list[str]:
        choices = []
        for item in self._server_switches:
            choices.append(self._server_label(item))
        choices.extend(["确认应用", "取消"])
        return choices

    def _server_label(self, item: dict[str, Any]) -> str:
        name = item["name"]
        enabled = not self._draft_states[name]
        loaded = item.get("loaded", False)
        switch_box = "[√]" if enabled else "[×]"
        runtime_txt = "已加载" if loaded else "未加载"
        status_txt = "启用" if enabled else "禁用"
        return f"{switch_box} {name}    当前草稿: {status_txt}    运行态: {runtime_txt}"

    def _selected_index(self) -> int:
        choice_list = self.query_one("#choice-list", ListView)
        return choice_list.index if choice_list.index is not None else 0

    def _refresh_server_row(self, index: int) -> None:
        choice_list = self.query_one("#choice-list", ListView)
        label = choice_list.children[index].query_one(Label)
        label.update(self._server_label(self._server_switches[index]))
        choice_list.index = index
        choice_list.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.action_confirm_or_toggle()

    def _on_key(self, event: Key) -> None:
        if event.key == "enter":
            self.action_confirm_or_toggle()
            event.stop()
            event.prevent_default()
            return
        if event.key == "space":
            self.action_toggle()
            event.stop()
            event.prevent_default()
            return

    def action_confirm_or_toggle(self) -> None:
        index = self._selected_index()
        if index < len(self._server_switches):
            self._toggle_index(index)
            return
        if index == len(self._server_switches):
            self.dismiss({"action": "confirm", "disabled_updates": dict(self._draft_states)})
            return
        self.dismiss({"action": "cancel"})

    def action_toggle(self) -> None:
        index = self._selected_index()
        if index < len(self._server_switches):
            self._toggle_index(index)

    def _toggle_index(self, index: int) -> None:
        name = self._server_switches[index]["name"]
        self._draft_states[name] = not self._draft_states[name]
        self._refresh_server_row(index)


class ModelPanelModal(ModalScreen[str]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("enter", "select", "Select", priority=True),
        Binding("f", "favorite", "Favorite", priority=True),
        Binding("d", "delete", "Delete", priority=True),
    ]

    def __init__(self, title: str, options: list[str]) -> None:
        super().__init__()
        self._title = title
        self._options = options

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-dialog"):
            yield Label(self._title, id="choice-title")
            yield ListView(*[ListItem(Label(option)) for option in self._options], id="choice-list")

    def on_mount(self) -> None:
        choice_list = self.query_one("#choice-list", ListView)
        choice_list.index = 0
        choice_list.focus()

    def _selected_index(self) -> int:
        choice_list = self.query_one("#choice-list", ListView)
        return choice_list.index if choice_list.index is not None else 0

    def _dismiss_action(self, action: str) -> None:
        self.dismiss(f"{action}:{self._selected_index()}")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.action_select()

    def _on_key(self, event: Key) -> None:
        if event.key == "enter":
            self.action_select()
            event.stop()
            event.prevent_default()
            return
        if event.key == "f":
            self.action_favorite()
            event.stop()
            event.prevent_default()
            return
        if event.key == "d":
            self.action_delete()
            event.stop()
            event.prevent_default()
            return

    def action_select(self) -> None:
        self._dismiss_action("select")

    def action_favorite(self) -> None:
        self._dismiss_action("favorite")

    def action_delete(self) -> None:
        self._dismiss_action("delete")


class ModelManagerModal(ModalScreen[str]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("enter", "select", "Select", priority=True),
        Binding("q", "close", "Close", priority=True),
        Binding("f", "favorite", "Favorite", priority=True),
        Binding("d", "delete", "Delete", priority=True),
        Binding("y", "confirm_delete", "Confirm Delete", priority=True),
        Binding("n", "cancel_delete", "Cancel Delete", priority=True),
    ]

    def __init__(self, model_manager: Any) -> None:
        super().__init__()
        self._model_manager = model_manager
        self._model_keys: list[tuple[str, str, str] | None] = []
        self._pending_delete_key: tuple[str, str, str] | None = None
        self._pending_delete_index: int | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-dialog"):
            yield Label("⚙️ 模型管理面板\nEnter 选择当前模型并关闭；f 切换常用；d 删除；选择添加模型可新增配置；q 关闭。", id="choice-title")
            yield ListView(id="choice-list")

    def on_mount(self) -> None:
        self._reload_rows(0)

    def _selected_index(self) -> int:
        choice_list = self.query_one("#choice-list", ListView)
        return choice_list.index if choice_list.index is not None else 0

    def _model_label(self, model: Any, current_key: tuple[str, str, str] | None) -> str:
        markers = []
        if model.key == current_key:
            markers.append("✓")
        if model.is_favorite:
            markers.append("♥")
        marker_text = " ".join(markers) if markers else " "
        return f"[{marker_text:^3}] {model.get_display_text()}"

    def _reload_rows(self, selected_index: int | None = None) -> None:
        self._pending_delete_key = None
        self._pending_delete_index = None
        self._reset_title()
        self._model_manager._reload_from_disk()
        current_model = self._model_manager.get_current_model()
        current_key = current_model.key if current_model else None
        labels = ["➕ 添加模型"]
        keys: list[tuple[str, str, str] | None] = [None]
        for model in self._model_manager.models:
            labels.append(self._model_label(model, current_key))
            keys.append(model.key)
        labels.append("退出")
        keys.append(None)
        self._model_keys = keys

        choice_list = self.query_one("#choice-list", ListView)
        choice_list.clear()

        def _mount_rows() -> None:
            choice_list.extend(ListItem(Label(label)) for label in labels)
            max_index = max(len(labels) - 1, 0)
            choice_list.index = min(selected_index or 0, max_index)
            choice_list.focus()

        self.call_after_refresh(_mount_rows)

    def _refresh_model_row_by_key(self, model_key: tuple[str, str, str]) -> None:
        index = next((index for index, key in enumerate(self._model_keys) if key == model_key), None)
        if index is None:
            return
        model = next((model for model in self._model_manager.models if model.key == model_key), None)
        if model is None:
            return
        current_model = self._model_manager.get_current_model()
        current_key = current_model.key if current_model else None
        choice_list = self.query_one("#choice-list", ListView)
        label = choice_list.children[index].query_one(Label)
        label.update(self._model_label(model, current_key))
        choice_list.index = index
        choice_list.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.action_select()

    def _on_key(self, event: Key) -> None:
        if event.key == "enter":
            self.action_select()
            event.stop()
            event.prevent_default()
            return
        if event.key == "q":
            self.action_close()
            event.stop()
            event.prevent_default()
            return
        if event.key == "f":
            self.action_favorite()
            event.stop()
            event.prevent_default()
            return
        if event.key == "d":
            self.action_delete()
            event.stop()
            event.prevent_default()
            return
        if event.key == "y":
            self.action_confirm_delete()
            event.stop()
            event.prevent_default()
            return
        if event.key == "n":
            self.action_cancel_delete()
            event.stop()
            event.prevent_default()
            return

    def action_select(self) -> None:
        index = self._selected_index()
        if index == 0:
            self._add_model(index)
            return
        if index == len(self._model_keys) - 1:
            self.dismiss("exit")
            return
        model_key = self._model_keys[index]
        if model_key is None:
            return
        target_index = self._target_index(model_key)
        if target_index is None:
            self._reload_rows(index)
            return
        selected_model = self._model_manager.models[target_index]
        if self._model_manager.set_current_model_by_index(target_index):
            self.dismiss(f"selected:{selected_model.get_display_text()}")

    def action_favorite(self) -> None:
        index = self._selected_index()
        if index == 0 or index == len(self._model_keys) - 1:
            return
        model_key = self._model_keys[index]
        if model_key is None:
            return
        target_index = self._target_index(model_key)
        if target_index is None:
            self._reload_rows(index)
            return
        if self._model_manager.toggle_favorite_by_index(target_index):
            self._refresh_model_row_by_key(model_key)

    def action_delete(self) -> None:
        index = self._selected_index()
        if index == 0 or index == len(self._model_keys) - 1:
            return
        model_key = self._model_keys[index]
        if model_key is None:
            return
        target_index = self._target_index(model_key)
        if target_index is None:
            self._reload_rows(index)
            return
        selected_model = self._model_manager.models[target_index]
        self._pending_delete_key = model_key
        self._pending_delete_index = index
        self.query_one("#choice-title", Label).update(
            "⚠️ 确认删除模型？\n"
            f"{selected_model.get_display_text()}\n"
            "按 y 确认删除，按 n 取消。"
        )

    def action_confirm_delete(self) -> None:
        if self._pending_delete_key is None:
            return
        selected_index = self._pending_delete_index or self._selected_index()
        self._model_manager.delete_model_by_key(self._pending_delete_key)
        self._reload_rows(selected_index)

    def action_cancel_delete(self) -> None:
        selected_index = self._pending_delete_index or self._selected_index()
        self._pending_delete_key = None
        self._pending_delete_index = None
        self._reset_title()
        choice_list = self.query_one("#choice-list", ListView)
        choice_list.index = selected_index
        choice_list.focus()

    def _reset_title(self) -> None:
        self.query_one("#choice-title", Label).update(
            "⚙️ 模型管理面板\nEnter 选择当前模型并关闭；f 切换常用；d 删除；选择添加模型可新增配置；q 关闭。"
        )

    def action_close(self) -> None:
        self.dismiss("exit")

    def _add_model(self, selected_index: int) -> None:
        self.app.push_screen(AddModelModal(), lambda model_config: self._finish_add_model(model_config, selected_index))

    def _finish_add_model(self, model_config: dict[str, str] | None, selected_index: int) -> None:
        if model_config is None:
            self._reload_rows(selected_index)
            return
        model_ids = [
            item.strip()
            for item in model_config["model_input"].replace("，", ",").split(",")
            if item.strip()
        ]
        self._model_manager.add_model(model_config["base_url"], model_config["api_key"], model_ids)
        self._reload_rows(selected_index)

    def _target_index(self, model_key: tuple[str, str, str]) -> int | None:
        return next(
            (index for index, model in enumerate(self._model_manager.models) if model.key == model_key),
            None,
        )


class MemoryPanelModal(ModalScreen[list[str]]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("q", "close", "Close", priority=True),
        Binding("enter", "toggle_detail", "Details", priority=True),
        Binding("space", "toggle_detail", "Details", priority=True),
        Binding("d", "delete", "Delete", priority=True),
        Binding("y", "confirm_delete", "Confirm Delete", priority=True),
        Binding("n", "cancel_delete", "Cancel Delete", priority=True),
    ]

    def __init__(self, memory_provider: Any) -> None:
        super().__init__()
        self._memory_provider = memory_provider
        self._memories: list[dict[str, Any]] = []
        self._expanded_id: str | None = None
        self._pending_delete_id: str | None = None
        self._pending_delete_index: int | None = None
        self._deleted_ids: list[str] = []

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="memory-dialog"):
            yield Label("🧠 长期记忆面板 (active: 0)\nEnter/Space 查看详情；d 删除；q 关闭。", id="choice-title")
            yield ListView(id="memory-list")
            yield RichLog(id="memory-detail", markup=True, wrap=True, min_width=1)

    def on_mount(self) -> None:
        self._reload_rows(0)

    def _selected_index(self) -> int:
        choice_list = self.query_one("#memory-list", ListView)
        return choice_list.index if choice_list.index is not None else 0

    def _memory_label(self, item: dict[str, Any]) -> str:
        memory_id = item.get("id", "")
        marker = "▼" if memory_id == self._expanded_id else " "
        category = item.get("category", "")
        updated_at = item.get("updated_at", "")
        insight = str(item.get("insight", "")).replace("\n", " ")
        if len(insight) > 72:
            insight = f"{insight[:69]}..."
        return f"[{marker}] {memory_id} · {category} · {updated_at}\n    {insight}"

    def _reload_rows(self, selected_index: int | None = None) -> None:
        self._pending_delete_id = None
        self._pending_delete_index = None
        self._memories = list(self._memory_provider.list_long_term_memories())
        self._reset_title()
        choice_list = self.query_one("#memory-list", ListView)
        choice_list.clear()

        labels = [self._memory_label(item) for item in self._memories] or ["暂无长期记忆"]

        def _mount_rows() -> None:
            choice_list.extend(ListItem(Label(label)) for label in labels)
            max_index = max(len(labels) - 1, 0)
            choice_list.index = min(selected_index or 0, max_index)
            choice_list.focus()
            self._update_detail()

        self.call_after_refresh(_mount_rows)

    def _title_text(self) -> str:
        return f"🧠 长期记忆面板 (active: {len(self._memories)})\nEnter/Space 查看详情；d 删除；q 关闭。"

    def _reset_title(self) -> None:
        self.query_one("#choice-title", Label).update(self._title_text())

    def _current_memory(self) -> dict[str, Any] | None:
        if not self._memories:
            return None
        index = self._selected_index()
        if index >= len(self._memories):
            return None
        return self._memories[index]

    def _update_detail(self) -> None:
        detail = self.query_one("#memory-detail", RichLog)
        detail.clear()
        current = self._current_memory()
        if current is None:
            detail.write("暂无详情。", expand=True, shrink=True)
            return
        if current.get("id") != self._expanded_id:
            detail.write("选中记忆后按 Enter/Space 查看详情。", expand=True, shrink=True)
            return
        detail.write(
            "\n".join(
                [
                    f"ID: {current.get('id', '')}",
                    f"Category: {current.get('category', '')}",
                    f"Updated: {current.get('updated_at', '')}",
                    "",
                    f"Insight:\n{current.get('insight', '')}",
                    "",
                    f"Evidence:\n{current.get('evidence', '')}",
                    "",
                    f"Reuse condition:\n{current.get('reuse_condition', '')}",
                ]
            ),
            expand=True,
            shrink=True,
            scroll_end=False,
        )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.action_toggle_detail()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        self._update_detail()

    def _on_key(self, event: Key) -> None:
        key_actions = {
            "enter": self.action_toggle_detail,
            "space": self.action_toggle_detail,
            "d": self.action_delete,
            "y": self.action_confirm_delete,
            "n": self.action_cancel_delete,
            "q": self.action_close,
        }
        action = key_actions.get(event.key)
        if action is None:
            return
        action()
        event.stop()
        event.prevent_default()

    def action_toggle_detail(self) -> None:
        if self._pending_delete_id is not None:
            return
        current = self._current_memory()
        if current is None:
            return
        memory_id = current.get("id")
        self._expanded_id = None if self._expanded_id == memory_id else memory_id
        index = self._selected_index()
        choice_list = self.query_one("#memory-list", ListView)
        label = choice_list.children[index].query_one(Label)
        label.update(self._memory_label(current))
        self._update_detail()
        choice_list.index = index
        choice_list.focus()

    def action_delete(self) -> None:
        current = self._current_memory()
        if current is None:
            return
        self._pending_delete_id = current.get("id")
        self._pending_delete_index = self._selected_index()
        self.query_one("#choice-title", Label).update(
            "⚠️ 确认删除长期记忆？\n"
            f"{self._pending_delete_id}\n"
            "按 y 确认删除，按 n 取消。"
        )

    def action_confirm_delete(self) -> None:
        if self._pending_delete_id is None:
            return
        selected_index = self._pending_delete_index or self._selected_index()
        if self._memory_provider.delete_long_term_memory(self._pending_delete_id):
            self._deleted_ids.append(self._pending_delete_id)
        if self._expanded_id == self._pending_delete_id:
            self._expanded_id = None
        self._reload_rows(selected_index)

    def action_cancel_delete(self) -> None:
        selected_index = self._pending_delete_index or self._selected_index()
        self._pending_delete_id = None
        self._pending_delete_index = None
        self._reset_title()
        choice_list = self.query_one("#memory-list", ListView)
        choice_list.index = selected_index
        choice_list.focus()

    def action_close(self) -> None:
        self.dismiss(list(self._deleted_ids))


class AddModelModal(ModalScreen[dict[str, str] | None]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("enter", "submit", "Submit", priority=True),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="model-form-dialog"):
            yield Label("➕ 添加模型", id="choice-title")
            yield Label("Base URL", classes="model-form-label")
            yield Input(placeholder="https://api.example.com/v1", id="model-base-url", classes="model-form-input")
            yield Label("API Key", classes="model-form-label")
            yield Input(placeholder="API Key", password=True, id="model-api-key", classes="model-form-input")
            yield Label("Model ID(s)（多个用逗号分隔）", classes="model-form-label")
            yield Input(placeholder="model-a, model-b", id="model-ids", classes="model-form-input")
            with Horizontal(id="custom-actions"):
                yield Button("取消", id="custom-cancel", variant="warning")

    def on_mount(self) -> None:
        self.query_one("#model-base-url", Input).focus()

    def _on_key(self, event: Key) -> None:
        if event.key == "enter":
            self.action_submit()
            event.stop()
            event.prevent_default()
            return
        if event.key == "escape":
            self.action_cancel()
            event.stop()
            event.prevent_default()
            return

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "custom-cancel":
            self.action_cancel()

    def action_submit(self) -> None:
        base_url = self.query_one("#model-base-url", Input).value.strip()
        api_key = self.query_one("#model-api-key", Input).value.strip()
        model_input = self.query_one("#model-ids", Input).value.strip()
        if not base_url or not api_key or not model_input:
            return
        self.dismiss({"base_url": base_url, "api_key": api_key, "model_input": model_input})

    def action_cancel(self) -> None:
        self.dismiss(None)


class LayoutModal(ModalScreen[str | dict[str, int]]):
    CSS = ChoiceModal.CSS

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("q", "cancel", "Cancel", priority=True),
        Binding("space", "increment_focused", "Increment", priority=True),
    ]

    _LABELS = {
        "content": "Content",
        "tools": "Tools",
        "reasoning": "Reasoning",
        "background": "Background",
        "sub_agent": "Sub-Agent",
    }

    def __init__(self, ratios: dict[str, int]) -> None:
        super().__init__()
        self._ratios = normalize_layout_ratios(ratios)

    def compose(self) -> ComposeResult:
        with Vertical(id="layout-dialog"):
            yield Label(
                "🧩 Layout 布局比例\n点击按钮或按 Space 在 0-10 间循环；0 表示隐藏但继续接收渲染。",
                id="choice-title",
            )
            with Horizontal(id="layout-columns"):
                with Vertical(classes="layout-column"):
                    yield Label("左侧高度比例")
                    for key in LAYOUT_LEFT_KEYS:
                        yield Button(self._button_label(key), id=f"layout-{key}", classes="layout-button")
                with Vertical(classes="layout-column"):
                    yield Label("右侧高度比例")
                    for key in LAYOUT_RIGHT_KEYS:
                        yield Button(self._button_label(key), id=f"layout-{key}", classes="layout-button")
            with Horizontal(id="layout-actions"):
                yield Button("确认应用", id="layout-apply", variant="success", classes="layout-action-button")
                yield Button("重置默认", id="layout-reset", variant="primary", classes="layout-action-button")
                yield Button("取消", id="layout-cancel", variant="warning", classes="layout-action-button")

    def on_mount(self) -> None:
        self.query_one("#layout-content", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("layout-"):
            action = button_id.removeprefix("layout-")
            if action in self._ratios:
                self._increment(action)
                return
            if action == "apply":
                self.dismiss(dict(self._ratios))
                return
            if action == "reset":
                self._ratios = dict(LAYOUT_DEFAULT_RATIOS)
                self._refresh_buttons()
                return
            if action == "cancel":
                self.action_cancel()

    def _on_key(self, event: Key) -> None:
        if event.key == "escape" or (event.key == "q" and not isinstance(self.focused, Input)):
            self.action_cancel()
            event.stop()
            event.prevent_default()
            return
        if event.key == "space":
            self.action_increment_focused()
            event.stop()
            event.prevent_default()

    def action_increment_focused(self) -> None:
        focused = self.focused
        if not isinstance(focused, Button) or focused.id is None:
            return
        key = focused.id.removeprefix("layout-")
        if key in self._ratios:
            self._increment(key)

    def action_cancel(self) -> None:
        self.dismiss("<cancelled>")

    def _button_label(self, key: str) -> str:
        return f"{self._LABELS[key]} {self._ratios[key]}"

    def _increment(self, key: str) -> None:
        self._ratios[key] = (self._ratios[key] + 1) % 11
        self._refresh_button(key)

    def _refresh_buttons(self) -> None:
        for key in self._ratios:
            self._refresh_button(key)

    def _refresh_button(self, key: str) -> None:
        button = self.query_one(f"#layout-{key}", Button)
        button.label = self._button_label(key)
        button.focus()


class MakeCodeInput(TextArea):
    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        app = self.app
        if not isinstance(app, MakeCodeTuiApp):
            return
        app.update_input_height()

    def _on_key(self, event: Key) -> None:
        app = self.app
        if not isinstance(app, MakeCodeTuiApp):
            return
        if event.key == "enter":
            app.submit_current_input()
            event.stop()
            event.prevent_default()
            return
        if event.key == "ctrl+n":
            app.action_insert_newline()
            event.stop()
            event.prevent_default()
            return
        if event.key == "ctrl+p":
            app.action_toggle_plan_mode()
            event.stop()
            event.prevent_default()
            return
        if event.key == "ctrl+c":
            app.action_cancel_response()
            event.stop()
            event.prevent_default()
            return
        if event.key == "escape":
            app.action_cancel_response()
            event.stop()
            event.prevent_default()
            return
        if event.key == "tab":
            app.complete_slash_command()
            event.stop()
            event.prevent_default()
            return
        if event.key == "up" and app.slash_hint_visible:
            app.move_slash_selection(-1)
            event.stop()
            event.prevent_default()
            return
        if event.key == "down" and app.slash_hint_visible:
            app.move_slash_selection(1)
            event.stop()
            event.prevent_default()
            return
        if event.key == "up":
            if not app.should_navigate_input_history(-1):
                return
            app.navigate_input_history(-1)
            event.stop()
            event.prevent_default()
            return
        if event.key == "down":
            if not app.should_navigate_input_history(1):
                return
            app.navigate_input_history(1)
            event.stop()
            event.prevent_default()
            return
        self.call_after_refresh(app.update_input_height)
        self.call_after_refresh(app.update_slash_hint)


class MakeCodeTuiApp(App[None]):
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen {
        layout: vertical;
    }

    #main-grid {
        height: 1fr;
        min-height: 10;
    }

    #left-column {
        width: 2fr;
        height: 1fr;
    }

    #right-column {
        width: 1fr;
        height: 1fr;
    }

    .hidden {
        display: none;
    }

    .pane {
        border: round #3b82f6;
        padding: 0 1;
    }

    .pane-active {
        border: heavy #f59e0b;
    }

    .pane-log {
        height: 1fr;
    }

    .pane-tail {
        display: none;
        height: auto;
        max-height: 8;
    }

    .pane-tail-visible {
        display: block;
    }

    #content-pane {
        height: 1fr;
    }

    #tools-pane {
        height: 1fr;
    }

    #reasoning-pane {
        height: 1fr;
    }

    #background-pane {
        height: 1fr;
    }

    #sub-agent-pane {
        height: 1fr;
    }

    #bottom-grid {
        height: auto;
        min-height: 3;
    }

    #bottom-grid.hidden {
        display: none;
    }

    #runtime-info-row {
        height: 1;
        min-height: 1;
        max-height: 1;
    }

    #runtime-info-bar {
        width: 1fr;
        height: 1;
        background: #111827;
        color: #e5e7eb;
    }

    #hitl-toggle {
        width: 14;
        min-width: 14;
        height: 1;
        min-height: 1;
        max-height: 1;
        background: #1f2937;
        color: #e5e7eb;
        border: none;
    }

    #slash-hints {
        display: none;
        height: 8;
        max-height: 8;
        border: round #f59e0b;
        background: #111827;
        color: #e5e7eb;
        padding: 0 1;
    }

    #slash-hints.visible {
        display: block;
    }

    #input-box {
        height: 3;
        min-height: 3;
        max-height: 5;
        border: round #22c55e;
    }

    #input-box.hidden {
        display: none;
    }
    """

    BINDINGS = [
        Binding("ctrl+p", "toggle_plan_mode", "Toggle Plan/Act", priority=True),
        Binding("ctrl+c", "cancel_response", "Cancel", priority=True),
        Binding("escape", "cancel_response", "Cancel", priority=True),
        Binding("ctrl+n", "insert_newline", "New line", priority=True),
    ]

    def __init__(
        self,
        submit_handler: Callable[[str], str | None] | None = None,
        runtime_info_provider: Callable[[], str] | None = None,
    ) -> None:
        super().__init__()
        self._logs: dict[TuiRegion, RichLog] = {}
        self._panes: dict[TuiRegion, Vertical] = {}
        self._tails: dict[TuiRegion, Static] = {}
        self._status = "MakeCode ready"
        self._runtime_info = ""
        self._submit_handler = submit_handler
        self._runtime_info_provider = runtime_info_provider
        self._mode_label = "ACT"
        self._agent_loop_active = False
        self._slash_matches: list[tuple[str, str]] = []
        self._slash_match_index = 0
        self._slash_hint_visible = False
        self._input_history: list[str] = []
        self._input_history_index: int | None = None
        self._input_history_draft = ""
        self._modal_active = False
        self._right_column_visible = True
        self._last_responsive_width = 0
        self._layout_ratios = load_layout_ratios()
        self._tool_result_count = 0
        self._tool_result_keep_limit = KEEP_RECENT_TOOL_CALL
        self._pane_active_counts: dict[TuiRegion, int] = {}
        self.title = "MakeCode"
        self.sub_title = "🎬 Act · Ready"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-grid"):
            with Vertical(id="left-column"):
                with Vertical(id="content-pane", classes="pane"):
                    yield RichLog(id="content-log", classes="pane-log", markup=True, wrap=True, min_width=1)
                    yield Static("", id="content-tail", classes="pane-tail")
                with Vertical(id="tools-pane", classes="pane"):
                    yield RichLog(id="tools-log", classes="pane-log", markup=True, wrap=True, min_width=1)
                    yield Static("", id="tools-tail", classes="pane-tail")
            with Vertical(id="right-column"):
                with Vertical(id="reasoning-pane", classes="pane"):
                    yield RichLog(id="reasoning-log", classes="pane-log", markup=True, wrap=True, min_width=1)
                    yield Static("", id="reasoning-tail", classes="pane-tail")
                with Vertical(id="background-pane", classes="pane"):
                    yield RichLog(id="background-log", classes="pane-log", markup=True, wrap=True, min_width=1)
                    yield Static("", id="background-tail", classes="pane-tail")
                with Vertical(id="sub-agent-pane", classes="pane"):
                    yield RichLog(id="sub-agent-log", classes="pane-log", markup=True, wrap=True, min_width=1)
                    yield Static("", id="sub-agent-tail", classes="pane-tail")
        with Vertical(id="bottom-grid"):
            yield Static("", id="slash-hints")
            yield MakeCodeInput(id="input-box", placeholder='Prompt here e.g. "整理当前项目的架构"')
        with Horizontal(id="runtime-info-row"):
            yield Static(self._runtime_info, id="runtime-info-bar")
            yield Button("HITL", id="hitl-toggle")
        yield Footer()

    def on_mount(self) -> None:
        self._panes = {
            TuiRegion.CONTENT: self.query_one("#content-pane", Vertical),
            TuiRegion.REASONING: self.query_one("#reasoning-pane", Vertical),
            TuiRegion.TOOLS: self.query_one("#tools-pane", Vertical),
            TuiRegion.BACKGROUND: self.query_one("#background-pane", Vertical),
            TuiRegion.SUB_AGENT: self.query_one("#sub-agent-pane", Vertical),
        }
        self._logs = {
            TuiRegion.CONTENT: self.query_one("#content-log", RichLog),
            TuiRegion.REASONING: self.query_one("#reasoning-log", RichLog),
            TuiRegion.TOOLS: self.query_one("#tools-log", RichLog),
            TuiRegion.BACKGROUND: self.query_one("#background-log", RichLog),
            TuiRegion.SUB_AGENT: self.query_one("#sub-agent-log", RichLog),
        }
        self._tails = {
            TuiRegion.CONTENT: self.query_one("#content-tail", Static),
            TuiRegion.REASONING: self.query_one("#reasoning-tail", Static),
            TuiRegion.TOOLS: self.query_one("#tools-tail", Static),
            TuiRegion.BACKGROUND: self.query_one("#background-tail", Static),
            TuiRegion.SUB_AGENT: self.query_one("#sub-agent-tail", Static),
        }
        self.query_one("#content-pane", Vertical).border_title = "Content"
        self.query_one("#reasoning-pane", Vertical).border_title = "Reasoning"
        self._update_tools_title()
        self.query_one("#background-pane", Vertical).border_title = "Background"
        self.query_one("#sub-agent-pane", Vertical).border_title = "Sub-Agent"
        self._apply_layout_ratios()
        self._update_header_status()
        self._update_input_title()
        self._update_hitl_button()
        self._update_runtime_info()
        self._update_responsive_layout()
        self.set_interval(0.5, self._check_responsive_layout)
        TUI_BRIDGE.bind(self)
        self.query_one("#input-box", MakeCodeInput).focus()

    def update_input_height(self) -> None:
        input_box = self.query_one("#input-box", MakeCodeInput)
        content_rows = max(input_box.wrapped_document.height, 1)
        target_height = min(content_rows, 3) + 2
        if input_box.styles.height != target_height:
            input_box.styles.height = target_height

    def on_resize(self, event: Resize) -> None:
        self._update_responsive_layout(event.size.width)

    def _check_responsive_layout(self) -> None:
        self._update_responsive_layout()

    def _update_responsive_layout(self, width: int | None = None) -> None:
        width = width or self.size.width
        if width == self._last_responsive_width:
            return
        self._last_responsive_width = width
        right_column = self.query_one("#right-column", Vertical)
        should_show_right_column = width >= 140
        if should_show_right_column == self._right_column_visible:
            return
        self._right_column_visible = should_show_right_column
        right_column.set_class(not should_show_right_column, "hidden")

    def _apply_layout_ratios(self) -> None:
        pane_ids = {
            "content": "#content-pane",
            "tools": "#tools-pane",
            "reasoning": "#reasoning-pane",
            "background": "#background-pane",
            "sub_agent": "#sub-agent-pane",
        }
        for key, selector in pane_ids.items():
            pane = self.query_one(selector, Vertical)
            ratio = self._layout_ratios[key]
            pane.set_class(ratio == 0, "hidden")
            if ratio > 0:
                pane.styles.height = f"{ratio}fr"

    def update_layout_ratios(self, ratios: dict[str, int]) -> None:
        self._layout_ratios = normalize_layout_ratios(ratios)
        save_layout_ratios(self._layout_ratios)
        self._apply_layout_ratios()

    def on_unmount(self) -> None:
        TUI_BRIDGE.unbind(self)

    def _update_tools_title(self) -> None:
        self.query_one("#tools-pane", Vertical).border_title = (
            f"Tools · Results: {self._tool_result_count}/{self._tool_result_keep_limit}"
        )

    def _set_pane_active(self, region: TuiRegion, active: bool) -> None:
        pane = self._panes.get(region)
        if pane is None:
            return
        current = self._pane_active_counts.get(region, 0)
        if active:
            current += 1
        else:
            current = max(current - 1, 0)
        self._pane_active_counts[region] = current
        pane.set_class(current > 0, "pane-active")

    def _update_tail(self, region: TuiRegion, payload: Any) -> None:
        tail = self._tails.get(region)
        if tail is None:
            return
        if payload is None or payload == "":
            tail.update("")
            tail.set_class(False, "pane-tail-visible")
            return
        tail.update(payload)
        tail.set_class(True, "pane-tail-visible")

    def _is_log_at_bottom(self, log: RichLog) -> bool:
        return bool(log.is_vertical_scroll_end or log.scroll_y >= log.max_scroll_y - 1)

    def _scroll_log_end_after_refresh(self, log: RichLog) -> None:
        self.call_after_refresh(lambda: log.scroll_end(animate=False))

    def _scroll_bottom_panes_after_refresh(self) -> None:
        for log in self._logs.values():
            if self._is_log_at_bottom(log):
                self._scroll_log_end_after_refresh(log)

    def handle_tui_event(self, event: TuiEvent) -> None:
        if event.region == TuiRegion.STATUS:
            self._runtime_info = str(event.payload)
            self._update_runtime_info()
            return
        if event.region == TuiRegion.RUNTIME_INFO:
            runtime_info = self.query_one("#runtime-info-bar", Static)
            runtime_info.update(str(event.payload))
            return

        log = self._logs[event.region]
        if event.active is not None:
            self._set_pane_active(event.region, event.active)
        if event.tail:
            should_scroll_end = self._is_log_at_bottom(log)
            self._update_tail(event.region, event.payload)
            if should_scroll_end:
                self._scroll_log_end_after_refresh(log)
            return
        if event.clear:
            log.clear()
            self._update_tail(event.region, "")
            self._pane_active_counts[event.region] = 0
            self._set_pane_active(event.region, False)
            if event.region == TuiRegion.TOOLS:
                self._tool_result_count = 0
                self._update_tools_title()
        if event.reset_tool_result_count:
            self._tool_result_count = 0
            self._update_tools_title()
        if event.tool_result_delta:
            self._tool_result_count += event.tool_result_delta
            self._update_tools_title()
        if event.payload is not None and event.region in {TuiRegion.CONTENT, TuiRegion.REASONING, TuiRegion.TOOLS, TuiRegion.BACKGROUND, TuiRegion.SUB_AGENT}:
            should_scroll_end = self._is_log_at_bottom(log)
            log.write(event.payload, expand=True, shrink=True, scroll_end=should_scroll_end)
            if should_scroll_end:
                self._scroll_log_end_after_refresh(log)
        elif event.payload is not None:
            log.write(event.payload)
        self._update_runtime_info()

    def open_choice_modal(
        self,
        title: str,
        options: list[str],
        allow_custom: bool,
        future: Future[str],
    ) -> None:
        def _done(value: str | None) -> None:
            self._modal_active = False
            if not future.done():
                future.set_result(value or "<cancelled>")

        self._modal_active = True
        self.push_screen(ChoiceModal(title, options, allow_custom), _done)

    def open_model_panel_modal(
        self,
        title: str,
        options: list[str],
        future: Future[str],
    ) -> None:
        def _done(value: str | None) -> None:
            self._modal_active = False
            if not future.done():
                future.set_result(value or "<cancelled>")

        self._modal_active = True
        self.push_screen(ModelPanelModal(title, options), _done)

    def open_mcp_switch_modal(self, server_switches: list[dict[str, Any]], future: Future[str | dict]) -> None:
        def _done(value: str | dict | None) -> None:
            self._modal_active = False
            if not future.done():
                future.set_result(value or {"action": "cancel"})

        self._modal_active = True
        self.push_screen(McpSwitchModal(server_switches), _done)

    def open_model_manager_modal(self, model_manager: Any, future: Future[str]) -> None:
        def _done(value: str | None) -> None:
            self._modal_active = False
            if not future.done():
                future.set_result(value or "<cancelled>")

        self._modal_active = True
        self.push_screen(ModelManagerModal(model_manager), _done)

    def open_add_model_modal(self, future: Future[dict[str, str] | None]) -> None:
        def _done(value: dict[str, str] | None) -> None:
            self._modal_active = False
            if not future.done():
                future.set_result(value)

        self._modal_active = True
        self.push_screen(AddModelModal(), _done)

    def open_layout_modal(self, future: Future[str | dict[str, int]]) -> None:
        def _done(value: str | dict[str, int] | None) -> None:
            self._modal_active = False
            if isinstance(value, dict):
                self.update_layout_ratios(value)
                if not future.done():
                    future.set_result(dict(self._layout_ratios))
                return
            if not future.done():
                future.set_result(value or "<cancelled>")

        self._modal_active = True
        self.push_screen(LayoutModal(self._layout_ratios), _done)

    def open_memory_panel_modal(self, memory_provider: Any, future: Future[list[str]]) -> None:
        def _done(value: list[str] | None) -> None:
            self._modal_active = False
            if not future.done():
                future.set_result(value or [])

        self._modal_active = True
        self.push_screen(MemoryPanelModal(memory_provider), _done)

    def action_toggle_plan_mode(self) -> None:
        from utils.plan_mode import toggle_plan_mode

        new_state = toggle_plan_mode()
        self._mode_label = "PLAN" if new_state else "ACT"
        self._update_header_status()
        self._update_input_title()
        self._update_runtime_info()
        self.handle_tui_event(TuiEvent(TuiRegion.STATUS, f"{self._mode_label} mode"))

    def action_insert_newline(self) -> None:
        input_box = self.query_one("#input-box", MakeCodeInput)
        input_box.insert("\n")

    def _record_input_history(self, text: str) -> None:
        if not self._input_history or self._input_history[-1] != text:
            self._input_history.append(text)
        self._input_history_index = None
        self._input_history_draft = ""

    def should_navigate_input_history(self, direction: int) -> bool:
        input_box = self.query_one("#input-box", MakeCodeInput)
        location = input_box.cursor_location
        if direction < 0:
            return input_box.navigator.is_first_wrapped_line(location)
        return input_box.navigator.is_last_wrapped_line(location)

    def navigate_input_history(self, direction: int) -> None:
        if not self._input_history:
            return
        input_box = self.query_one("#input-box", MakeCodeInput)
        if self._input_history_index is None:
            self._input_history_draft = input_box.text
            if direction < 0:
                self._input_history_index = len(self._input_history) - 1
            else:
                return
        else:
            next_index = self._input_history_index + direction
            if next_index < 0:
                next_index = 0
            if next_index >= len(self._input_history):
                self._input_history_index = None
                input_box.load_text(self._input_history_draft)
                input_box.cursor_location = input_box.document.end
                self.update_slash_hint()
                return
            self._input_history_index = next_index

        input_box.load_text(self._input_history[self._input_history_index])
        input_box.cursor_location = input_box.document.end
        self.update_slash_hint()

    def on_key(self, event: Key) -> None:
        if self._modal_active:
            return
        if event.key == "ctrl+p":
            self.action_toggle_plan_mode()
            event.stop()
            event.prevent_default()
            return
        if event.key == "ctrl+n":
            self.action_insert_newline()
            event.stop()
            event.prevent_default()
            return
        if event.key == "ctrl+c":
            self.action_cancel_response()
            event.stop()
            event.prevent_default()
            return
        if event.key == "escape":
            self.action_cancel_response()
            event.stop()
            event.prevent_default()
            return
        if event.key == "tab":
            self.complete_slash_command()
            event.stop()
            event.prevent_default()
            return
        if event.key == "up" and self.slash_hint_visible:
            self.move_slash_selection(-1)
            event.stop()
            event.prevent_default()
            return
        if event.key == "down" and self.slash_hint_visible:
            self.move_slash_selection(1)
            event.stop()
            event.prevent_default()
            return
        if event.key == "up":
            if not self.should_navigate_input_history(-1):
                return
            self.navigate_input_history(-1)
            event.stop()
            event.prevent_default()
            return
        if event.key == "down":
            if not self.should_navigate_input_history(1):
                return
            self.navigate_input_history(1)
            event.stop()
            event.prevent_default()
            return
        if event.key != "enter":
            self.update_input_height()
            self.update_slash_hint()
            return
        self.submit_current_input()
        event.stop()
        event.prevent_default()

    def submit_current_input(self) -> None:
        if self.slash_hint_visible:
            self.accept_slash_selection()
            return
        input_box = self.query_one("#input-box", MakeCodeInput)
        text = input_box.text.strip()
        if not text:
            return
        self._record_input_history(text)
        input_box.load_text("")
        self.update_input_height()
        self._slash_matches = []
        self._slash_match_index = 0
        self._hide_slash_hints()
        from system.console_render import render_content_user_message

        self.handle_tui_event(TuiEvent(TuiRegion.CONTENT, render_content_user_message(text)))
        if self._submit_handler is not None:
            threading.Thread(target=self._run_submit_handler, args=(text,), daemon=True).start()

    def _run_submit_handler(self, text: str) -> None:
        if self._submit_handler is None:
            return
        result = self._submit_handler(text)
        if result == "exit":
            self.call_from_thread(self.exit)

    def set_agent_loop_active(self, active: bool) -> None:
        was_active = self._agent_loop_active
        self._agent_loop_active = active
        self._update_header_status()
        self._update_input_visibility()
        if was_active and not active:
            self._scroll_bottom_panes_after_refresh()
        self._update_runtime_info()

    def _update_input_visibility(self) -> None:
        bottom_grid = self.query_one("#bottom-grid", Vertical)
        input_box = self.query_one("#input-box", MakeCodeInput)
        bottom_grid.set_class(self._agent_loop_active, "hidden")
        input_box.set_class(self._agent_loop_active, "hidden")
        if self._agent_loop_active:
            self._hide_slash_hints()
            return
        self.update_input_height()
        input_box.focus()

    def _update_header_status(self) -> None:
        mode_text = "📋 Plan" if self._mode_label == "PLAN" else "🎬 Act"
        agent_text = "⚙️ Agent Running" if self._agent_loop_active else "Ready"
        self.sub_title = f"{mode_text} · {agent_text}"

    def _update_input_title(self) -> None:
        self.query_one("#input-box", MakeCodeInput).border_title = f"MakeCode · {self._mode_label} · Enter 发送/选择 · Ctrl+C 取消回复 · Ctrl+N 换行 · Ctrl+P 切换 · ↑↓ 选择命令"

    def action_cancel_response(self) -> None:
        from system.stream_cancel import cancel_current_response

        if cancel_current_response():
            self.query_one("#input-box", MakeCodeInput).focus()

    def action_toggle_hitl(self) -> None:
        from utils.hitl import toggle_hitl

        toggle_hitl()
        self._update_hitl_button()
        self._update_runtime_info()
        self.query_one("#input-box", MakeCodeInput).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "hitl-toggle":
            self.action_toggle_hitl()

    def _update_hitl_button(self) -> None:
        try:
            from utils.hitl import get_hitl_status

            enabled = get_hitl_status()
        except Exception:
            enabled = False
        button = self.query_one("#hitl-toggle", Button)
        button.label = "HITL ON" if enabled else "HITL OFF"

    def refresh_status(self) -> None:
        self._update_header_status()
        self._update_hitl_button()
        self._update_runtime_info()

    def _update_runtime_info(self) -> None:
        if self._runtime_info_provider is None:
            return
        try:
            value = self._runtime_info_provider()
        except Exception:
            return
        runtime_info = self.query_one("#runtime-info-bar", Static)
        if self._agent_loop_active:
            value = f"⚙️ Agent: RUNNING  | {value}"
        runtime_info.update(value)

    def _get_slash_matches(self, text: str) -> list[tuple[str, str]]:
        stripped = text.strip()
        if not stripped.startswith("/") or " " in stripped:
            return []
        from system.commands import COMMAND_DESCRIPTIONS

        return [
            (command, description)
            for command, description in COMMAND_DESCRIPTIONS.items()
            if command.startswith(stripped)
        ]

    def update_slash_hint(self) -> None:
        input_box = self.query_one("#input-box", MakeCodeInput)
        matches = self._get_slash_matches(input_box.text)
        self._slash_matches = matches
        self._slash_match_index = 0
        if not matches:
            self._hide_slash_hints()
            return
        self._show_slash_hints(matches)

    def _show_slash_hints(self, matches: list[tuple[str, str]]) -> None:
        hint_box = self.query_one("#slash-hints", Static)
        selected = self._slash_match_index % len(matches)
        window_size = 6
        start = min(max(0, selected - window_size + 1), max(0, len(matches) - window_size))
        end = min(len(matches), start + window_size)
        lines = []
        for index, (command, desc) in enumerate(matches[start:end], start=start):
            marker = "❯ " if index == selected else "  "
            lines.append(f"{marker}[bold cyan]{command}[/bold cyan]  [#aaaaaa]{desc}[/#aaaaaa]")
        hint_box.update("\n".join(lines))
        hint_box.add_class("visible")
        self._slash_hint_visible = True

    @property
    def slash_hint_visible(self) -> bool:
        return self._slash_hint_visible

    def move_slash_selection(self, delta: int) -> None:
        matches = self._slash_matches or self._get_slash_matches(self.query_one("#input-box", MakeCodeInput).text)
        if not matches:
            return
        self._slash_matches = matches
        self._slash_match_index = (self._slash_match_index + delta) % len(matches)
        self._show_slash_hints(matches)

    def accept_slash_selection(self) -> None:
        input_box = self.query_one("#input-box", MakeCodeInput)
        matches = self._slash_matches or self._get_slash_matches(input_box.text)
        if not matches:
            return
        command, _ = matches[self._slash_match_index % len(matches)]
        input_box.load_text(command)
        input_box.cursor_location = input_box.document.end
        self._hide_slash_hints()
        input_box.focus()

    def _hide_slash_hints(self) -> None:
        if not self._slash_hint_visible:
            return
        hint_box = self.query_one("#slash-hints", Static)
        hint_box.update("")
        hint_box.remove_class("visible")
        self._slash_hint_visible = False

    def complete_slash_command(self) -> None:
        input_box = self.query_one("#input-box", MakeCodeInput)
        matches = self._slash_matches or self._get_slash_matches(input_box.text)
        if not matches:
            return
        command, _ = matches[self._slash_match_index % len(matches)]
        input_box.load_text(command)
        input_box.cursor_location = input_box.document.end
        self._slash_matches = matches
        self._slash_match_index = (self._slash_match_index + 1) % len(matches)
        self._show_slash_hints(matches)


def post_tui(
    region: TuiRegion | str,
    payload: RenderableType | str | None = None,
    *,
    clear: bool = False,
    tool_result_delta: int = 0,
    reset_tool_result_count: bool = False,
    tail: bool = False,
    active: bool | None = None,
) -> None:
    TUI_BRIDGE.post(
        region,
        payload,
        clear=clear,
        tool_result_delta=tool_result_delta,
        reset_tool_result_count=reset_tool_result_count,
        tail=tail,
        active=active,
    )


def set_agent_loop_active(active: bool) -> None:
    TUI_BRIDGE.set_agent_loop_active(active)


def refresh_status() -> None:
    TUI_BRIDGE.refresh_status()


def choose_model_panel_tui(title: str, options: list[str]) -> str:
    with TUI_BRIDGE._lock:
        app = TUI_BRIDGE._app
    if app is None:
        return "<cancelled>"
    future: Future[str] = Future()
    if TUI_BRIDGE._is_app_thread():
        app.open_model_panel_modal(title, options, future)
    else:
        app.call_from_thread(app.open_model_panel_modal, title, options, future)
    return future.result()


def manage_models_tui(model_manager: Any) -> str:
    return TUI_BRIDGE.manage_models(model_manager)


def manage_layout_tui() -> str | dict[str, int]:
    return TUI_BRIDGE.manage_layout()


def manage_memories_tui(memory_provider: Any) -> list[str]:
    return TUI_BRIDGE.manage_memories(memory_provider)


def choose_mcp_switch_tui(server_switches: list[dict[str, Any]]) -> str | dict:
    return TUI_BRIDGE.choose_mcp_switch(server_switches)


def choose_add_model_tui() -> dict[str, str] | None:
    return TUI_BRIDGE.choose_add_model()


def choose_tui(title: str, options: list[str], *, allow_custom: bool = False) -> str:
    return TUI_BRIDGE.choose(title, options, allow_custom=allow_custom)
