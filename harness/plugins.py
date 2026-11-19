"""OpenStudio SI plugin catalog and read-only readiness checks."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from semantic_version import Version

from harness.plugin_sdk import encoded
from openstudio_si_mcp.compatibility import PLUGIN_CONTRACT_VERSION

CORE_DESCRIPTION = (
    "Coordinate OpenStudio modeling, select providers, manage sessions and artifacts, "
    "run simulations, and query results."
)
HVAC_PLUGIN_ID = "openstudio-si-hvac"
HVAC_PLUGIN_NAME = "OpenStudio SI HVAC Modeler"
HVAC_DESCRIPTION = (
    "Create and size VAV reheat, packaged VAV, and prototype CAV systems, "
    "inspect HVAC topology, and edit air-side and plant equipment and controls."
)
HVAC_RELEASE = json.loads(
    Path(__file__).with_name("hvac_release.json").read_text(encoding="utf-8")
)
HVAC_VERSION = HVAC_RELEASE["version"]
HVAC_CORE_RANGE = f">={HVAC_RELEASE['core_min']} <{HVAC_RELEASE['core_before']}"
HVAC_FORWARDING_RELEASE = "0.4.3"
HVAC_FORWARDING_BEFORE = "0.5.0"
PLUGIN_DESCRIPTOR = "openstudio-si-plugin.json"
HVAC_SKILLS = frozenset(
    {
        "hvac-modeler",
        "add-vav-reheat",
        "hvac-sizing-assistant",
        "openstudio-boiler-performance-editor",
        "openstudio-cav-system-creator",
        "openstudio-chiller-performance-editor",
        "openstudio-coil-replacer",
        "openstudio-district-capacity-editor",
        "openstudio-economizer-editor",
        "openstudio-heat-recovery-editor",
        "openstudio-hvac-remover",
        "openstudio-hvac-visualizer",
        "openstudio-outdoor-air-connector",
        "openstudio-plant-equipment-replacer",
        "openstudio-plant-loop-creator",
        "openstudio-pump-performance-editor",
        "openstudio-pump-replacer",
        "openstudio-supply-fan-performance-editor",
        "openstudio-supply-fan-replacer",
        "openstudio-terminal-editor",
        "openstudio-terminal-replacer",
        "openstudio-tower-performance-editor",
        "openstudio-vav-reheat-system-creator",
        "openstudio-ventilation-editor",
        "openstudio-water-coil-connector",
        "openstudio-water-coil-editor",
    }
)


def hvac_release_fingerprint(export_root: Path) -> str:
    """Fingerprint both shipped HVAC plugins, including generated files."""
    files = {
        path.relative_to(export_root).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for plugin in (
            export_root / HVAC_PLUGIN_ID,
            export_root / "plugins" / HVAC_PLUGIN_ID,
        )
        for path in plugin.rglob("*")
        if path.is_file()
    }
    return hashlib.sha256(encoded(files)).hexdigest()


def semver_precedence(version: str) -> Version:
    """Order releases using the same SemVer library as public plugin tools."""
    Version(version)  # Validate build metadata before ignoring it for precedence.
    return Version(version.partition("+")[0])


def supports_hvac_core(version: str | None, minimum: str, before: str) -> bool:
    if version is None:
        return False
    try:
        actual, lower, upper = (
            semver_precedence(value) for value in (version, minimum, before)
        )
        # The legacy HVAC contract uses stable bounds and excludes prereleases.
        return (
            not any(value.prerelease for value in (actual, lower, upper))
            and lower <= actual < upper
        )
    except ValueError:
        return False


def read_plugin_descriptor(directory: Path) -> dict[str, Any]:
    """Check the HVAC package on disk without executing any of its scripts."""
    data = json.loads((directory / PLUGIN_DESCRIPTOR).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("id") != HVAC_PLUGIN_ID:
        raise ValueError(f"Expected a {HVAC_PLUGIN_ID} plugin directory.")
    for key in (
        "version",
        "core_contract_version",
        "required_openstudio_version",
        "core_min",
        "core_before",
    ):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError(f"Missing or invalid {key} in {PLUGIN_DESCRIPTOR}.")
    if not supports_hvac_core(data["core_min"], data["core_min"], data["core_before"]):
        raise ValueError(
            "The HVAC plugin descriptor has an invalid core version range."
        )
    skills = data.get("skills")
    if (
        not isinstance(skills, list)
        or "hvac-modeler" not in skills
        or any(
            not isinstance(skill, str)
            or not skill.isascii()
            or not skill.replace("-", "").isalnum()
            for skill in skills
        )
    ):
        raise ValueError("The HVAC plugin descriptor has an invalid skill list.")
    for skill in skills:
        if not (directory / "skills" / skill / "SKILL.md").is_file():
            raise ValueError(f"Missing HVAC skill {skill}.")
    library = (directory / "sdk").resolve()
    bundle = json.loads((library / "bundle.json").read_text(encoding="utf-8"))
    if not isinstance(bundle, dict):
        raise TypeError("Invalid SDK bundle manifest.")
    files = bundle.get("files")
    if (
        bundle.get("layout_version") != 1
        or not isinstance(files, dict)
        or "compatibility.json" not in files
        or hashlib.sha256(encoded(files)).hexdigest() != bundle.get("bundle_id")
    ):
        raise ValueError("Invalid SDK bundle identity.")
    for relative, expected in files.items():
        payload = library / relative
        if (
            payload.is_symlink()
            or not payload.resolve().is_relative_to(library)
            or hashlib.sha256(payload.read_bytes()).hexdigest() != expected
        ):
            raise ValueError(f"Missing or mismatched SDK file {relative}.")
    requirements = json.loads(
        (library / "compatibility.json").read_text(encoding="utf-8")
    )
    if (
        not isinstance(requirements, dict)
        or requirements.get("required_openstudio_version")
        != data["required_openstudio_version"]
    ):
        raise ValueError(
            "The plugin and SDK bundle require different OpenStudio versions."
        )
    return data


def cached_hvac_plugins() -> list[Path]:
    """Find candidate packages without inferring which plugins the host enabled."""
    roots = (
        Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"),
        Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"),
    )
    return sorted(
        {
            descriptor.parent.resolve()
            for root in roots
            for descriptor in (root / "plugins" / "cache").glob(
                f"*/{HVAC_PLUGIN_ID}/*/{PLUGIN_DESCRIPTOR}"
            )
            if descriptor.is_file()
        }
    )


def plugin_readiness(
    checks: dict[str, Any], plugin_dir: Path | None
) -> dict[str, dict[str, Any]]:
    """Report core and optional specialist readiness using current host evidence."""
    core_ready = checks["core_ready"]
    core = {
        "name": "OpenStudio SI",
        "description": CORE_DESCRIPTION,
        "version": checks["version"],
        "ready": core_ready,
        "status": "ready" if core_ready else "runtime_not_ready",
        "message": "Core runtime checks passed."
        if core_ready
        else "See core diagnostics.",
    }
    hvac = {
        "name": HVAC_PLUGIN_NAME,
        "description": HVAC_DESCRIPTION,
        "ready": False,
        "status": "not_detected",
        "message": "If the HVAC Modeler skill is visible in this host, pass its plugin root with --plugin-dir.",
    }
    if plugin_dir is not None:
        hvac.update(_hvac_readiness(checks, plugin_dir))
    elif candidates := cached_hvac_plugins():
        hvac.update(
            status="cached_unverified",
            candidates=[str(path) for path in candidates],
            message="An HVAC package is cached. Check the active host's skills and pass its root with --plugin-dir.",
        )
    return {"openstudio-si": core, HVAC_PLUGIN_ID: hvac}


def _hvac_readiness(checks: dict[str, Any], directory: Path) -> dict[str, Any]:
    try:
        data = read_plugin_descriptor(directory)
    except (OSError, TypeError, ValueError) as exc:
        return {"status": "package_incomplete", "message": str(exc)}

    result: dict[str, Any] = {"version": data["version"]}
    required = data["required_openstudio_version"]
    version_probe = checks["openstudio"].get("version_probe", {})
    observed = version_probe.get("openstudio_version") or ""
    release = observed.partition("+")[0]
    if data["core_contract_version"] != PLUGIN_CONTRACT_VERSION:
        result.update(
            status="incompatible_core",
            message=(
                f"This plugin requires core MCP contract {data['core_contract_version']}."
            ),
        )
    elif not all(
        supports_hvac_core(version, data["core_min"], data["core_before"])
        for version in (
            checks["version"],
            checks.get("plugin_compatibility", {}).get("plugin_version"),
        )
    ):
        result.update(
            status="incompatible_core",
            message=f"Enable OpenStudio SI core and runtime >={data['core_min']} <{data['core_before']} in this host.",
        )
    elif not checks["core_ready"]:
        result.update(
            status="core_not_ready", message="Resolve the core runtime checks."
        )
    elif release != required:
        result.update(
            status="incompatible_openstudio",
            message=(f"Local HVAC capabilities require OpenStudio CLI {required}."),
        )
    else:
        result.update(
            ready=True,
            status="ready",
            message=(
                "Package and runtime checks passed. Run the owning capability's preflight before editing."
            ),
        )
    return result
