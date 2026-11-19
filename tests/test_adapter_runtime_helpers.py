from __future__ import annotations

import json
import py_compile
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli

from adapters.runtime_helpers import (
    render_doctor_runtime_script,
    render_install_runtime_script,
)
from openstudio_si_mcp.compatibility import PLUGIN_CONTRACT_VERSION, package_version


def _compile_script(tmp_path: Path, filename: str, content: str) -> None:
    script_path = tmp_path / filename
    script_path.write_text(content, encoding="utf-8")
    py_compile.compile(str(script_path), doraise=True)


def test_rendered_runtime_helpers_are_executable_python(tmp_path: Path) -> None:
    doctor = render_doctor_runtime_script()
    installer = render_install_runtime_script()

    _compile_script(tmp_path, "doctor_runtime.py", doctor)
    _compile_script(tmp_path, "install_runtime.py", installer)

    assert PLUGIN_CONTRACT_VERSION in doctor
    assert "def nlr_mcp_status" in doctor
    assert '"nlr_openstudio": nlr_mcp_status()' in doctor
    assert "optional_capabilities" in doctor
    assert '"--plugin-contract-version"' in doctor
    assert "return doctor.returncode or 1" in doctor
    assert "return 2" in doctor
    assert "core_ready" in doctor
    assert "plugin_ready" in doctor
    assert "newer OpenStudio SI MCP interface" in doctor
    assert "ready for energy modeling" in doctor
    assert 'runtime_cli, "install-runtime"' in installer
    assert "def runtime_command_path(command: str)" in installer
    assert "def runtime_cli_path" in installer
    assert 'scripts_dir / f"{command}.exe"' in installer
    assert 'runtime_mcp = runtime_command_path("openstudio-si-mcp")' in installer
    assert "requires Python 3.10 or newer" in installer
    assert f"openstudio-si=={package_version()}" in installer
    assert "def is_pipx_managed_runtime" in installer
    assert '["pipx", "upgrade", "--install", "openstudio-si"]' in installer
    assert "do not replace it with this absolute path" in installer
    assert '"-m", "cli"' not in installer
    assert (
        'print("\\nOpenStudio SI runtime installation completed.")\n    return 0'
        in installer
    )


def test_rendered_installer_includes_only_requested_host_guidance() -> None:
    without_guidance = render_install_runtime_script()
    with_guidance = render_install_runtime_script(
        post_install_guidance="Reload the host plugin."
    )

    assert "Reload the host plugin." not in without_guidance
    assert 'print("\\nReload the host plugin.")\n    return 0' in with_guidance


def test_exported_helpers_keep_project_venv_commands_separate_from_path(
    tmp_path: Path,
) -> None:
    venv = tmp_path / ".venv"
    scripts = venv / "bin"
    scripts.mkdir(parents=True)
    local_cli = scripts / "openstudio-si"
    local_cli.touch()
    selected_python = SimpleNamespace(prefix=str(venv), base_prefix="/machine")

    doctor_namespace = {"__name__": "test_doctor"}
    exec(render_doctor_runtime_script(), doctor_namespace)
    doctor_namespace["sys"] = selected_python
    doctor_namespace["shutil"] = SimpleNamespace(
        which=lambda command: f"/global/bin/{command}"
    )
    assert doctor_namespace["command_status"]("openstudio-si")["path"] == str(local_cli)
    assert doctor_namespace["command_status"]("openstudio-si-mcp")["path"] is None

    installer_namespace = {"__name__": "test_installer"}
    exec(render_install_runtime_script(), installer_namespace)
    installer_namespace["sys"] = selected_python
    installer_namespace["shutil"] = SimpleNamespace(
        which=lambda command: f"/global/bin/{command}"
    )
    assert installer_namespace["runtime_cli_path"]() == str(local_cli)
    assert installer_namespace["runtime_command_path"]("openstudio-si-mcp") is None
    assert installer_namespace["is_pipx_managed_runtime"]() is False


