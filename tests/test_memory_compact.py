import json
import sys
from pathlib import Path

import prompt_toolkit
import prompt_toolkit.shortcuts.utils

prompt_toolkit.print_formatted_text = lambda *args, **kwargs: None
prompt_toolkit.shortcuts.utils.print_formatted_text = lambda *args, **kwargs: None
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import utils.memory as memory


class FakeSummaryClient:
    def __init__(self, events=None, fallback=None, memory_decision=None):
        self.events = events or []
        self.fallback = fallback
        self.memory_decision = memory_decision or ("No durable memory.", [], {"role": "assistant"})
        self.summary_tools_seen = []

    def get_summary_stream_events(self, conversation_text, reason, tools=None):
        self.summary_tools_seen.append(tools)
        assert tools is None
        yield from self.events

    def get_summary(self, conversation_text, reason, tools=None):
        assert tools is None
        return self.fallback

    def get_memory_decision(self, conversation_text, summary, reason, tools):
        assert tools == memory.LONG_TERM_MEMORY_TOOLS
        return self.memory_decision


def test_save_long_term_memory_appends_jsonl(tmp_path, monkeypatch):
    memory_file = tmp_path / "custom" / "memory.jsonl"
    monkeypatch.setattr(memory, "MEMORY_JSONL_FILE", memory_file)

    result = memory.save_long_term_memory(
        category="preference",
        insight="Prefer concise Chinese explanations.",
        evidence="User requested direct project mechanism explanations.",
        reuse_condition="When explaining this project in future sessions.",
    )

    record = json.loads(memory_file.read_text(encoding="utf-8").strip())
    assert record["id"].startswith("mem_")
    assert record["category"] == "preference"
    assert record["insight"] == "Prefer concise Chinese explanations."
    assert record["evidence"] == "User requested direct project mechanism explanations."
    assert record["reuse_condition"] == "When explaining this project in future sessions."
    assert record["status"] == "active"
    assert result["path"] == memory_file.as_posix()
    assert result["id"] == record["id"]


def test_auto_compact_does_not_write_memory_without_tool_call(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "TRANSCRIPT_DIR", tmp_path / "transcripts")
    monkeypatch.setattr(memory, "MAKECODE_DIR", tmp_path / ".makecode")
    monkeypatch.setattr(memory, "MEMORY_JSONL_FILE", tmp_path / ".makecode" / "memory.jsonl")
    fake_client = FakeSummaryClient(events=[
        {"type": "text", "content": "Summary only."},
        {"type": "done", "content": ("Summary only.", [], {"role": "assistant"})},
    ])
    monkeypatch.setattr(
        memory,
        "llm_client",
        fake_client,
    )

    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]

    result = memory.auto_compact(messages, reason="test")

    assert result == "History successfully compacted and summarized."
    assert not memory.MEMORY_JSONL_FILE.exists()
    assert fake_client.summary_tools_seen == [None]
    assert messages == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "[Previous conversation compressed. Reason: test] \n\nSummary only."},
        {"role": "assistant", "content": "Understood. I have the context from the summary. Ready to proceed."},
    ]


def test_auto_compact_executes_memory_tool_call(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "TRANSCRIPT_DIR", tmp_path / "transcripts")
    monkeypatch.setattr(memory, "MAKECODE_DIR", tmp_path / ".makecode")
    monkeypatch.setattr(memory, "MEMORY_JSONL_FILE", tmp_path / ".makecode" / "memory.jsonl")
    memory_decision = (
        "Saving durable memory.",
        [
            {
                "name": "SaveLongTermMemory",
                "arguments": '{"category":"workflow","insight":"Use TaskManager first.","evidence":"System policy requires topology planning.","reuse_condition":"When executing coding tasks."}',
            }
        ],
        {"role": "assistant"},
    )
    fake_client = FakeSummaryClient(
        events=[
            {"type": "text", "content": "Summary."},
            {"type": "done", "content": ("Summary.", [], {"role": "assistant"})},
        ],
        memory_decision=memory_decision,
    )
    monkeypatch.setattr(
        memory,
        "llm_client",
        fake_client,
    )

    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]

    memory.auto_compact(messages, reason="test")

    record = json.loads(memory.MEMORY_JSONL_FILE.read_text(encoding="utf-8").strip())
    assert fake_client.summary_tools_seen == [None]
    assert record["category"] == "workflow"
    assert record["insight"] == "Use TaskManager first."
    assert record["evidence"] == "System policy requires topology planning."
    assert record["reuse_condition"] == "When executing coding tasks."
    assert record["status"] == "active"


