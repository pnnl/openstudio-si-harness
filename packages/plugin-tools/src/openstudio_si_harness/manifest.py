"""Load and validate the public plugin manifest and its discovered contents."""

import json
from importlib.resources import files
from pathlib import Path

from jsonschema import Draft202012Validator

from . import HARNESS_API_VERSION
from .content import selected_files
from .compatibility import satisfies, semver, validate_range
from .discovery import inspect_contents, read_yaml

MANIFEST = "openstudio-si-plugin.yaml"
PROBES = {
    "plugin": {"metadata"},
    "python-distribution": {"metadata"},
    "command": {"metadata", "command-version"},
    "mcp-server": {"mcp-initialize", "mcp-tools"},
    "dataset": {"dataset-metadata"},
    "configuration": {"configuration-present"},
}


def load_plugin(directory: Path) -> dict:
    root = directory.resolve()
    from .export import DESCRIPTOR, validate_export

    if (root / DESCRIPTOR).exists():
        return validate_export(root)
    if any(
        (root / f".{host}-plugin/plugin.json").exists() for host in ("claude", "codex")
    ):
        raise ValueError("Exported package is missing its export descriptor.")
    if not root.is_dir():
        raise ValueError(f"Plugin source is not a directory: {directory}")

    if (root / MANIFEST).is_symlink():
        raise ValueError("Manifest must not be a symlink.")
    data = read_yaml((root / MANIFEST).read_text(encoding="utf-8"))
    schema = json.loads(
        files("openstudio_si_harness")
        .joinpath("schemas/plugin.schema.json")
        .read_text()
    )
    errors = sorted(
        Draft202012Validator(schema).iter_errors(data),
        key=lambda error: str(error.json_path),
    )
    if errors:
        raise ValueError(
            "; ".join(f"{error.json_path}: {error.message}" for error in errors)
        )
    semver(data["version"])
    if data["id"] in {"openstudio-si", "openstudio-si-hvac"}:
        raise ValueError(
            "Reserved plugin ID. Use the existing core/HVAC exporters until their contract migration."
        )
    selected = selected_files(root, data)
    api_range = data["compatibility"]["harness_api"]
    if not satisfies(HARNESS_API_VERSION, api_range):
        raise ValueError(
            f"Harness API {HARNESS_API_VERSION} does not satisfy {api_range}."
        )
    dependencies = {}
    for dependency in data["dependencies"]:
        identity = dependency["id"]
        if identity in dependencies:
            raise ValueError(f"Duplicate dependency ID: {identity}")
        dependencies[identity] = dependency
        if dependency["probe"] not in PROBES[dependency["kind"]]:
            raise ValueError(f"Unsupported probe for dependency {identity}.")
        if dependency.get("version_spec"):
            validate_range(dependency["kind"], dependency["version_spec"])
        if dependency["kind"] == "plugin" and dependency["target"] == data["id"]:
            raise ValueError("A plugin cannot depend on itself.")
    capability_ids = set()
    for capability in data["capabilities"]:
        if capability["id"] in capability_ids:
            raise ValueError(f"Duplicate capability ID: {capability['id']}")
        capability_ids.add(capability["id"])
        required = set(capability["requires"])
        optional = set(capability.get("optional", []))
        if required & optional:
            raise ValueError(
                f"Capability {capability['id']} repeats a required dependency as optional."
            )
        missing = (required | optional) - dependencies.keys()
        if missing:
            raise ValueError(
                f"Unknown dependencies for {capability['id']}: {sorted(missing)}"
            )
    cases = (root / data["evaluation"]["cases"]).resolve()
    if not cases.is_relative_to(root) or not cases.is_dir():
        raise ValueError(
            "Evaluation cases must identify a directory inside the plugin source."
        )
    discovered = inspect_contents(root)
    if not discovered["skills"]:
        raise ValueError("A plugin must contain at least one skills/*/SKILL.md.")
    for dependency in dependencies.values():
        if (
            dependency["kind"] == "mcp-server"
            and dependency["target"] not in discovered["mcp_servers"]
            and not (
                dependency.get("provider") in dependencies
                and dependencies[dependency["provider"]]["kind"] == "plugin"
                and (
                    not dependency["required"]
                    or dependencies[dependency["provider"]]["required"]
                )
            )
        ):
            raise ValueError(
                f"Dependency {dependency['id']} needs a local MCP declaration or a declared plugin provider."
            )
    for dependency in dependencies.values():
        if "provider" in dependency and (
            dependency["kind"] != "mcp-server"
            or dependency["provider"] not in dependencies
            or dependencies[dependency["provider"]]["kind"] != "plugin"
        ):
            raise ValueError(f"Invalid plugin provider for {dependency['id']}")
    if "inventory" in data and data["inventory"] != discovered["inventory"]:
        raise ValueError(
            f"Inventory does not match discovered files: {discovered['inventory']}"
        )
    return {
        "manifest": {**data, "inventory": discovered["inventory"]},
        "files": selected,
        **discovered,
    }
