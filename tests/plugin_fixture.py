"""Relocate the deployment unit while retaining a convenient skill path."""

from pathlib import Path
import shutil


def copy_plugin_for_skill(skill: Path, destination: Path) -> Path:
    assert skill.parent.name == "skills"
    shutil.copytree(skill.parent.parent, destination)
    return destination / "skills" / skill.name


def hvac_export_root(export: Path, host: str) -> Path:
    if host == "codex":
        return export / "plugins/openstudio-si-hvac"
    return export / "openstudio-si-hvac"
