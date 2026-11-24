"""Build independent host packages from one plugin source directory."""

import json
from pathlib import Path
import shutil
import tempfile
import hashlib
import sys

from . import __version__, HARNESS_API_VERSION
from .mcp import host_servers

from .manifest import load_plugin, validate_manifest_schema
from .content import safe_relative

HOSTS = ("claude", "codex")
DESCRIPTOR = "openstudio-si-export.json"


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def export_plugin(source: Path, output: Path, hosts: tuple[str, ...] = HOSTS) -> dict:
    """Validate first, stage all host outputs, and publish to a new output directory."""
    if not hosts or len(set(hosts)) != len(hosts) or set(hosts) - set(HOSTS):
        raise ValueError(f"Supported host targets are {HOSTS}.")
    root = source.resolve()
    destination = output.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"Output already exists: {output}")
    if destination.resolve().is_relative_to(root):
        raise ValueError("Export output must be outside the plugin source directory.")
    plugin = load_plugin(root)
    manifest = plugin["manifest"]
    if plugin["mcp_servers"] and sys.platform not in {"darwin", "linux"}:
        raise ValueError("MCP launcher exports support macOS and Linux only.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".si-export-", dir=destination.parent
    ) as stage:
        staged = Path(stage) / "package"
        for host in hosts:
            package = staged / host / manifest["id"]
            package.mkdir(parents=True)
            for relative in plugin["files"]:
                target = package / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / relative, target)
            # Empty evaluation directories are valid template inputs too.
            (package / manifest["evaluation"]["cases"]).mkdir(
                parents=True, exist_ok=True
            )
            shutil.copyfile(
                root / "openstudio-si-plugin.yaml",
                package / "openstudio-si-plugin.yaml",
            )
            metadata = host_metadata(manifest, host)
            if plugin["mcp_servers"]:
                _write_json(
                    package / ".mcp.json",
                    {"mcpServers": host_servers(plugin["mcp_servers"])},
                )
                if host == "codex":
                    metadata["mcpServers"] = "./.mcp.json"
            _write_json(package / f".{host}-plugin" / "plugin.json", metadata)
            _write_json(
                package / DESCRIPTOR,
                {
                    "format": "openstudio-si-export/1",
                    "tool_version": __version__,
                    "harness_api_version": HARNESS_API_VERSION,
                    "host": host,
                    "manifest": manifest,
                    "source_mcp_servers": plugin["mcp_servers"],
                    "supported_platforms": ["darwin", "linux"]
                    if plugin["mcp_servers"]
                    else ["any"],
                    "files": file_hashes(package),
                },
            )
            validate_export(package)
        staged.rename(destination)
    return {
        "id": manifest["id"],
        "version": manifest["version"],
        "inventory": plugin["inventory"],
        "packages": {host: str(destination / host / manifest["id"]) for host in hosts},
    }


def host_metadata(manifest: dict, host: str) -> dict:
    metadata = {
        "name": manifest["id"],
        "version": manifest["version"],
        "description": manifest["description"],
        "author": {"name": manifest["owner"]["organization"]},
        "license": manifest["license"],
    }
    if host == "claude":
        metadata["displayName"] = manifest["name"]
        dependencies = [
            {
                "name": dependency["target"],
                **(
                    {"version": dependency["version_spec"]}
                    if dependency.get("version_spec")
                    else {}
                ),
            }
            for dependency in manifest["dependencies"]
            if dependency["kind"] == "plugin" and dependency["required"]
        ]
        if dependencies:
            metadata["dependencies"] = dependencies
    else:
        metadata["skills"] = "./skills/"
        metadata["interface"] = {
            "displayName": manifest["name"],
            "shortDescription": manifest["description"],
        }
    return metadata


def file_hashes(root: Path) -> dict:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Export contains a symlink.")
        if path.is_file() and path != root / DESCRIPTOR:
            result[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return result


def validate_export(directory: Path) -> dict:
    """Check content consistency, then re-run source validation in isolation.

    Hashes detect changes, not malicious replacement of the complete descriptor.
    Marketplace provenance/signatures are a separate release responsibility.
    """
    from .discovery import read_json

    root = directory.resolve()
    if (root / DESCRIPTOR).is_symlink():
        raise ValueError("Export descriptor must not be a symlink.")
    descriptor = read_json(root / DESCRIPTOR)
    if not isinstance(descriptor, dict) or set(descriptor) != {
        "format",
        "tool_version",
        "harness_api_version",
        "host",
        "manifest",
        "source_mcp_servers",
        "supported_platforms",
        "files",
    }:
        raise ValueError("Invalid exported package descriptor fields.")
    if (
        not isinstance(descriptor["manifest"], dict)
        or not isinstance(descriptor["files"], dict)
        or not isinstance(descriptor["source_mcp_servers"], dict)
    ):
        raise ValueError("Invalid exported package descriptor values.")
    validate_manifest_schema(descriptor["manifest"])
    if (
        descriptor.get("format") != "openstudio-si-export/1"
        or descriptor.get("host") not in HOSTS
    ):
        raise ValueError("Invalid exported package descriptor.")
    if not descriptor.get("tool_version") or not descriptor.get("harness_api_version"):
        raise ValueError("Export is missing tool/API versions.")
    expected_platforms = (
        ["darwin", "linux"] if descriptor["source_mcp_servers"] else ["any"]
    )
    if descriptor["supported_platforms"] != expected_platforms:
        raise ValueError("Invalid exported package platform declaration.")
    if expected_platforms != ["any"] and sys.platform not in expected_platforms:
        raise ValueError("MCP launcher package supports macOS and Linux only.")
    if descriptor.get("files") != file_hashes(root):
        raise ValueError("Export file hashes do not match the package contents.")
    with tempfile.TemporaryDirectory(prefix="si-verify-") as stage:
        source = Path(stage)
        for relative in descriptor["files"]:
            if relative == ".mcp.json" or relative.startswith(
                (".claude-plugin/", ".codex-plugin/")
            ):
                continue
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / relative, target)
        (source / safe_relative(descriptor["manifest"]["evaluation"]["cases"])).mkdir(
            parents=True, exist_ok=True
        )
        if descriptor.get("source_mcp_servers"):
            _write_json(
                source / ".mcp.json", {"mcpServers": descriptor["source_mcp_servers"]}
            )
        plugin = load_plugin(source)
        if plugin["manifest"] != descriptor["manifest"]:
            raise ValueError("Export manifest disagrees with source metadata.")
        expected_mcp = host_servers(plugin["mcp_servers"])
        actual_mcp = (
            read_json(root / ".mcp.json")["mcpServers"]
            if (root / ".mcp.json").exists()
            else {}
        )
        if expected_mcp != actual_mcp:
            raise ValueError("Export MCP configuration disagrees with source metadata.")
        host = descriptor["host"]
        metadata = read_json(root / f".{host}-plugin/plugin.json")
        expected_metadata = host_metadata(plugin["manifest"], host)
        if expected_mcp and host == "codex":
            expected_metadata["mcpServers"] = "./.mcp.json"
        if metadata != expected_metadata:
            raise ValueError("Host metadata disagrees with source metadata.")
    return plugin
