"""Read standard plugin contents without importing or executing plugin code."""

import json
from pathlib import Path

import yaml

from .mcp import validate_server, override_variable


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys instead of silently overriding metadata."""


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ValueError(f"Invalid or duplicate YAML key: {key!r}")
        result[key] = loader.construct_object(value_node)
    return result


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping
)


def read_yaml(text: str):
    return yaml.load(text, Loader=UniqueKeyLoader)


def read_json(path: Path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key {key} in {path.name}")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)


def inspect_contents(root: Path) -> dict:
    skills = []
    names = set()
    for path in sorted((root / "skills").glob("*/SKILL.md")):
        content = path.read_text(encoding="utf-8")
        if not content.startswith("---\n") or "\n---\n" not in content[4:]:
            raise ValueError(f"Missing skill frontmatter: {path.relative_to(root)}")
        header = read_yaml(content[4:].split("\n---\n", 1)[0])
        if not isinstance(header, dict):
            raise ValueError(f"Invalid skill frontmatter: {path.relative_to(root)}")
        name = header.get("name")
        if name != path.parent.name or name in names:
            raise ValueError(
                f"Skill name must match its unique directory: {path.relative_to(root)}"
            )
        if (
            not isinstance(header.get("description"), str)
            or not header["description"].strip()
        ):
            raise ValueError(f"Missing skill description: {name}")
        names.add(name)
        skills.append(
            {
                "id": name,
                "description": header["description"],
                "path": path.relative_to(root).as_posix(),
            }
        )
    servers = {}
    config_path = root / ".mcp.json"
    if config_path.exists():
        config = read_json(config_path)
        if not isinstance(config, dict) or not isinstance(
            config.get("mcpServers"), dict
        ):
            raise ValueError(".mcp.json must declare an mcpServers object.")
        servers = config["mcpServers"]
        overrides = [override_variable(name) for name in servers]
        if len(set(overrides)) != len(overrides):
            raise ValueError(
                "MCP server names collide in executable override variables."
            )
        for name, entry in servers.items():
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("command"), str)
                or not entry["command"]
            ):
                raise ValueError(f"MCP server {name} must declare a stdio command.")
            if not isinstance(entry.get("args", []), list) or any(
                not isinstance(arg, str) for arg in entry.get("args", [])
            ):
                raise ValueError(f"Invalid arguments for MCP server {name}.")
            if "url" in entry or entry.get("type", "stdio") != "stdio":
                raise ValueError(f"MCP server {name}: v1 supports stdio.")
            env = entry.get("env", {})
            if not isinstance(env, dict) or any(
                not isinstance(value, str) for value in env.values()
            ):
                raise ValueError(f"Invalid environment for MCP server {name}.")
            validate_server(name, entry)
    return {
        "skills": skills,
        "mcp_servers": servers,
        "inventory": {
            "skill_count": len(skills),
            "mcp_server_count": len(servers),
        },
    }
