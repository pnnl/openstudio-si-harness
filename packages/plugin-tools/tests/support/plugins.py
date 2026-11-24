"""Shared source-plugin, dependency and capability builders."""

import yaml
from openstudio_si_harness.generator import init_project


def plugin(tmp_path, identity, dependencies=(), capabilities=None, with_mcp=False):
    project = tmp_path / identity
    init_project(
        project, identity=identity, license_identifier="MIT", with_mcp=with_mcp
    )
    root = project / "plugin"
    path = root / "openstudio-si-plugin.yaml"
    data = yaml.safe_load(path.read_text())
    data["dependencies"] = list(dependencies)
    if capabilities is not None:
        data["capabilities"] = capabilities
    path.write_text(yaml.safe_dump(data))
    return root


def dependency(identity="provider", *, required=True, **kwargs):
    return {
        "id": identity,
        "target": identity,
        "kind": "plugin",
        "probe": "metadata",
        "required": required,
        **kwargs,
    }


def capability(identity, requires=()):
    return {
        "id": identity,
        "name": identity,
        "description": "A reviewed task.",
        "requires": list(requires),
        "effects": ["read-data"],
    }
