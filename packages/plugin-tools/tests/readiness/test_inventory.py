"""Metadata doctor contracts, without plugin imports or service startup."""

import importlib.metadata
import json
import os
import subprocess
import sys

import pytest
import yaml
from openstudio_si_harness.cli import main
from openstudio_si_harness.export import export_plugin
from openstudio_si_harness.readiness import doctor

from ..support.plugins import capability, dependency, plugin


def test_standalone_identity_inventory_and_cli(tmp_path, capsys):
    root = plugin(tmp_path, "standalone")
    assert main(["plugin", "doctor", "--plugin-dir", str(root)]) == 0
    report = json.loads(capsys.readouterr().out)
    entry = report["plugins"][0]
    assert report["status"] == "ready" and report["scope"] == "metadata_only"
    assert entry["description"] and entry["owner"]["maintainers"]
    assert entry["inventory"] == {"skill_count": 1, "mcp_server_count": 0}
    assert entry["host_enabled"] == entry["service_connected"] == "unknown"
    assert entry["presence"] == "source"


def test_optional_core_does_not_block_independent_lookup(tmp_path):
    root = plugin(
        tmp_path,
        "standards",
        [dependency("core", required=False)],
        [capability("lookup"), capability("modeling", ["core"])],
    )
    report = doctor([root], plugin_id="standards", capability="lookup")
    capabilities = report["plugins"][0]["capabilities"]
    assert report["status"] == "ready"
    assert capabilities[0]["status"] == "ready"
    assert capabilities[1]["status"] == "unavailable"
    assert capabilities[1]["blocking_dependencies"] == ["core"]


@pytest.mark.parametrize(
    "version_spec,state", [(">=0.1.0 <0.2.0", "ready"), (">=0.2.0", "unavailable")]
)
def test_provider_version_range(tmp_path, version_spec, state):
    provider = plugin(tmp_path, "provider")
    consumer = plugin(tmp_path, "consumer", [dependency(version_spec=version_spec)])
    result = doctor([consumer, provider], plugin_id="consumer")
    assert result["status"] == state


def test_required_and_optional_cycles(tmp_path):
    first = plugin(tmp_path, "first", [dependency("second")])
    second = plugin(tmp_path, "second", [dependency("first")])
    result = doctor([first, second])
    assert result["status"] == "unavailable"
    assert result["cycles"] == [
        {"host": "source", "plugins": ["first", "second", "first"]}
    ]
    for root in (first, second):
        path = root / "openstudio-si-plugin.yaml"
        data = yaml.safe_load(path.read_text())
        data["dependencies"][0]["required"] = False
        path.write_text(yaml.safe_dump(data))
    result = doctor([first, second])
    assert result["status"] == "ready" and result["cycles"]


def test_separate_host_packages_are_independent(tmp_path):
    root = plugin(tmp_path, "provider", capabilities=[capability("lookup")])
    exports = export_plugin(root, tmp_path / "exports")["packages"]
    result = doctor(list(exports.values()), plugin_id="provider", capability="lookup")
    assert result["status"] == "ready"
    assert {entry["host"] for entry in result["plugins"]} == {"claude", "codex"}
    assert all(entry["status"] == "ready" for entry in result["plugins"])


def test_cache_discovery_and_missing_cache_is_not_proof_of_absence(
    tmp_path, monkeypatch
):
    root = plugin(tmp_path, "consumer", [dependency()])
    monkeypatch.setenv("HOME", str(tmp_path))
    cache = tmp_path / ".codex/plugins/cache/local/example/0.1.0"
    export_plugin(root, cache, ("codex",))
    result = doctor()
    assert result["status"] == "unknown"
    assert result["plugins"][0]["presence"] == "cached"
    assert result["plugins"][0]["dependencies"][0]["reason"] == "plugin_not_discovered"


def test_broken_hashes_do_not_hide_healthy_plugins(tmp_path):
    root = plugin(tmp_path, "healthy")
    exported = export_plugin(root, tmp_path / "exports")["packages"]["claude"]
    from pathlib import Path

    (Path(exported) / "skills/healthy/SKILL.md").write_text("tampered")
    result = doctor([root, exported])
    assert result["status"] == "unknown"
    assert result["plugins"][0]["status"] == "ready"
    assert result["plugins"][1]["reason"] == "invalid_or_unreadable_metadata"