def test_auto_compact_refreshes_system_prompt_after_memory_decision(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "TRANSCRIPT_DIR", tmp_path / "transcripts")
    monkeypatch.setattr(memory, "MEMORY_JSONL_FILE", tmp_path / ".makecode" / "memory.jsonl")
    monkeypatch.setattr(
        memory,
        "llm_client",
        FakeSummaryClient(events=[
            {"type": "text", "content": "Summary."},
            {"type": "done", "content": ("Summary.", [], {"role": "assistant"})},
        ]),
    )

    messages = [
        {"role": "system", "content": "old system"},
        {"role": "user", "content": "hello"},
    ]

    memory.auto_compact(messages, reason="test", system_prompt_fn=lambda: "new system with memory")

    assert messages[0] == {"role": "system", "content": "new system with memory"}
    assert messages[1]["content"] == "[Previous conversation compressed. Reason: test] \n\nSummary."


def test_auto_compact_creates_transcript_parent_directory(tmp_path, monkeypatch):
    transcript_dir = tmp_path / "missing" / ".makecode" / "transcripts"
    monkeypatch.setattr(memory, "TRANSCRIPT_DIR", transcript_dir)
    monkeypatch.setattr(memory, "MEMORY_JSONL_FILE", tmp_path / ".makecode" / "memory.jsonl")
    monkeypatch.setattr(
        memory,
        "llm_client",
        FakeSummaryClient(events=[
            {"type": "text", "content": "Summary."},
            {"type": "done", "content": ("Summary.", [], {"role": "assistant"})},
        ]),
    )

    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]

    memory.auto_compact(messages, reason="test")

    assert transcript_dir.exists()
    assert len(list(transcript_dir.glob("transcript_*.jsonl"))) == 1


def test_memory_delete_soft_deletes_and_render_excludes_deleted(tmp_path, monkeypatch):
    memory_file = tmp_path / ".makecode" / "memory.jsonl"
    monkeypatch.setattr(memory, "MEMORY_JSONL_FILE", memory_file)

    memory.save_long_term_memory(
        category="workflow",
        insight="Keep this active.",
        evidence="First memory.",
        reuse_condition="Future work.",
    )
    memory.save_long_term_memory(
        category="pitfall",
        insight="Delete this one.",
        evidence="Second memory.",
        reuse_condition="Never.",
    )
    records = memory.list_long_term_memories()
    deleted_id = records[1]["id"]

    assert memory.delete_long_term_memory(deleted_id)
    assert not memory.delete_long_term_memory("missing")

    active = memory.list_long_term_memories()
    rendered = memory.render_long_term_memory_markdown()
    assert len(active) == 1
    assert active[0]["insight"] == "Keep this active."
    assert "Keep this active." in rendered
    assert "Delete this one." not in rendered


def test_invalid_jsonl_memory_rows_are_removed_during_load(tmp_path, monkeypatch):
    memory_file = tmp_path / ".makecode" / "memory.jsonl"
    monkeypatch.setattr(memory, "MEMORY_JSONL_FILE", memory_file)
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    active_record = {
        "id": "mem_active",
        "created_at": "2026-05-10 12:00:00",
        "updated_at": "2026-05-10 12:00:00",
        "category": "workflow",
        "insight": "Keep valid active memory.",
        "evidence": "Valid active row.",
        "reuse_condition": "Future sessions.",
        "status": "active",
    }
    deleted_record = {
        "id": "mem_deleted",
        "created_at": "2026-05-10 12:01:00",
        "updated_at": "2026-05-10 12:02:00",
        "category": "pitfall",
        "insight": "Do not render deleted memory.",
        "evidence": "Valid deleted row.",
        "reuse_condition": "Never.",
        "status": "deleted",
    }
    second_active_record = {
        "id": "mem_second_active",
        "created_at": "2026-05-10 12:03:00",
        "updated_at": "2026-05-10 12:03:00",
        "category": "preference",
        "insight": "Keep another active memory.",
        "evidence": "Valid active row after invalid row.",
        "reuse_condition": "Future sessions.",
        "status": "active",
    }
    memory_file.write_text(
        json.dumps(active_record, ensure_ascii=False) + "\n"
        "{bad json\n"
        + json.dumps(deleted_record, ensure_ascii=False) + "\n"
        + json.dumps(second_active_record, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    active = memory.list_long_term_memories()

    assert [item["id"] for item in active] == ["mem_active", "mem_second_active"]
    cleaned_lines = memory_file.read_text(encoding="utf-8").splitlines()
    assert len(cleaned_lines) == 3
    assert "{bad json" not in cleaned_lines
    assert memory.delete_long_term_memory("mem_second_active")
    rendered = memory.render_long_term_memory_markdown()
    assert "Keep valid active memory." in rendered
    assert "Keep another active memory." not in rendered


def test_stop_cancel_listener_clears_global_cancel_flag():
    from system.stream_cancel import is_cancelled, stop_cancel_listener, stream_cancel_event

    stream_cancel_event.set()
    stop_cancel_listener()

    assert not is_cancelled()