@pytest.mark.parametrize("implementation", ["cli", "exported"])
@pytest.mark.parametrize("host", ["codex", "claude", "claude_parent"])
@pytest.mark.parametrize(
    "names, expected",
    [
        (["openstudio-mcp"], "openstudio-mcp"),
        (["nlr_openstudio"], None),
        (["nlr_openstudio", "openstudio-mcp"], "openstudio-mcp"),
        (["other"], None),
        ([], None),
    ],
)
def test_nlr_discovery_names(
    monkeypatch, tmp_path: Path, implementation, host, names, expected
) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    if host == "codex":
        config_path = tmp_path / ".codex" / "config.toml"
        config_path.parent.mkdir()
        config_path.write_text(
            "# openstudio-mcp is optional\n"
            + "\n".join(
                f'[mcp_servers."{name}"]\ncommand = "docker"' for name in names
            ),
            encoding="utf-8",
        )
    else:
        config_path = (tmp_path if host == "claude_parent" else project) / ".mcp.json"
        config_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        name: {"description": "NLR OpenStudio-MCP"} for name in names
                    }
                }
            ),
            encoding="utf-8",
        )
    if implementation == "cli":
        status = cli._nlr_mcp_status()
    else:
        namespace = {"__name__": "test_doctor"}
        exec(
            compile(render_doctor_runtime_script(), "doctor_runtime.py", "exec"),
            namespace,
        )
        # Exercise discovery with a TOML parser even on Python 3.10.
        monkeypatch.setitem(namespace, "tomllib", cli.tomllib)
        status = namespace["nlr_mcp_status"]()

    assert status["configured"] is (expected is not None)
    if expected is not None:
        assert status["name"] == expected
        assert status["source"] == str(config_path)
    assert "ready" not in status


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_core_metadata_works_without_runtime(tmp_path, host):
    core = tmp_path / "core"
    manifest = core / f".{host}-plugin/plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"name": "openstudio-si", "version": package_version()})
    )
    config = core / ".mcp.json"
    environment = {
        "OPENSTUDIO_SI_PLUGIN_VERSION": package_version(),
        "OPENSTUDIO_SI_PLUGIN_CONTRACT_VERSION": PLUGIN_CONTRACT_VERSION,
    }
    config.write_text(
        json.dumps({"mcpServers": {"openstudio_si": {"env": environment}}})
    )
    helper = core / "skills/setup-openstudio-si/scripts/doctor_runtime.py"
    helper.parent.mkdir(parents=True)
    helper.write_text(render_doctor_runtime_script())
    command = [sys.executable, "-I", "-S", str(helper), "--core-metadata"]
    result = subprocess.run(command, env={"PATH": ""}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "ok": True,
        "plugin_version": package_version(),
        "plugin_contract_version": PLUGIN_CONTRACT_VERSION,
    }

    environment["OPENSTUDIO_SI_PLUGIN_CONTRACT_VERSION"] = "999.0"
    config.write_text(
        json.dumps({"mcpServers": {"openstudio_si": {"env": environment}}})
    )
    result = subprocess.run(command, env={"PATH": ""}, capture_output=True, text=True)
    assert result.returncode == 2, result.stderr
    assert not json.loads(result.stdout)["ok"]


def test_hvac_export_refuses_an_incompatible_core(tmp_path, monkeypatch):
    from adapters import hvac_plugin

    monkeypatch.setattr(hvac_plugin, "package_version", lambda: "0.5.0")
    with pytest.raises(ValueError, match="outside the HVAC dependency range"):
        hvac_plugin.export_hvac_plugin(
            tmp_path,
            tmp_path / "hvac",
            "claude",
            render_skill=lambda *_: "",
            copy_reference=lambda *_: None,
            dry_run=True,
        )
    assert not (tmp_path / "hvac").exists()


def test_hvac_forwarders_cover_split_release_hotfixes(tmp_path, monkeypatch):
    from adapters import hvac_plugin

    monkeypatch.setattr(hvac_plugin, "package_version", lambda: "0.4.4+hotfix")
    assert len(hvac_plugin.hvac_forwarders(tmp_path)) == 25
    monkeypatch.setattr(hvac_plugin, "package_version", lambda: "0.5.0")
    assert hvac_plugin.hvac_forwarders(tmp_path) == {}


def test_hvac_release_version_precedence():
    from harness.plugins import semver_precedence

    assert semver_precedence("0.1.0-rc.2+build.9") < semver_precedence("0.1.0-rc.10")
    assert semver_precedence("0.1.0-rc.10") < semver_precedence("0.1.0")
    assert semver_precedence("0.1.0+build.10") == semver_precedence("0.1.0")


def test_hvac_core_ranges_use_npm_prerelease_rules():
    from harness.plugins import supports_hvac_core

    assert supports_hvac_core("0.4.3+build.2", "0.4.3", "0.5.0")
    assert not supports_hvac_core("0.5.0-rc.1", "0.4.3", "0.5.0")
    assert not supports_hvac_core("invalid", "0.4.3", "0.5.0")
