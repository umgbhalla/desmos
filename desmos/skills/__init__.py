"""Pi/Prime-shaped skill discovery.

Locations and progressive disclosure match earendil-works/pi and Prime Agent:
catalog names+descriptions in the prompt, full SKILL.md on demand, Python
skills bound into the kernel namespace.
"""

from __future__ import annotations

import importlib.util
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

SkillKind = Literal["markdown", "python"]


@dataclass
class Skill:
    name: str
    description: str
    file_path: Path
    kind: SkillKind
    import_name: str | None = None
    disable_model_invocation: bool = False


_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)
_NAME = re.compile(r"^name:\s*(.+)$", re.M)
_DESC = re.compile(r"^description:\s*(.+)$", re.M)
_DISABLE = re.compile(r"^disable-model-invocation:\s*true\s*$", re.M | re.I)


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    m = _FM.match(text)
    if not m:
        return {}, text
    raw = m.group(1)
    meta: dict[str, str] = {}
    if nm := _NAME.search(raw):
        meta["name"] = nm.group(1).strip().strip("\"'")
    if dm := _DESC.search(raw):
        meta["description"] = dm.group(1).strip().strip("\"'")
    if _DISABLE.search(raw):
        meta["disable-model-invocation"] = "true"
    return meta, text[m.end() :]


def _slug(name: str) -> str:
    return name.strip().lower().replace("-", "_")


def _python_entry(skill_dir: Path, import_name: str) -> Path | None:
    candidates = [
        skill_dir / "src" / import_name / "__init__.py",
        skill_dir / import_name / "__init__.py",
        skill_dir / f"{import_name}.py",
        skill_dir / "skill.py",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def _read_skill(md_path: Path) -> Skill | None:
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta, _ = parse_frontmatter(text)
    name = meta.get("name") or (md_path.parent.name if md_path.name == "SKILL.md" else md_path.stem)
    name = name.strip()
    if not name:
        return None
    desc = meta.get("description") or ""
    import_name = _slug(name)
    skill_dir = md_path.parent
    py = _python_entry(skill_dir, import_name) if md_path.name == "SKILL.md" else None
    return Skill(
        name=name,
        description=desc,
        file_path=md_path,
        kind="python" if py else "markdown",
        import_name=import_name if py else None,
        disable_model_invocation=meta.get("disable-model-invocation") == "true",
    )


def _scan_dir(root: Path, *, root_md: bool) -> list[Skill]:
    if not root.is_dir():
        return []
    found: list[Skill] = []
    if root_md:
        for md in sorted(root.glob("*.md")):
            if md.name == "SKILL.md":
                continue
            skill = _read_skill(md)
            if skill:
                found.append(skill)
    for md in sorted(root.rglob("SKILL.md")):
        skill = _read_skill(md)
        if skill:
            found.append(skill)
    return found


def skill_roots(cwd: Path) -> list[tuple[Path, bool]]:
    """(path, allow_root_md) lowest precedence first."""
    roots: list[tuple[Path, bool]] = []
    roots.append((Path(__file__).resolve().parent, False))
    home = Path.home()
    roots.append((home / ".agents" / "skills", False))
    roots.append((home / ".desmos" / "skills", True))

    chain: list[Path] = []
    cur = cwd.resolve()
    while True:
        chain.append(cur)
        if (cur / ".git").exists() or cur.parent == cur:
            break
        cur = cur.parent
    for path in reversed(chain):
        roots.append((path / ".agents" / "skills", False))
        roots.append((path / ".desmos" / "skills", True))
    return roots


def discover_skills(cwd: Path) -> list[Skill]:
    by_name: dict[str, Skill] = {}
    for root, root_md in skill_roots(cwd):
        for skill in _scan_dir(root, root_md=root_md):
            by_name[skill.name] = skill
    return sorted(by_name.values(), key=lambda s: s.name)


def format_skills_for_prompt(skills: list[Skill]) -> str:
    visible = [s for s in skills if not s.disable_model_invocation]
    if not visible:
        return ""
    lines = [
        "The following skills provide specialized instructions for specific tasks.",
        'Load the full file with <skill name="..."/> when the task matches.',
        "Python skills are imported into the kernel by python_import.",
        "",
        "<available_skills>",
    ]
    for skill in visible:
        lines.append("  <skill>")
        lines.append(f"    <name>{_xml(skill.name)}</name>")
        lines.append(f"    <type>{skill.kind}</type>")
        if skill.import_name:
            lines.append(f"    <python_import>{_xml(skill.import_name)}</python_import>")
        lines.append(f"    <description>{_xml(skill.description)}</description>")
        lines.append(f"    <location>{_xml(str(skill.file_path))}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def load_skill_body(skill: Skill) -> str:
    try:
        return skill.file_path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"could not read {skill.file_path}: {exc}"


def bind_python_skill(ns: dict[str, Any], skill: Skill) -> Any | None:
    if skill.kind != "python" or not skill.import_name:
        return None
    entry = _python_entry(skill.file_path.parent, skill.import_name)
    if entry is None:
        return None
    spec = importlib.util.spec_from_file_location(skill.import_name, entry)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ns[skill.import_name] = module
    return getattr(module, "handle", None) or getattr(module, "run", None)
