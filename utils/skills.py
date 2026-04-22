import json
from pathlib import Path

import frontmatter
from openai import pydantic_function_tool
from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import HTML
from pydantic import BaseModel, Field

from init import WORKDIR

SKILLS_DIR = WORKDIR / "skills"
DEFAULT_SKILLS_PROMPT_ENABLED = True


def get_skill_system_note(skill_dir: str, meta_json: str) -> str:
    """Generate the system note for skill loading, providing workspace context."""
    return (
        f"> **[SYSTEM NOTE]**\n"
        f"> The absolute workspace path for this skill is: `{skill_dir}`\n"
        f"> Whenever you need to execute commands, read files, or access any directories (e.g., `scripts/`, `example/`, `output/`) mentioned in this skill document, "
        f"> you MUST resolve them relative to this absolute path (e.g., `{skill_dir}/<relative_path>`).\n\n"
        f"**Skill Metadata:**\n```json\n{meta_json}\n```\n\n"
    )


class LoadSkill(BaseModel):
    """
    Load a specialized skill module by name to get its full instructions and context.

    WHEN TO USE:
    - When a task requires domain-specific knowledge or methodology
    - When the system prompt lists available skills relevant to the current task

    WORKFLOW:
    1. Check system prompt's "Skills Catalog" section for available skills
    2. Call LoadSkill with the exact skill name
    3. Follow the returned instructions to complete the task

    RETURNS: Full skill content including instructions, metadata, and file paths.
    """

    name: str = Field(
        ...,
        description="Exact skill name (case-sensitive). Available skills are listed in the system prompt under 'Skills Catalog'.",
    )


class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self.skills = {}
        self.is_enabled = DEFAULT_SKILLS_PROMPT_ENABLED
        self._load_all()

    def toggle(self) -> str:
        self.is_enabled = not self.is_enabled
        return "skills已加载" if self.is_enabled else "skills已关闭"

    def _load_all(self):
        self.skills = {}
        if not self.skills_dir.exists():
            return
        for f in sorted(self.skills_dir.rglob("SKILL.md")):
            text = f.read_text(encoding="utf-8")
            meta, body = self._parse_frontmatter(text)
            name = str(meta.get("name", f.parent.name)).strip() or f.parent.name
            self.skills[name] = {"meta": meta, "body": body, "path": str(f)}

    @staticmethod
    def _parse_frontmatter(text: str) -> tuple:
        """Parse YAML frontmatter using python-frontmatter."""
        try:
            post = frontmatter.loads(text)
            return post.metadata, post.content
        except Exception as e:
            print_formatted_text(
                HTML(
                    f"<ansiyellow>Warning: Failed to parse frontmatter: {e}</ansiyellow>"
                )
            )
            return {}, text

    def get_descriptions(self) -> str:
        """Short descriptions for UI/system prompt injection."""
        self._load_all()
        if not self.skills:
            return "(no skills available)"

        lines = []
        for i, (name, skill) in enumerate(self.skills.items(), 1):
            meta = skill["meta"]
            desc = str(meta.get("description", "No description provided.")).strip()
            desc = desc.replace("\n", " ").replace("\r", "")
            tags = meta.get("tags", "")
            tags_text = ""
            if isinstance(tags, list):
                tags_text = ", ".join(str(tag).strip() for tag in tags if str(tag).strip())
            elif tags:
                tags_text = str(tags).strip()

            path_text = Path(skill["path"]).parent.relative_to(self.skills_dir).as_posix()
            line = f"{i}. **{name}**"
            if tags_text:
                line += f" [{tags_text}]"
            line += f"\n   - Description: {desc}"
            line += f"\n   - Directory: skills/{path_text}"
            lines.append(line)
        return "\n".join(lines)

    def render_prompt_block(self) -> str:
        """Render a system-prompt block describing currently available skills."""
        if not self.is_enabled:
            return (
                "\n\n## Skills Catalog Status\n"
                "- Status: OFF\n"
                "- Skills catalog injection into the system prompt is currently disabled.\n"
                "- You may still use `LoadSkill` if the exact skill name is already known from prior context."
            )

        self._load_all()
        skills_path = self.skills_dir.absolute().as_posix()
        if not self.skills:
            return (
                "\n\n## Skills Catalog Status\n"
                "- Status: ON\n"
                f"- Source directory: `{skills_path}`\n"
                "- No skills are currently available in this workspace."
            )

        return (
            "\n\n## Skills Catalog Status\n"
            "- Status: ON\n"
            f"- Source directory: `{skills_path}`\n"
            "- The following skills are preloaded into context. When relevant, call `LoadSkill` directly using the exact skill name below.\n\n"
            "### Available Skills\n"
            f"{self.get_descriptions()}"
        )

    def get_content(self, name: str) -> str:
        """Return the full skill body in tool_result."""
        self._load_all()
        skill = self.skills.get(name)
        if not skill:
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"

        skill_dir = Path(skill["path"]).parent.absolute().as_posix()
        meta_json = json.dumps(skill["meta"], ensure_ascii=False, indent=2)
        system_note = get_skill_system_note(skill_dir, meta_json)
        return f'<skill name="{name}">\n{system_note}{skill["body"]}\n</skill>'


SKILL_LOADER = SkillLoader(SKILLS_DIR)

TOOLS = [
    pydantic_function_tool(LoadSkill),
]

SKILL_NAMESPACE = {
    "type": "namespace",
    "name": "Skills",
    "description": (
        "Tool for loading specialized skill modules by exact name. "
        "Available skills are injected into the system prompt when the skills catalog toggle is on. "
        "Only load a skill when it is relevant to the user's request."
    ),
    "tools": TOOLS,
}

SKILL_TOOLS = [
    SKILL_NAMESPACE,
]

SKILL_TOOLS_HANDLERS = {
    "LoadSkill": SKILL_LOADER.get_content,
}
