"""
Plan Mode state management.

Plan Mode restricts the agent to read-only and planning tools only.
This ensures the LLM focuses on analysis and task topology planning
before any file modifications or command execution.

Toggle via Tab key or /plan command.
"""

# Tools allowed in Plan Mode: read-only + planning
PLAN_TOOLS_WHITELIST = frozenset({
    # File read-only
    "RunRead",
    "RunGrep",
    "RunGlob",
    # TaskManager planning
    "CreateTask",
    "UpdateTaskContent",
    "UpdateTaskStatus",
    "UpdateTaskDependencies",
    "DeleteAllTasks",
    "GetTask",
    "GetRunnableTasks",
    "GetTaskTable",
    # Auxiliary
    "GetSystemTime",
    "LoadSkill",
})

# Global state
PLAN_MODE_ENABLED = False


def toggle_plan_mode(enabled: bool = None) -> bool:
    """Toggle plan mode. Returns new state."""
    global PLAN_MODE_ENABLED
    if enabled is not None:
        PLAN_MODE_ENABLED = enabled
    else:
        PLAN_MODE_ENABLED = not PLAN_MODE_ENABLED
    return PLAN_MODE_ENABLED


def is_plan_mode() -> bool:
    return PLAN_MODE_ENABLED
