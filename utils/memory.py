import json
import time
import uuid
from datetime import datetime
from pathlib import Path

from openai import pydantic_function_tool
from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import HTML
from pydantic import BaseModel, Field
from rich.markdown import Markdown
from rich.table import Table
from init import WORKDIR
from system.console_render import _render_tool_call, _render_tool_output, console as _compact_console
from system.stream_render import StreamRenderer
from utils.common import sanitize_title
from utils.llm_client import llm_client

THRESHOLD = 1024 * 160
MAKECODE_DIR = WORKDIR / ".makecode"
TRANSCRIPT_DIR = MAKECODE_DIR / "transcripts"
CHECKPOINT_DIR = MAKECODE_DIR / "checkpoint"
MEMORY_DIR = MAKECODE_DIR / "memory"
MEMORY_JSONL_FILE = MEMORY_DIR / "memory.jsonl"
MEMORY_CONFIG_FILE = MEMORY_DIR / "memory_config.json"
DEFAULT_MEMORY_SIZE = 30
KEEP_RECENT_TOOL_CALL = 100


class AppendLongTermMemory(BaseModel):
    """Append a durable memory entry to WORKDIR/.makecode/memory/memory.jsonl."""

    category: str = Field(
        ...,
        description=(
            "Memory category, e.g. 'preference', 'project-convention', "
            "'workflow', 'pitfall', 'environment', or 'release-process'."
        ),
    )
    insight: str = Field(
        ...,
        description="The durable lesson, user preference, convention, or reusable experience to remember.",
    )
    evidence: str = Field(
        ...,
        description="Brief source context from the compacted conversation explaining why this memory is justified.",
    )
    reuse_condition: str = Field(
        ...,
        description="When this memory should be applied in future sessions.",
    )


class DeleteLongTermMemory(BaseModel):
    """Delete an active durable memory by ID using logical deletion."""

    memory_id: str = Field(..., description="The active memory ID to delete.")


class UpdateLongTermMemory(BaseModel):
    """Update an active durable memory by ID."""

    memory_id: str = Field(..., description="The active memory ID to update.")
    category: str = Field(..., description="Updated memory category.")
    insight: str = Field(..., description="Updated durable knowledge to remember.")
    evidence: str = Field(..., description="Updated source context explaining why this memory is justified.")
    reuse_condition: str = Field(..., description="Updated condition for when this memory should be applied.")


LONG_TERM_MEMORY_TOOLS = [
    pydantic_function_tool(AppendLongTermMemory),
    pydantic_function_tool(DeleteLongTermMemory),
    pydantic_function_tool(UpdateLongTermMemory),
]


def _new_memory_record(category: str, insight: str, evidence: str, reuse_condition: str) -> dict:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    memory_id = f"mem_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    return {
        "id": memory_id,
        "created_at": timestamp,
        "updated_at": timestamp,
        "category": category.strip(),
        "insight": insight.strip(),
        "evidence": evidence.strip(),
        "reuse_condition": reuse_condition.strip(),
        "status": "active",
    }


def _memory_sort_key(record: dict) -> str:
    return record.get("created_at") or record.get("updated_at") or ""


