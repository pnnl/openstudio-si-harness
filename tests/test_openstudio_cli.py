from __future__ import annotations

import json
import os
import subprocess
import sys
import sqlite3
import stat
from pathlib import Path

import cli
from cli import main
from harness.plugins import (
    HVAC_CORE_RANGE,
    HVAC_FORWARDING_BEFORE,
    HVAC_FORWARDING_RELEASE,
    HVAC_PLUGIN_ID,
    HVAC_RELEASE,
    HVAC_VERSION,
    hvac_release_fingerprint,
    HVAC_SKILLS,
    PLUGIN_DESCRIPTOR,
    plugin_readiness,
    semver_precedence,
    supports_hvac_core,
)
from openstudio_si_mcp.runtime.learning_store import LearningStore


def test_cli_version_prints_version(capsys) -> None:
    assert main(["version"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip()


def test_cli_paths_json_reports_runtime_paths(capsys) -> None:
    assert main(["paths", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "harness_root" in payload["paths"]
    assert "data_dir" in payload["paths"]
    assert "workspace_dir" in payload["paths"]


def test_cli_doctor_json_reports_mcp_readiness(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    from openstudio_si_mcp.sdk_docs import lookup as sdk_docs_lookup

    # This test's documented class counts belong to the bundled 3.10 fixture.
    monkeypatch.setattr(sdk_docs_lookup, "OPENSTUDIO_SDK_DOCS_VERSION", "3.10.0")
    data_dir = tmp_path / "runtime-data"
    (data_dir / "workspace").mkdir(parents=True)
    monkeypatch.setenv("OPENSTUDIO_SI_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPENSTUDIO_PATH", "/fake/openstudio")
    monkeypatch.setattr(
        cli,
        "resolve_openstudio_executable_with_source",
        lambda: ("/fake/openstudio", "OPENSTUDIO_PATH"),
    )

    def fake_command_available(command: str) -> dict:
        return {"command": command, "available": True, "path": command}

    def fake_run_probe(command: list[str], *, timeout_seconds: int = 10) -> dict:
        stdout = "3.10.0+test" if command[0] == "/fake/openstudio" else "help"
        return {"ok": True, "returncode": 0, "stdout": stdout, "stderr": ""}

    monkeypatch.setattr(cli, "_command_available", fake_command_available)
    monkeypatch.setattr(cli, "_run_probe", fake_run_probe)

    exit_code = main(["doctor", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ready"] is True
    assert payload["core_ready"] is True
    assert payload["commands"]["openstudio"]["path"] == "/fake/openstudio"
    assert payload["mcp_ready"] is True
    assert payload["simulation_ready"] is True
    assert payload["assets"]["ok"] is True
    assert payload["runtime_storage"]["ok"] is True
    assert payload["runtime_storage"]["writable"] is True
    assert payload["sqlite_registry"]["ok"] is True
    assert payload["mcp_startup"]["ok"] is True
    assert payload["python_openstudio"]["ok"] is True
    assert payload["measures"]["ok"] is True
    assert payload["measures"]["count"] >= 1
    assert payload["sdk_docs"]["ok"] is True
    assert payload["sdk_docs"]["source"] == "bundled"
    assert payload["sdk_docs"]["selected_version"] == "3.10.0"
    assert payload["sdk_docs"]["document_probe"]["class_count"] == 863
    assert (
        payload["sdk_docs"]["document_probe"]["documented_openstudio_version"]
        == "3.10.0"
    )
    assert "openstudio" in payload


def test_cli_doctor_reports_uninitialized_storage_without_creating_it(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    data_dir = tmp_path / "runtime-data"
    monkeypatch.setenv("OPENSTUDIO_SI_DATA_DIR", str(data_dir))

    assert main(["doctor", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["runtime_storage"]["initialized"] is False
    assert payload["runtime_storage"]["ok"] is False
    assert not data_dir.exists()


def test_cli_install_runtime_initializes_storage(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    data_dir = tmp_path / "runtime-data"
    monkeypatch.setenv("OPENSTUDIO_SI_DATA_DIR", str(data_dir))

    assert main(["install-runtime"]) == 0
    captured = capsys.readouterr()
    assert "Initialized runtime workspace" in captured.out
    assert (data_dir / "workspace").is_dir()


def test_cli_learning_curates_measure_candidates_and_requires_prune_confirmation(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    data_dir = tmp_path / "runtime-data"
    monkeypatch.setenv("OPENSTUDIO_SI_DATA_DIR", str(data_dir))
    store = LearningStore(data_dir / "learning.sqlite")
    for workflow_id in ("workflow-1", "workflow-2", "workflow-3"):
        store.capture_event(
            event_type="script_execution",
            summary="Applied daylighting controls to perimeter zones.",
            source="claude_code",
            workflow_id=workflow_id,
            scope={"tags": ["daylighting", "geometry"]},
            evidence={
                "script_fingerprint": "daylighting-controls-v1",
                "outcome": "success",
            },
        )

    assert main(["learning", "propose-measures", "--json"]) == 0
    curated = json.loads(capsys.readouterr().out)
    assert len(curated["measure_candidate_ids"]) == 1

    assert main(["learning", "propose-measures", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["measure_candidate_ids"] == []

    candidate_id = curated["measure_candidate_ids"][0]
    with sqlite3.connect(data_dir / "learning.sqlite") as connection:
        connection.execute(
            "UPDATE learning_candidates SET created_at = ? WHERE candidate_id = ?",
            ("2000-01-01T00:00:00+00:00", candidate_id),
        )

    assert main(["learning", "prune-preview", "--json"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["candidates"][0]["candidate_id"] == candidate_id

    assert main(["learning", "prune", "--candidate-id", candidate_id]) == 2
    assert "Refusing to prune" in capsys.readouterr().err
    assert (
        main(["learning", "prune", "--candidate-id", candidate_id, "--yes", "--json"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["deleted_candidate_ids"] == [
        candidate_id
    ]


def test_cli_configure_openstudio_persists_confirmed_executable(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    executable = tmp_path / "openstudio"
    executable.write_text("#!/bin/sh\necho 3.0.0-rc1+baflkdhsia\n", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    data_dir = tmp_path / "runtime-data"
    monkeypatch.setenv("OPENSTUDIO_SI_DATA_DIR", str(data_dir))

    assert main(["configure-openstudio", "--path", str(executable)]) == 0

    config = json.loads((data_dir / "runtime.json").read_text(encoding="utf-8"))
    assert config["openstudio_path"] == str(executable.resolve())
    assert "Saved OpenStudio executable" in capsys.readouterr().out


def test_cli_configure_openstudio_resolves_relative_path_before_probing(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    executable = tmp_path / "openstudio"
    executable.write_text("#!/bin/sh\necho 3.10.0\n", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    data_dir = tmp_path / "runtime-data"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENSTUDIO_SI_DATA_DIR", str(data_dir))

    assert main(["configure-openstudio", "--path", "openstudio"]) == 0

    config = json.loads((data_dir / "runtime.json").read_text(encoding="utf-8"))
    assert config["openstudio_path"] == str(executable.resolve())
    assert str(executable.resolve()) in capsys.readouterr().out


def test_cli_configure_openstudio_rejects_unrecognized_executable(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    executable = tmp_path / "not-openstudio"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    data_dir = tmp_path / "runtime-data"
    monkeypatch.setenv("OPENSTUDIO_SI_DATA_DIR", str(data_dir))

    assert main(["configure-openstudio", "--path", str(executable)]) == 2

    assert not (data_dir / "runtime.json").exists()
    assert "recognized OpenStudio version" in capsys.readouterr().err


def test_cli_configure_openstudio_reports_configuration_write_failure(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    executable = tmp_path / "openstudio"
    executable.write_text("#!/bin/sh\necho 3.10.0\n", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(
        cli,
        "set_openstudio_path",
        lambda _: (_ for _ in ()).throw(OSError("read-only filesystem")),
    )

    assert main(["configure-openstudio", "--path", str(executable)]) == 2

    assert "Could not save" in capsys.readouterr().err


def test_cli_install_codex_creates_managed_agents_guidance(
    tmp_path: Path, capsys
) -> None:
    assert (
        main(
            [
                "install",
                "codex",
                "--target-dir",
                str(tmp_path),
                "--workspace-root",
                str(Path(".").resolve()),
            ]
        )
        == 0
    )

    assert "Wrote" in capsys.readouterr().out
    instructions = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "openstudio-modeling-orchestrator" in instructions
    assert "## SDK Script Gate" in instructions


def test_cli_doctor_text_output_reports_checks(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    data_dir = tmp_path / "runtime-data"
    (data_dir / "workspace").mkdir(parents=True)
    monkeypatch.setenv("OPENSTUDIO_SI_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPENSTUDIO_PATH", "/fake/openstudio")
    monkeypatch.setattr(
        cli,
        "resolve_openstudio_executable_with_source",
        lambda: ("/fake/openstudio", "OPENSTUDIO_PATH"),
    )

    def fake_command_available(command: str) -> dict:
        return {"command": command, "available": True, "path": command}

    def fake_run_probe(command: list[str], *, timeout_seconds: int = 10) -> dict:
        stdout = "3.10.0+test" if command[0] == "/fake/openstudio" else "help"
        return {"ok": True, "returncode": 0, "stdout": stdout, "stderr": ""}

    monkeypatch.setattr(cli, "_command_available", fake_command_available)
    monkeypatch.setattr(cli, "_run_probe", fake_run_probe)

    assert main(["doctor"]) == 0
    output = capsys.readouterr().out
    assert "MCP runtime status: ready" in output
    assert "Core plugin readiness: ready" in output
    assert "OpenStudio execution status: ready" in output
    assert "Checks:" in output
    assert "- runtime assets: ok" in output
    assert "- runtime storage: ok" in output
    assert "- python openstudio sdk: ok" in output
    assert "- sdk docs lookup: ok" in output


def test_cli_doctor_falls_back_from_invalid_sdk_docs_override(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    data_dir = tmp_path / "runtime-data"
    (data_dir / "workspace").mkdir(parents=True)
    monkeypatch.setenv("OPENSTUDIO_SI_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPENSTUDIO_SDK_DOCS_DIR", str(tmp_path / "missing-sdk-docs"))
    monkeypatch.setenv("OPENSTUDIO_PATH", "/fake/openstudio")
    monkeypatch.setattr(
        cli,
        "resolve_openstudio_executable_with_source",
        lambda: ("/fake/openstudio", "OPENSTUDIO_PATH"),
    )

    def fake_command_available(command: str) -> dict:
        return {"command": command, "available": True, "path": command}

    def fake_run_probe(command: list[str], *, timeout_seconds: int = 10) -> dict:
        stdout = "3.10.0+test" if "--version" in command else "help"
        return {"ok": True, "returncode": 0, "stdout": stdout, "stderr": ""}

    monkeypatch.setattr(cli, "_command_available", fake_command_available)
    monkeypatch.setattr(cli, "_run_probe", fake_run_probe)

    assert main(["doctor", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mcp_ready"] is True
    assert payload["sdk_docs"]["ok"] is True
    assert payload["sdk_docs"]["source"] == "bundled_fallback"
    assert payload["sdk_docs"]["path"].endswith("openstudio_si_mcp/sdk_docs/docs")
    assert payload["sdk_docs"]["override_path"] == str(tmp_path / "missing-sdk-docs")
    assert (
        "using the bundled SDK documentation" in payload["sdk_docs"]["override_warning"]
    )
    assert not any(
        item["code"] == "sdk_docs_unavailable" for item in payload["diagnostics"]
    )


def test_cli_doctor_text_reports_missing_openstudio_sdk(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    data_dir = tmp_path / "runtime-data"
    (data_dir / "workspace").mkdir(parents=True)
    monkeypatch.setenv("OPENSTUDIO_SI_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPENSTUDIO_PATH", "/fake/openstudio")
    monkeypatch.setattr(
        cli,
        "resolve_openstudio_executable_with_source",
        lambda: ("/fake/openstudio", "OPENSTUDIO_PATH"),
    )

    def fake_command_available(command: str) -> dict:
        return {"command": command, "available": True, "path": command}

    def fake_run_probe(command: list[str], *, timeout_seconds: int = 10) -> dict:
        stdout = "3.10.0+test" if "--version" in command else "help"
        return {"ok": True, "returncode": 0, "stdout": stdout, "stderr": ""}

    monkeypatch.setattr(cli, "_command_available", fake_command_available)
    monkeypatch.setattr(cli, "_run_probe", fake_run_probe)
    monkeypatch.setattr(
        cli,
        "_python_openstudio_probe",
        lambda: {"ok": False, "error": "No module named openstudio"},
    )

    assert main(["doctor"]) == 1
    output = capsys.readouterr().out
    assert "MCP runtime status: ready" in output
    assert "Core plugin readiness: not ready" in output
    assert "OpenStudio execution status: not ready" in output
    assert "- python openstudio sdk: failed" in output
    assert "native OpenStudio application" in output
    assert "[openstudio_python_sdk_unavailable]" in output
    assert "Technical detail: No module named openstudio" in output


def test_cli_doctor_reports_plugin_runtime_contract_mismatch(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    data_dir = tmp_path / "runtime-data"
    (data_dir / "workspace").mkdir(parents=True)
    monkeypatch.setenv("OPENSTUDIO_SI_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPENSTUDIO_PATH", "/fake/openstudio")
    monkeypatch.setattr(
        cli,
        "resolve_openstudio_executable_with_source",
        lambda: ("/fake/openstudio", "OPENSTUDIO_PATH"),
    )

    def fake_command_available(command: str) -> dict:
        return {"command": command, "available": True, "path": command}

    def fake_run_probe(command: list[str], *, timeout_seconds: int = 10) -> dict:
        stdout = "3.10.0+test" if "--version" in command else "help"
        return {"ok": True, "returncode": 0, "stdout": stdout, "stderr": ""}

    monkeypatch.setattr(cli, "_command_available", fake_command_available)
    monkeypatch.setattr(cli, "_run_probe", fake_run_probe)

    assert (
        main(
            [
                "doctor",
                "--json",
                "--plugin-version",
                "9.9.9",
                "--plugin-contract-version",
                "3",
            ]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["mcp_ready"] is True
    assert payload["core_ready"] is False
    assert payload["plugin_ready"] is False
    assert payload["plugin_compatibility"]["ok"] is False
    assert payload["plugin_compatibility"]["status"] == "incompatible"
    assert any(
        item["code"] == "plugin_runtime_incompatible" for item in payload["diagnostics"]
    )
    assert (
        next(
            item
            for item in payload["diagnostics"]
            if item["code"] == "plugin_runtime_incompatible"
        )["severity"]
        == "error"
    )


def test_cli_doctor_blocks_core_readiness_when_openstudio_is_missing(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    data_dir = tmp_path / "runtime-data"
    (data_dir / "workspace").mkdir(parents=True)
    monkeypatch.setenv("OPENSTUDIO_SI_DATA_DIR", str(data_dir))
    monkeypatch.delenv("OPENSTUDIO_PATH", raising=False)
    monkeypatch.setattr(
        cli, "resolve_openstudio_executable_with_source", lambda: (None, None)
    )

    def fake_command_available(command: str) -> dict:
        if command == "openstudio":
            return {"command": command, "available": False, "path": None}
        return {"command": command, "available": True, "path": command}

    monkeypatch.setattr(cli, "_command_available", fake_command_available)
    monkeypatch.setattr(
        cli,
        "_run_probe",
        lambda command, *, timeout_seconds=10: {
            "ok": True,
            "returncode": 0,
            "stdout": "3.10.0+test" if "--version" in command else "help",
            "stderr": "",
        },
    )
    monkeypatch.setattr(cli, "_python_openstudio_probe", lambda: {"ok": True})

    assert main(["doctor", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["mcp_ready"] is True
    assert payload["simulation_ready"] is False
    assert payload["core_ready"] is False
    assert payload["plugin_ready"] is True
    assert payload["ready"] is False
    diagnostic = next(
        item
        for item in payload["diagnostics"]
        if item["code"] == "openstudio_command_unavailable"
    )
    assert diagnostic["severity"] == "error"
    assert "configure-openstudio" in diagnostic["remediation"]


def test_optional_nlr_capability_does_not_block_core_readiness(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "_docker_status",
        lambda: {"installed": False, "running": False, "command": {}},
    )
    monkeypatch.setattr(cli, "_nlr_mcp_status", lambda: {"configured": False})

    capability = cli._optional_capabilities()["nlr_openstudio"]

    assert capability["blocking"] is False
    assert capability["status"] == "unavailable"
    assert "Docker is not installed" in capability["message"]


def test_configured_nlr_reports_missing_docker_without_claiming_it_is_stopped(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "_docker_status",
        lambda: {"installed": False, "running": False, "command": {}},
    )
    monkeypatch.setattr(cli, "_nlr_mcp_status", lambda: {"configured": True})

    capability = cli._optional_capabilities()["nlr_openstudio"]

    assert capability["status"] == "unavailable"
    assert capability["message"] == (
        "NLR OpenStudio-MCP is configured, but Docker is not installed."
    )


def test_nlr_status_ignores_unrelated_mentions(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path))
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(
        '# nlr_openstudio is optional\nlabel = "nlr_openstudio"\n',
        encoding="utf-8",
    )
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"other": {"description": "nlr_openstudio"}}}),
        encoding="utf-8",
    )

    status = cli._nlr_mcp_status()

    assert status["configured"] is False


def test_nlr_status_skips_project_scan_when_working_directory_is_unavailable(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(
        cli.Path,
        "cwd",
        staticmethod(lambda: (_ for _ in ()).throw(OSError("directory removed"))),
    )

    status = cli._nlr_mcp_status()

    assert status["configured"] is False


def test_cli_export_claude_marketplace(tmp_path: Path) -> None:
    assert (
        main(
            [
                "export",
                "claude",
                "--output-dir",
                str(tmp_path),
                "--workspace-root",
                str(Path(".").resolve()),
                "--runtime-mode",
                "marketplace",
            ]
        )
        == 0
    )

    plugin_dir = tmp_path / "openstudio-si"
    mcp_json = json.loads((plugin_dir / ".mcp.json").read_text(encoding="utf-8"))
    assert mcp_json["mcpServers"]["openstudio_si"]["command"] == "openstudio-si-mcp"
    assert (plugin_dir / "skills" / "setup-openstudio-si" / "SKILL.md").exists()
    assert (
        plugin_dir / "skills" / "setup-openstudio-si" / "scripts" / "doctor_runtime.py"
    ).exists()
    assert (
        main(["validate-export", str(plugin_dir), "--runtime-mode", "marketplace"]) == 0
    )


def test_cli_export_paired_marketplace_includes_provenance(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-state"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-state"))
    assert (
        main(
            [
                "export",
                "marketplace",
                "--output-dir",
                str(tmp_path),
                "--workspace-root",
                str(Path(".").resolve()),
                "--runtime-mode",
                "marketplace",
            ]
        )
        == 0
    )

    claude_plugin = tmp_path / "openstudio-si"
    codex_plugin = tmp_path / "plugins" / "openstudio-si"
    assert (claude_plugin / ".claude-plugin" / "plugin.json").exists()
    assert (codex_plugin / ".codex-plugin" / "plugin.json").exists()
    assert (tmp_path / ".claude-plugin" / "marketplace.json").exists()
    assert (tmp_path / ".agents" / "plugins" / "marketplace.json").exists()
    assert not (tmp_path / "INSTALL.md").exists()

    for plugin in (claude_plugin, codex_plugin):
        config = json.loads((plugin / ".mcp.json").read_text(encoding="utf-8"))
        assert set(config["mcpServers"]) == {"openstudio_si"}
        assert config["mcpServers"]["openstudio_si"]["command"] == "openstudio-si-mcp"
        helper = plugin / "skills/setup-openstudio-si/scripts/doctor_runtime.py"
        result = subprocess.run(
            [sys.executable, "-I", "-S", str(helper), "--core-metadata"],
            env={"PATH": ""}, capture_output=True, text=True, check=True,
        )
        assert json.loads(result.stdout) == {
            "ok": True, "plugin_version": cli._version(),
            "plugin_contract_version": cli.PLUGIN_CONTRACT_VERSION,
        }

    for host, plugin in (("claude", claude_plugin), ("codex", codex_plugin)):
        specialist = plugin.parent / HVAC_PLUGIN_ID
        assert {
            path.parent.name for path in (specialist / "skills").glob("*/SKILL.md")
        } == HVAC_SKILLS
        assert not (plugin / "skills/hvac-modeler").exists()
        for skill in HVAC_SKILLS - {"hvac-modeler"}:
            legacy = plugin / "skills" / skill
            if host == "claude" and supports_hvac_core(
                cli._version(), HVAC_FORWARDING_RELEASE, HVAC_FORWARDING_BEFORE
            ):
                assert (
                    f"openstudio-si-hvac:{skill}" in (legacy / "SKILL.md").read_text()
                )
            else:
                assert not legacy.exists()
            assert not (legacy / "scripts").exists()
        assert not (plugin / "sdk/common/vav_plan.py").exists()
        assert not (specialist / ".mcp.json").exists()
        manifest = json.loads((specialist / f".{host}-plugin/plugin.json").read_text())
        assert manifest["name"] == HVAC_PLUGIN_ID
        assert manifest["version"] == HVAC_VERSION
        assert (
            json.loads((specialist / "sdk/bundle.json").read_text())["plugin_version"]
            == HVAC_VERSION
        )
        for skill in (specialist / "skills").glob("*/SKILL.md"):
            text = skill.read_text()
            assert f"version: {HVAC_VERSION}\n" in text.split("---", 2)[1]
            assert "doctor_runtime.py --core-metadata" in text
            assert (
                "live OpenStudio SI MCP connection is required\nfor simulation and results"
                in text
            )
        if host == "claude":
            assert manifest["dependencies"] == [
                {"name": "openstudio-si", "version": HVAC_CORE_RANGE}
            ]
        else:
            assert "dependencies" not in manifest
        assert "packaged VAV" in manifest["description"]
        market_path = {
            "claude": ".claude-plugin/marketplace.json",
            "codex": ".agents/plugins/marketplace.json",
        }[host]
        marketplace = json.loads((tmp_path / market_path).read_text())
        assert {entry["name"] for entry in marketplace["plugins"]} == {
            "openstudio-si",
            HVAC_PLUGIN_ID,
        }
        core_bundle = json.loads((plugin / "sdk/bundle.json").read_text())["files"]
        hvac_bundle = json.loads((specialist / "sdk/bundle.json").read_text())["files"]
        common = core_bundle.keys() & hvac_bundle.keys()
        assert common
        assert all(core_bundle[path] == hvac_bundle[path] for path in common)

    fingerprint = hvac_release_fingerprint(tmp_path)
    assert fingerprint == HVAC_RELEASE["content_sha256"], (
        "HVAC export content changed. Bump its version and record the new fingerprint "
        f"in harness/hvac_release.json. Observed fingerprint: {fingerprint}"
    )
    base = os.environ.get("OPENSTUDIO_SI_RELEASE_BASE_SHA")
    if base:
        previous_files = subprocess.check_output(
            ["git", "ls-tree", "--name-only", base, "--", "harness/hvac_release.json"],
            text=True,
        )
        if previous_files.strip():
            previous = json.loads(
                subprocess.check_output(
                    ["git", "show", f"{base}:harness/hvac_release.json"], text=True
                )
            )
            if fingerprint != previous["content_sha256"]:
                assert semver_precedence(HVAC_VERSION) > semver_precedence(
                    previous["version"]
                ), (
                    "Changed HVAC content requires a version bump, even if its fingerprint was updated."
                )

    specialist = codex_plugin.parent / HVAC_PLUGIN_ID
    checks = {
        "version": cli._version(),
        "plugin_compatibility": {"plugin_version": cli._version()},
        "core_ready": True,
        "openstudio": {"version_probe": {"openstudio_version": "3.11.0+241b8abb4d"}},
    }
    assert plugin_readiness(checks, None)[HVAC_PLUGIN_ID]["status"] == "not_detected"
    assert plugin_readiness(checks, specialist)[HVAC_PLUGIN_ID]["ready"]
    cached = tmp_path / f"codex-state/plugins/cache/test-market/openstudio-si-hvac/{HVAC_VERSION}"
    cached.parent.mkdir(parents=True)
    cached.symlink_to(specialist, target_is_directory=True)
    assert (
        plugin_readiness(checks, None)[HVAC_PLUGIN_ID]["status"] == "cached_unverified"
    )
    for installed, runtime in ((None, "0.4.3"), ("0.4.2", "0.4.3"), ("0.4.3", "0.4.2")):
        mixed = {
            **checks,
            "version": runtime,
            "plugin_compatibility": {"plugin_version": installed},
        }
        assert (
            plugin_readiness(mixed, specialist)[HVAC_PLUGIN_ID]["status"]
            == "incompatible_core"
        )
    checks["openstudio"]["version_probe"]["openstudio_version"] = "3.10.0"
    assert (
        plugin_readiness(checks, specialist)[HVAC_PLUGIN_ID]["status"]
        == "incompatible_openstudio"
    )
    checks["core_ready"] = False
    assert (
        plugin_readiness(checks, specialist)[HVAC_PLUGIN_ID]["status"]
        == "core_not_ready"
    )
    descriptor = specialist / PLUGIN_DESCRIPTOR
    metadata = json.loads(descriptor.read_text())
    metadata["core_contract_version"] = "999.0"
    descriptor.write_text(json.dumps(metadata))
    assert (
        plugin_readiness(checks, specialist)[HVAC_PLUGIN_ID]["status"]
        == "incompatible_core"
    )
    (specialist / "sdk/common/vav_plan.py").unlink()
    assert (
        plugin_readiness(checks, specialist)[HVAC_PLUGIN_ID]["status"]
        == "package_incomplete"
    )

    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "INSTALL.claude.md" in readme
    assert "INSTALL.codex.md" in readme
    assert (
        (tmp_path / "INSTALL.claude.md")
        .read_text(encoding="utf-8")
        .startswith("# Install OpenStudio SI In Claude Code")
    )
    assert (
        (tmp_path / "INSTALL.codex.md")
        .read_text(encoding="utf-8")
        .startswith("# Install OpenStudio SI In Codex")
    )

    provenance = json.loads((tmp_path / ".generated.json").read_text(encoding="utf-8"))
    assert provenance["schema_version"] == 1
    assert provenance["generator"] == "openstudio-si export marketplace"
    assert provenance["package"]["name"] == "openstudio-si"
    assert provenance["plugin"] == {
        "name": "openstudio-si",
        "mcp_interface_contract_version": "6.0",
        "runtime_mode": "marketplace",
    }
    assert set(provenance["source"]) == {"revision", "dirty"}


def test_cli_validate_export(tmp_path: Path) -> None:
    assert (
        main(
            [
                "export",
                "codex",
                "--output-dir",
                str(tmp_path),
                "--workspace-root",
                str(Path(".").resolve()),
                "--runtime-mode",
                "marketplace",
            ]
        )
        == 0
    )

    plugin_dir = tmp_path / "plugins" / "openstudio-si"
    assert main(["validate-export", str(plugin_dir)]) == 0


def test_cli_validate_export_rejects_missing_marketplace_setup(tmp_path: Path) -> None:
    assert (
        main(
            [
                "export",
                "codex",
                "--output-dir",
                str(tmp_path),
                "--workspace-root",
                str(Path(".").resolve()),
                "--runtime-mode",
                "marketplace",
            ]
        )
        == 0
    )

    plugin_dir = tmp_path / "plugins" / "openstudio-si"
    (
        plugin_dir / "skills" / "setup-openstudio-si" / "scripts" / "doctor_runtime.py"
    ).unlink()
    assert (
        main(["validate-export", str(plugin_dir), "--runtime-mode", "marketplace"]) == 1
    )


def test_cli_validate_export_rejects_missing_compatibility_metadata(
    tmp_path: Path,
) -> None:
    assert (
        main(
            [
                "export",
                "claude",
                "--output-dir",
                str(tmp_path),
                "--workspace-root",
                str(Path(".").resolve()),
                "--runtime-mode",
                "marketplace",
            ]
        )
        == 0
    )

    plugin_dir = tmp_path / "openstudio-si"
    mcp_path = plugin_dir / ".mcp.json"
    mcp_config = json.loads(mcp_path.read_text(encoding="utf-8"))
    del mcp_config["mcpServers"]["openstudio_si"]["env"]
    mcp_path.write_text(json.dumps(mcp_config), encoding="utf-8")

    assert (
        main(["validate-export", str(plugin_dir), "--runtime-mode", "marketplace"]) == 1
    )


def test_cli_validate_export_allows_historical_metadata_unless_strict(
    tmp_path: Path,
) -> None:
    assert (
        main(
            [
                "export",
                "codex",
                "--output-dir",
                str(tmp_path),
                "--workspace-root",
                str(Path(".").resolve()),
                "--runtime-mode",
                "marketplace",
            ]
        )
        == 0
    )

    plugin_dir = tmp_path / "plugins" / "openstudio-si"
    mcp_path = plugin_dir / ".mcp.json"
    mcp_config = json.loads(mcp_path.read_text(encoding="utf-8"))
    mcp_config["mcpServers"]["openstudio_si"]["env"]["OPENSTUDIO_SI_PLUGIN_VERSION"] = (
        "0.0.1"
    )
    mcp_path.write_text(json.dumps(mcp_config), encoding="utf-8")

    assert (
        main(["validate-export", str(plugin_dir), "--runtime-mode", "marketplace"]) == 0
    )
    assert (
        main(
            [
                "validate-export",
                str(plugin_dir),
                "--runtime-mode",
                "marketplace",
                "--strict-runtime-version",
            ]
        )
        == 1
    )
