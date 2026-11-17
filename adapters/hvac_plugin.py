"""Export the HVAC specialist alongside the OpenStudio SI core plugin."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

from harness.asset_manifest import (
    reference_exports_for_host,
    resource_exports_for_host,
    sdk_reference_aliases,
    skill_exports_for_host,
)
from harness.plugin_sdk import write_resources
from harness.plugins import (
    HVAC_CORE_RANGE,
    HVAC_DESCRIPTION,
    HVAC_FORWARDING_BEFORE,
    HVAC_FORWARDING_RELEASE,
    HVAC_PLUGIN_ID,
    HVAC_PLUGIN_NAME,
    HVAC_RELEASE,
    HVAC_SKILLS,
    HVAC_VERSION,
    PLUGIN_DESCRIPTOR,
    supports_hvac_core,
)
from openstudio_si_mcp.compatibility import PLUGIN_CONTRACT_VERSION, package_version


def export_hvac_plugin(
    workspace_root: Path,
    directory: Path,
    host: str,
    *,
    render_skill: Callable[[Path, str], str],
    copy_reference: Callable[[Path, Path], None],
    dry_run: bool,
    core_plugin_name: str = "openstudio-si",
) -> list[Path]:
    core_version = package_version()
    if not supports_hvac_core(
        core_version, HVAC_RELEASE["core_min"], HVAC_RELEASE["core_before"]
    ):
        raise ValueError(
            f"OpenStudio SI core {core_version} is outside the HVAC dependency range ({HVAC_CORE_RANGE})."
        )
    skills = skill_exports_for_host(
        workspace_root, directory, host, skill_ids=HVAC_SKILLS
    )
    if {skill.skill for skill in skills} != HVAC_SKILLS:
        raise ValueError(f"The {host} export is missing registered HVAC skills.")
    references = reference_exports_for_host(
        workspace_root, directory, host, skill_ids=HVAC_SKILLS
    )
    resources = resource_exports_for_host(
        workspace_root,
        directory,
        host,
        skill_ids=HVAC_SKILLS,
        plugin_version=HVAC_VERSION,
        plugin_name=HVAC_PLUGIN_NAME,
    )
    manifest_path = directory / f".{host}-plugin" / "plugin.json"
    descriptor_path = directory / PLUGIN_DESCRIPTOR
    files = [
        manifest_path,
        descriptor_path,
        *(export.target for export in [*skills, *references, *resources]),
    ]
    if dry_run:
        return files

    manifest = {
        "name": HVAC_PLUGIN_ID,
        "version": HVAC_VERSION,
        "description": HVAC_DESCRIPTION,
        "author": {"name": "OpenStudio SI"},
    }
    if host == "codex":
        manifest["skills"] = "./skills/"
        manifest["interface"] = {
            "displayName": HVAC_PLUGIN_NAME,
            "shortDescription": "HVAC system design, sizing, topology, and equipment edits.",
            "category": "Engineering",
        }
    else:
        manifest["displayName"] = HVAC_PLUGIN_NAME
        manifest["dependencies"] = [
            {"name": core_plugin_name, "version": HVAC_CORE_RANGE}
        ]

    requirements = json.loads(
        (workspace_root / "skills/sdk_scripts/compatibility.json").read_text(
            encoding="utf-8"
        )
    )
    descriptor = {
        "id": HVAC_PLUGIN_ID,
        "name": HVAC_PLUGIN_NAME,
        "description": HVAC_DESCRIPTION,
        "version": HVAC_VERSION,
        "core_contract_version": PLUGIN_CONTRACT_VERSION,
        "core_min": HVAC_RELEASE["core_min"],
        "core_before": HVAC_RELEASE["core_before"],
        "required_openstudio_version": requirements["required_openstudio_version"],
        "skills": sorted(HVAC_SKILLS),
    }
    for path, data in ((manifest_path, manifest), (descriptor_path, descriptor)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    for export in skills:
        export.target.parent.mkdir(parents=True, exist_ok=True)
        export.target.write_text(
            _render_hvac_skill(render_skill(export.source, export.skill), export.skill),
            encoding="utf-8",
        )
    for export in references:
        copy_reference(export.source, export.target)
    write_resources(
        resources,
        directory,
        aliases=sdk_reference_aliases(workspace_root, host, skill_ids=HVAC_SKILLS),
    )
    return files


def _render_hvac_skill(content: str, skill: str) -> str:
    header, frontmatter, body = content.split("---", 2)
    frontmatter, replaced = re.subn(
        r"(?m)^( +)?version: .+$",
        lambda match: f"{match[1] or ''}version: {HVAC_VERSION}",
        frontmatter,
    )
    if not replaced:
        if "\nmetadata:\n" in frontmatter:
            frontmatter = frontmatter.replace(
                "\nmetadata:\n", f"\nmetadata:\n  version: {HVAC_VERSION}\n", 1
            )
        else:
            frontmatter += f"metadata:\n  version: {HVAC_VERSION}\n"

    gate = f"""## Core preflight

Before using this capability, locate the active OpenStudio SI core plugin via
its `delegated-nlr-modeling` skill in this session. Use host Python to run
`<core-plugin-root>/skills/setup-openstudio-si/scripts/doctor_runtime.py --core-metadata`.
Require `ok: true`, `plugin_version` {HVAC_CORE_RANGE}, and
`plugin_contract_version` {PLUGIN_CONTRACT_VERSION}. If the core plugin is missing
or incompatible, stop the covered HVAC workflow and ask the user to enable
or update it. Do not draft a replacement SDK script.

Local topology inspection and bundled SDK edits use this disk check and the owning
skill's local OpenStudio preflight. A live OpenStudio SI MCP connection is required
for simulation and results. For those steps, use `runtime_plugin_compatibility`
to verify the connected core plugin and runtime are both {HVAC_CORE_RANGE} and
declare the compatible contract. Keep the existing provider-selection rules.

"""
    if skill == "hvac-modeler":
        gate += "Apply this gate before selecting a capability for an HVAC request.\n\n"
    return f"{header}---{frontmatter}---\n\n{gate}{body.lstrip()}"


def hvac_forwarders(core_dir: Path) -> dict[Path, str]:
    """Keep pre-split skill names callable for the first split release."""
    if not supports_hvac_core(
        package_version(), HVAC_FORWARDING_RELEASE, HVAC_FORWARDING_BEFORE
    ):
        return {}
    return {
        core_dir / "skills" / skill / "SKILL.md": f"""---
name: {skill}
description: Forward the retired core HVAC skill name to OpenStudio SI HVAC Modeler.
---

# This HVAC workflow moved

Continue with `{HVAC_PLUGIN_ID}:{skill}` in OpenStudio SI HVAC Modeler. On a host
that shows both skill copies, select the skill from the `{HVAC_PLUGIN_ID}` plugin
and use its skill directory. Keep the saved session and model lineage. Recheck
the saved plan, inputs, and approval against the specialist skill before applying.

If the specialist is unavailable or fails its core preflight, stop the covered
HVAC workflow and ask the user to enable or update the required plugin. Do not
draft or run an ad hoc script for this operation.
"""
        for skill in sorted(HVAC_SKILLS - {"hvac-modeler"})
    }