def append_long_term_memory(
        category: str,
        insight: str,
        evidence: str,
        reuse_condition: str,
        **kwargs,
) -> dict:
    records = _read_memory_records(include_deleted=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    active_records = [record for record in records if record.get("status") == "active"]
    overflow_count = len(active_records) + 1 - get_memory_size()
    deleted_ids = []
    if overflow_count > 0:
        for record in sorted(active_records, key=_memory_sort_key)[:overflow_count]:
            record["status"] = "deleted"
            record["updated_at"] = now
            deleted_ids.append(record.get("id", ""))

    record = _new_memory_record(category, insight, evidence, reuse_condition)
    records.append(record)
    _write_memory_records(records)
    return {**record, "path": MEMORY_JSONL_FILE.as_posix(), "deleted_overflow_ids": deleted_ids}


def delete_long_term_memory(memory_id: str) -> bool:
    records = _read_memory_records(include_deleted=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    found = False
    for record in records:
        if record.get("id") == memory_id and record.get("status") == "active":
            record["status"] = "deleted"
            record["updated_at"] = now
            found = True
            break
    if found:
        _write_memory_records(records)
    return found


def update_long_term_memory(
        memory_id: str,
        category: str,
        insight: str,
        evidence: str,
        reuse_condition: str,
        **kwargs,
) -> dict:
    records = _read_memory_records(include_deleted=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for record in records:
        if record.get("id") == memory_id and record.get("status") == "active":
            record["updated_at"] = now
            record["category"] = category.strip()
            record["insight"] = insight.strip()
            record["evidence"] = evidence.strip()
            record["reuse_condition"] = reuse_condition.strip()
            _write_memory_records(records)
            return {**record, "path": MEMORY_JSONL_FILE.as_posix()}
    return {"error": f"active memory not found: {memory_id}"}


LONG_TERM_MEMORY_TOOL_HANDLERS = {
    "AppendLongTermMemory": append_long_term_memory,
    "DeleteLongTermMemory": lambda memory_id, **kwargs: {
        "memory_id": memory_id,
        "deleted": delete_long_term_memory(memory_id),
    },
    "UpdateLongTermMemory": update_long_term_memory,
}


def _validate_memory_size(size) -> int:
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError("memory size must be a positive integer")
    return size


def get_memory_size() -> int:
    if not MEMORY_CONFIG_FILE.exists():
        return DEFAULT_MEMORY_SIZE
    try:
        with open(MEMORY_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return DEFAULT_MEMORY_SIZE
    try:
        return _validate_memory_size(data.get("memory_size", DEFAULT_MEMORY_SIZE))
    except ValueError:
        return DEFAULT_MEMORY_SIZE


def set_memory_size(size: int) -> int:
    size = _validate_memory_size(size)
    MEMORY_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(MEMORY_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"memory_size": size}, f, ensure_ascii=False, indent=2)
    return size


def get_active_memory_count() -> int:
    return len(list_long_term_memories())


def _read_memory_records(include_deleted: bool = False) -> list[dict]:
    if not MEMORY_JSONL_FILE.exists():
        return []

    records = []
    with open(MEMORY_JSONL_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if include_deleted or record.get("status") == "active":
                records.append(record)
    return records


def _write_memory_records(records: list[dict]) -> None:
    MEMORY_JSONL_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(MEMORY_JSONL_FILE, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def list_long_term_memories() -> list[dict]:
    return _read_memory_records(include_deleted=False)


def render_long_term_memory_markdown() -> str:
    records = list_long_term_memories()
    if not records:
        return ""

    parts = []
    for record in records:
        parts.append(
            f"## {record.get('id', '')}\n"
            f"- Category: {record.get('category', '')}\n"
            f"- Updated at: {record.get('updated_at', '')}\n"
            f"- Insight: {record.get('insight', '')}\n"
            f"- Evidence: {record.get('evidence', '')}\n"
            f"- Reuse condition: {record.get('reuse_condition', '')}"
        )
    return "\n\n".join(parts)


def _parse_tool_arguments(arguments) -> dict:
    if isinstance(arguments, dict):
        return arguments
    if not arguments:
        return {}
    return json.loads(arguments, strict=False)


def memory_agent_loop(
        conversation_text: str,
        summary: str,
        reason: str,
        current_memory_content: str,
        tools: list,
        mode: str = "compact",
        max_iterations: int = 5,
) -> list[dict]:
    _compact_console.print("\n[bold yellow]🧠 Managing long-term memory...[/bold yellow]")
    _compact_console.rule("[bold yellow]Memory[/bold yellow]", style="yellow")
    saved_outputs = []
    messages = llm_client.get_memory_decision_messages(
        conversation_text,
        summary,
        reason,
        current_memory_content,
        mode=mode,
    )

    for _ in range(max_iterations):
        try:
            _, memory_tool_calls, raw_message = llm_client.get_memory_decision_stream_messages(messages, tools)
        except Exception as e:
            _compact_console.print(f"[bold red]Memory manager error: {e}[/bold red]")
            _compact_console.rule(style="yellow")
            return saved_outputs

        if raw_message is not None:
            llm_client.append_assistant_message(messages, raw_message)

        if not memory_tool_calls:
            break

        for tool_call in memory_tool_calls:
            tool_name = tool_call.get("name")
            tool_id = tool_call.get("id")
            handler = LONG_TERM_MEMORY_TOOL_HANDLERS.get(tool_name)
            if not handler:
                output = f"Unknown memory tool: {tool_name}"
            else:
                _render_tool_call(tool_name, tool_call.get("arguments"), identity="🧠 Memory Agent")
                try:
                    arguments = _parse_tool_arguments(tool_call.get("arguments"))
                    output = handler(**arguments)
                except Exception as e:
                    output = f"Error executing {tool_name}: {e}."
                _render_tool_output(tool_name, output, identity="🧠 Memory Agent")
            saved_outputs.append({"tool": tool_name, "output": output})
            if tool_id:
                messages.append(llm_client.format_tool_result(tool_id, tool_name, output))

    if not saved_outputs:
        _compact_console.print("[yellow]No long-term memory changes.[/yellow]")
    _compact_console.rule(style="yellow")
    return saved_outputs


def manual_memory_update(prompt: str, history: list = None) -> list[dict]:
    prompt = prompt.strip()
    conversation_messages = [msg for msg in (history or []) if msg.get("role") != "system"]
    return memory_agent_loop(
        conversation_text=json.dumps(
            conversation_messages,
            ensure_ascii=False,
            default=str,
        ),
        summary="",
        reason=prompt,
        current_memory_content=render_long_term_memory_markdown(),
        tools=LONG_TERM_MEMORY_TOOLS,
        mode="active",
    )


def save_checkpoint(messages: list, filepath: Path = None, title: str = None) -> Path:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    if filepath is None:
        uid = uuid.uuid4().hex[:8]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if title:
            safe_title = sanitize_title(title)
            if safe_title:
                filename = f"ckpt_{safe_title}_{timestamp}_{uid}.json"
            else:
                filename = f"ckpt_{timestamp}_{uid}.json"
        else:
            filename = f"ckpt_{timestamp}_{uid}.json"
        filepath = CHECKPOINT_DIR / filename
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)
    return filepath


def get_checkpoint_title(filepath: Path) -> str:
    """Extract title from checkpoint filename if available."""
    stem = filepath.stem
    if not stem.startswith("ckpt_"):
        return None
        
    parts = stem.split("_")
    # Check if it has title format: ckpt_title_YYYYMMDD_HHMMSS_uid
    # Timestamp format is YYYYMMDD_HHMMSS which is 8_6 chars
    
    # Try to find timestamp
    for i, part in enumerate(parts):
        if len(part) == 8 and part.isdigit():  # YYYYMMDD
            # Check next part for time
            if i + 1 < len(parts) and len(parts[i+1]) == 6 and parts[i+1].isdigit():
                # Found timestamp at index i
                if i > 1:  # There is a title (ckpt_title_...)
                    title_parts = parts[1:i]
                    return " ".join(title_parts).replace("_", " ")
                return None
    return None


# --- Checkpoint rename --- #


def rename_checkpoint_with_title(filepath: Path, title: str) -> Path:
    """Rename an existing checkpoint file to include *title* in its name.

    Because ``sanitize_title`` never allows ``_``, we can discover the
    timestamp anchor by splitting on ``_`` and finding the 8-digit date
    segment followed by a 6-digit time segment.
    Everything between ``ckpt`` and that date is the (possibly empty)
    old title portion.
    """
    safe_title = sanitize_title(title)
    if not safe_title:
        return filepath

    stem = filepath.stem
    if not stem.startswith("ckpt_"):
        return filepath

    parts = stem.split("_")
    # Find date segment: 8-digit, followed by 6-digit time
    try:
        date_idx = next(
            i for i, p in enumerate(parts)
            if len(p) == 8 and p.isdigit()
               and i + 1 < len(parts)
               and len(parts[i + 1]) == 6 and parts[i + 1].isdigit()
        )
    except StopIteration:
        return filepath

    ts = f"{parts[date_idx]}_{parts[date_idx + 1]}"
    uid = parts[-1]
    new_path = filepath.parent / f"ckpt_{safe_title}_{ts}_{uid}.json"

    if new_path == filepath:
        return filepath

    if filepath.exists():
        filepath.rename(new_path)
    return new_path


def list_checkpoints() -> list:
    if not CHECKPOINT_DIR.exists():
        return []
    files = list(CHECKPOINT_DIR.glob("ckpt_*.json"))
    files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return files


def load_checkpoint(filepath: Path) -> list:
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


try:
    import tiktoken
    import os
    import sys

    # Determine base path for the bundled executable or normal execution
    if getattr(sys, "frozen", False):
        _base_path = Path(sys._MEIPASS)
    else:
        _base_path = Path(__file__).parent.parent

    # Use local cache if it exists (for offline/packaged environments)
    _local_cache = _base_path / "tiktoken_cache"
    if _local_cache.exists():
        os.environ["TIKTOKEN_CACHE_DIR"] = str(_local_cache)

    _ENCODER = tiktoken.get_encoding("o200k_base")
except ImportError:
    print_formatted_text(
        HTML(f"\n<ansiyellow>⚠️ tiktoken加载失败, token将使用估算模式 </ansiyellow>\n")
    )
    _ENCODER = None


def estimate_tokens(messages: list, tools_definition: list = None):
    # 计算基础文本的 token 数（messages 已包含系统提示词）
    text = json.dumps(messages, ensure_ascii=False)
    if _ENCODER:
        base_tokens = len(_ENCODER.encode(text, disallowed_special=()))
    else:
        base_tokens = len(text) // 2

    # 加上工具定义的 token 数
    if tools_definition:
        tools_text = json.dumps(tools_definition, ensure_ascii=False)
        if _ENCODER:
            base_tokens += len(_ENCODER.encode(tools_text, disallowed_special=()))
        else:
            base_tokens += len(tools_text) // 2

    return base_tokens


def micro_compact(input_list: list) -> list:
    tool_results = []
    for msg in input_list:
        if msg.get("type") == "function_call_output" or msg.get("role") == "tool":
            tool_results.append(msg)

    if len(tool_results) <= KEEP_RECENT_TOOL_CALL:
        return input_list

    tool_call_info_map = {}
    for msg in input_list:
        if msg.get("type") == "function_call":
            tool_call_info_map[msg.get("call_id")] = {
                "name": msg.get("name"),
                "arguments": msg.get("arguments"),
            }
        elif msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tc_id = (
                    tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                )
                tc_func = (
                    tc.get("function", {})
                    if isinstance(tc, dict)
                    else getattr(tc, "function", None)
                )
                if tc_func:
                    tc_name = (
                        tc_func.get("name")
                        if isinstance(tc_func, dict)
                        else getattr(tc_func, "name", None)
                    )
                    tc_args = (
                        tc_func.get("arguments")
                        if isinstance(tc_func, dict)
                        else getattr(tc_func, "arguments", None)
                    )
                    if tc_id:
                        tool_call_info_map[tc_id] = {
                            "name": tc_name,
                            "arguments": tc_args,
                        }

    to_clear = tool_results[:-KEEP_RECENT_TOOL_CALL]
    for result in to_clear:
        call_id = result.get("call_id") or result.get("tool_call_id")
        info = tool_call_info_map.get(call_id, {})
        tool_name = info.get("name", "unknown tool")
        tool_arguments = info.get("arguments", {})

        replacement = (
            f"[Previous {tool_name} result cleared, arguments were: {tool_arguments}]"
        )
        if "output" in result:
            result["output"] = replacement
        elif "content" in result:
            result["content"] = replacement

    return input_list


def auto_compact(
        messages: list,
        reason: str = "User triggered compact",
        system_prompt_fn=None,
) -> str:
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"

    with open(transcript_path, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")
    print_formatted_text(
        HTML(f"\n<ansiyellow>[Transcript saved to: {transcript_path}]</ansiyellow>")
    )

    # Filter out original system messages to prevent system instructions clash
    filtered_messages = [m for m in messages if m.get("role") != "system"]
    conversation_text = json.dumps(filtered_messages, default=str, ensure_ascii=False)

    _compact_console.print(
        f"\n[bold yellow]⚡️ Compacting context...[/bold yellow]  "
        f"[dim]{reason}[/dim]"
    )
    _compact_console.rule("[bold cyan]📝 Summary", style="cyan")

    chunks: list[str] = []
    try:
        renderer = StreamRenderer(console=_compact_console, update_interval=0.1)
        summary, _, _ = renderer.render_text_stream(
            llm_client.get_summary_stream_events(conversation_text, reason)
        )
        if summary:
            chunks.append(summary)

    except Exception as e:
        # 打印红色的错误提示，比原生的 print 更友好
        _compact_console.print(f"\n[bold red]Stream Error: {e}[/bold red]")

        # 流式失败时回退到普通调用
        fallback = llm_client.get_summary(conversation_text, reason)
        # 回退时也同样使用 Markdown 渲染
        _compact_console.print(Markdown(fallback))
        chunks = [fallback]

    _compact_console.rule(style="cyan")
    summary = "".join(chunks)

    memory_agent_loop(
        conversation_text=conversation_text,
        summary=summary,
        reason=reason,
        current_memory_content=render_long_term_memory_markdown(),
        tools=LONG_TERM_MEMORY_TOOLS,
    )

    system_msgs = [m for m in messages if m.get("role") == "system"]
    if system_prompt_fn and system_msgs:
        system_msgs = [{"role": "system", "content": system_prompt_fn()}]

    summary_msgs = [
        {
            "role": "user",
            "content": f"[Previous conversation compressed. Reason: {reason}] \n\n{summary}",
        },
        {
            "role": "assistant",
            "content": "Understood. I have the context from the summary. Ready to proceed.",
        },
    ]

    # Rebuild history in-place
    new_history = system_msgs + summary_msgs
    messages.clear()
    messages.extend(new_history)

    return "History successfully compacted and summarized."