def test_distribution_metadata_does_not_import_package(tmp_path, monkeypatch):
    root = plugin(
        tmp_path,
        "python-consumer",
        [
            dependency(
                kind="python-distribution",
                target="never-import-this-package",
                version_spec=">=1.0,<2",
            )
        ],
    )
    observed = []
    monkeypatch.setattr(
        importlib.metadata, "version", lambda name: observed.append(name) or "1.2.3"
    )
    result = doctor([root])
    assert result["status"] == "unknown"
    assert observed == []
    assert (
        result["plugins"][0]["dependencies"][0]["evidence"]["measurement_scope"]
        == "not_measured"
    )
    assert "never-import-this-package" not in sys.modules


def test_configuration_never_records_value_and_command_is_not_executed(
    tmp_path, monkeypatch
):
    marker = tmp_path / "executed"
    executable = tmp_path / "fake-command"
    executable.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("SI_READINESS_TEST_CONFIG", "private-configuration-value")
    root = plugin(
        tmp_path,
        "configured",
        [
            dependency(
                "config",
                kind="configuration",
                target="SI_READINESS_TEST_CONFIG",
                probe="configuration-present",
            ),
            dependency("command", kind="command", target="fake-command"),
        ],
    )
    result = doctor([root])
    assert result["status"] == "unknown" and not marker.exists()
    assert "private-configuration-value" not in json.dumps(result)


def test_service_and_dataset_remain_unknown_without_runtime_binding(tmp_path):
    root = plugin(
        tmp_path,
        "dataset-consumer",
        [dependency(kind="dataset", probe="dataset-metadata")],
    )
    assert doctor([root])["status"] == "unknown"
    provider = plugin(
        tmp_path, "provider", with_mcp=True, capabilities=[capability("lookup")]
    )
    data = yaml.safe_load((provider / "openstudio-si-plugin.yaml").read_text())
    server = next(iter(json.loads((provider / ".mcp.json").read_text())["mcpServers"]))
    consumer = plugin(
        tmp_path,
        "service-consumer",
        [
            dependency(),
            dependency(
                "server",
                kind="mcp-server",
                probe="mcp-tools",
                provider="provider",
                target=server,
            ),
        ],
    )
    assert data["id"] == "provider"
    result = doctor([provider, consumer], plugin_id="service-consumer")
    assert result["status"] == "unknown"
    assert (
        result["plugins"][0]["dependencies"][1]["reason"]
        == "runtime_probe_not_performed"
    )


def test_cli_unavailable_and_invalid_selection(tmp_path, capsys):
    root = plugin(tmp_path, "consumer", [dependency()])
    assert main(["plugin", "doctor", "--plugin-dir", str(root)]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "unavailable"
    assert main(["plugin", "doctor", "--capability", "lookup"]) == 1
    assert "requires --plugin-id" in capsys.readouterr().err


def test_malformed_metadata_is_unknown_without_echoing_values(tmp_path):
    root = plugin(tmp_path, "broken")
    (root / "openstudio-si-plugin.yaml").write_text("description: [private-value\n")
    report = doctor([root])
    assert report["status"] == "unknown"
    assert "private-value" not in json.dumps(report)


def test_missing_external_mcp_declaration_is_unavailable(tmp_path):
    provider = plugin(tmp_path, "provider")
    consumer = plugin(
        tmp_path,
        "consumer",
        [
            dependency(),
            dependency(
                "service",
                kind="mcp-server",
                probe="mcp-tools",
                target="absent",
                provider="provider",
            ),
        ],
    )
    report = doctor([provider, consumer], plugin_id="consumer")
    assert report["status"] == "unavailable"
    assert (
        report["plugins"][0]["dependencies"][1]["reason"] == "provider_mcp_not_declared"
    )


def test_installed_public_cli_does_not_import_modeling_runtime(tmp_path):
    root = plugin(tmp_path, "standalone")
    script = """
import sys
from openstudio_si.cli import main
status = main(['plugin', 'doctor', '--plugin-dir', sys.argv[1]])
assert 'openstudio' not in sys.modules
assert 'openstudio_si_mcp' not in sys.modules
raise SystemExit(status)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(root)],
        cwd=tmp_path,
        env={"PATH": os.defpath},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "ready"
