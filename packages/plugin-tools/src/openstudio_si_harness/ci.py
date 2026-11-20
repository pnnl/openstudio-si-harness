"""Configure thin, pinned calls to PNNL-owned reusable workflows."""

import hashlib
import json
import re
from importlib.resources import files
from pathlib import Path

from . import __version__

WORKFLOWS = ("validate.yml", "release-plugin.yml")


def configure_ci(
    project: Path,
    *,
    workflow_ref: str,
    tooling_version: str = __version__,
    dry_run: bool = False,
) -> dict:
    root = project.resolve()
    if not re.fullmatch("[0-9a-f]{40}", workflow_ref):
        raise ValueError(
            "Workflow ref must be an immutable full 40-character commit SHA."
        )
    if not re.fullmatch(
        r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tooling_version
    ):
        raise ValueError(
            "Tooling version must be an exact stable release, such as 0.4.4."
        )
    if tuple(map(int, tooling_version.split("."))) < (0, 4, 3):
        raise ValueError("Plugin development requires openstudio-si>=0.4.3.")
    state_path = root / ".openstudio-si/ci.json"
    state = json.loads(state_path.read_text())
    replacements = {}
    for name in WORKFLOWS:
        relative = ".github/workflows/" + name
        target = root / relative
        if target.is_symlink() or not target.resolve().is_relative_to(root):
            raise ValueError("Managed workflow must remain inside this project.")
        if (
            hashlib.sha256(target.read_bytes()).hexdigest()
            != state["managed_hashes"][relative]
        ):
            raise ValueError(
                f"Managed workflow was edited; review/reconcile before upgrading: {relative}"
            )
        template = (
            files("openstudio_si_harness")
            .joinpath("templates/ci", name + ".tmpl")
            .read_text()
        )
        replacements[relative] = template.replace(
            "@@WORKFLOW_REF@@", workflow_ref
        ).replace("@@TOOLING_VERSION@@", tooling_version)
    state.update(
        configured=True,
        workflow_ref=workflow_ref,
        tooling_version=tooling_version,
        managed_hashes={
            relative: hashlib.sha256(value.encode()).hexdigest()
            for relative, value in replacements.items()
            if relative.startswith(".github/")
        },
    )
    replacements[".openstudio-si/ci.json"] = json.dumps(state, indent=2) + "\n"
    for relative in replacements:
        target = root / relative
        if target.is_symlink() or not target.resolve().is_relative_to(root):
            raise ValueError("CI configuration must remain inside the project.")
    if not dry_run:
        original = {
            name: (root / name).read_bytes() if (root / name).exists() else None
            for name in replacements
        }
        written = []
        try:
            for name, content in replacements.items():
                (root / name).write_text(content, encoding="utf-8")
                written.append(name)
        except OSError:
            for name in reversed(written):
                if original[name] is None:
                    (root / name).unlink()
                else:
                    (root / name).write_bytes(original[name])
            raise
    return {
        "project": str(root),
        "configured": not dry_run,
        "workflow_ref": workflow_ref,
        "tooling_version": tooling_version,
        "files": sorted(replacements),
    }
