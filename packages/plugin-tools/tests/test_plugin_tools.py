import copy
import json
from pathlib import Path

import pytest
import yaml

from openstudio_si_harness.compatibility import satisfies, semver
from openstudio_si_harness.export import export_plugin
from openstudio_si_harness.manifest import load_plugin


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "plugin source"
    root.mkdir()
    (root / "evals/cases").mkdir(parents=True)
    skill = root / "skills/lookup"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: lookup\ndescription: Query standards.\n---\nRead the source table.\n"
    )
    (skill / "references").mkdir()
    (skill / "references/coverage.md").write_text("Supported editions")
    data = {
        "schema_version": "0.1",
        "id": "fixture-plugin",
        "name": "Standards",
        "description": "Query requirements.",
        "version": "0.1.0",
        "owner": {
            "id": "individual",
            "organization": "Independent contributor",
            "maintainers": [{"name": "Developer"}],
        },
        "license": "BSD-2-Clause",
        "compatibility": {"harness_api": ">=0.1.0 <0.2.0"},
        "dependencies": [],
        "capabilities": [
            {
                "id": "lookup",
                "name": "Lookup",
                "description": "Query tables.",
                "requires": [],
                "effects": ["read-data"],
            }
        ],
        "evaluation": {"cases": "evals/cases"},
    }
    (root / "openstudio-si-plugin.yaml").write_text(yaml.safe_dump(data))
    return root, data


@pytest.mark.parametrize(
    "identity,server",
    [
        ("fixture-provider", "example_service"),
        ("fixture-consumer", None),
        ("fixture-local-service", "local_service"),
    ],
)
def test_independent_host_exports(source, tmp_path, identity, server):
    root, data = source
    data["id"] = identity
    if server:
        (root / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        server: {
                            "command": "test-service",
                            "args": ["--transport", "stdio"],
                        }
                    }
                }
            )
        )
    if identity != "fixture-provider":
        data["dependencies"] = [
            {
                "id": "core",
                "kind": "plugin",
                "target": "fixture-provider",
                "version_spec": ">=0.1.0 <0.2.0",
                "required": True,
                "probe": "metadata",
            }
        ]
    (root / "openstudio-si-plugin.yaml").write_text(yaml.safe_dump(data))
    output = tmp_path / "relocated output"
    report = export_plugin(root, output)
    assert report["inventory"] == {
        "skill_count": 1,
        "mcp_server_count": int(bool(server)),
    }
    for host in ("claude", "codex"):
        package = output / host / identity
        manifest = json.loads((package / f".{host}-plugin/plugin.json").read_text())
        assert manifest["name"] == identity
        assert (
            package / "skills/lookup/references/coverage.md"
        ).read_text() == "Supported editions"
        assert (package / ".mcp.json").exists() == bool(server)
        if host == "claude" and identity != "fixture-provider":
            assert manifest["dependencies"][0]["version"] == ">=0.1.0 <0.2.0"
    assert not (root / ".git").exists()
    with pytest.raises(ValueError, match="already exists"):
        export_plugin(root, output)


@pytest.mark.parametrize(
    "change", ["unknown", "probe", "api", "counts", "self", "duplicate"]
)
def test_bad_manifest_fails_before_export(source, tmp_path, change):
    root, data = source
    dep = {
        "id": "core",
        "kind": "plugin",
        "target": "openstudio-si",
        "required": True,
        "probe": "metadata",
    }
    data["dependencies"] = [copy.deepcopy(dep)]
    if change == "unknown":
        data["capabilities"][0]["requires"] = ["missing"]
    elif change == "probe":
        data["dependencies"][0]["probe"] = "mcp-tools"
    elif change == "api":
        data["compatibility"]["harness_api"] = ">=9.0.0"
    elif change == "counts":
        data["inventory"] = {"skill_count": 2, "mcp_server_count": 0}
    elif change == "self":
        data["dependencies"][0]["target"] = data["id"]
    else:
        data["dependencies"].append(dep)
    (root / "openstudio-si-plugin.yaml").write_text(yaml.safe_dump(data))
    output = tmp_path / "output"
    with pytest.raises(ValueError):
        export_plugin(root, output)
    assert not output.exists()


def test_escaping_resources_and_duplicate_keys_are_rejected(source, tmp_path):
    root, _ = source
    outside = tmp_path / "private"
    outside.write_text("private")
    link = root / "skills/lookup/references/outside"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        load_plugin(root)
    link.unlink()
    manifest = root / "openstudio-si-plugin.yaml"
    manifest.write_text(manifest.read_text() + "id: other\n")
    with pytest.raises(ValueError, match="duplicate YAML key"):
        load_plugin(root)


def test_version_ranges():
    assert satisfies("0.4.3+build.2", ">=0.4.3 <0.5.0")
    assert semver("0.1.0-rc.2") < semver("0.1.0-rc.10") < semver("0.1.0")
    with pytest.raises(ValueError):
        semver("0.1.0-01")


def test_generated_minimal_example_validates_without_runtime(tmp_path):
    from openstudio_si_harness.generator import init_project

    root = tmp_path / "project"
    init_project(root, identity="example-plugin", license_identifier="BSD-3-Clause")
    plugin = load_plugin(root / "plugin")
    assert plugin["manifest"]["id"] == "example-plugin"
    assert plugin["manifest"]["dependencies"] == []
    assert plugin["inventory"] == {"skill_count": 1, "mcp_server_count": 0}


@pytest.mark.parametrize(
    "config",
    [
        {"mcpServers": {"service": {"url": "http://example.test"}}},
        {"mcpServers": {"service": {"command": "besd-mcp", "args": "stdio"}}},
    ],
)
def test_invalid_connections_are_rejected(source, config):
    root, _ = source
    (root / ".mcp.json").write_text(json.dumps(config))
    with pytest.raises(ValueError):
        load_plugin(root)


def test_source_output_overlap_is_rejected(source):
    root, _ = source
    with pytest.raises(ValueError, match="outside"):
        export_plugin(root, root / "dist")
    assert not (root / "dist").exists()


@pytest.mark.parametrize("identity", ["openstudio-si", "openstudio-si-hvac"])
def test_reserved_ids_cannot_replace_released_plugins(source, tmp_path, identity):
    root, data = source
    data["id"] = identity
    (root / "openstudio-si-plugin.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="Reserved plugin ID"):
        export_plugin(root, tmp_path / "out")


@pytest.mark.parametrize("expression", [">=0.4.3,<0.5.0", "!=0.4.4", "==0.4.3"])
def test_reject_non_npm_ranges(expression):
    with pytest.raises(ValueError):
        satisfies("0.4.3", expression)


def test_npm_prerelease_exclusion_and_explicit_opt_in():
    assert not satisfies("0.5.0-rc.1", ">=0.4.3 <0.5.0")
    assert satisfies("0.5.0-rc.1", ">=0.5.0-rc.0 <0.5.0")
    assert satisfies("0.4.3+build.9", "=0.4.3")


def test_filtered_resources_external_provider_and_roundtrip(source, tmp_path):
    from openstudio_si_harness.export import DESCRIPTOR, validate_export

    root, data = source
    for name in ["bin", "sdk", "hooks", "commands", "custom"]:
        (root / name).mkdir()
        (root / name / "resource").write_text(name)
    data["package"] = {"include": ["custom"]}
    data["dependencies"] = [
        dict(
            id="core",
            kind="plugin",
            target="openstudio-si",
            required=True,
            probe="metadata",
        ),
        dict(
            id="core-mcp",
            kind="mcp-server",
            target="openstudio_si",
            provider="core",
            required=True,
            probe="mcp-tools",
        ),
    ]
    data["capabilities"][0]["requires"] = ["core", "core-mcp"]
    (root / "openstudio-si-plugin.yaml").write_text(yaml.safe_dump(data))
    (root / "evals/cases/example.json").write_text("{}")
    for name in [".env", ".env.local", ".DS_Store"]:
        (root / "skills/lookup" / name).write_text("excluded")
    (root / "skills/lookup/__pycache__").mkdir()
    (root / "skills/lookup/__pycache__/x.pyc").write_bytes(b"cache")
    (root / ".venv/bin").mkdir(parents=True)
    (root / ".venv/bin/python").symlink_to("/missing/python")
    export_plugin(root, tmp_path / "out")
    for host in ["claude", "codex"]:
        package = tmp_path / "out" / host / data["id"]
        assert not (package / "openstudio-si-plugin.json").exists()
        assert not any(
            p.name in {".env", ".env.local", ".DS_Store", "x.pyc"}
            for p in package.rglob("*")
        )
        for name in ["bin", "sdk", "hooks", "commands", "custom"]:
            assert (package / name / "resource").read_text() == name
        assert (package / "evals/cases/example.json").exists()
        descriptor = json.loads((package / DESCRIPTOR).read_text())
        assert descriptor["tool_version"] and descriptor["files"]
        assert validate_export(package)["manifest"]["id"] == data["id"]
        (package / "sdk/resource").write_text("changed")
        with pytest.raises(ValueError, match="hashes"):
            validate_export(package)


def test_unknown_resource_is_explicit(source):
    root, _ = source
    (root / "custom-runtime").mkdir()
    with pytest.raises(ValueError, match="package.include"):
        load_plugin(root)


def test_api_version_independent_of_tool_release(source, monkeypatch):
    import openstudio_si_harness

    monkeypatch.setattr(openstudio_si_harness, "__version__", "9.0.0")
    load_plugin(source[0])


@pytest.mark.parametrize(
    "entry",
    [
        {"command": "/Users/developer/bin/server"},
        {"command": "server", "args": ["${CLAUDE_PLUGIN_ROOT}/server.py"]},
        {"command": "server", "env": {"API_KEY": "literal-secret"}},
        {"command": "server", "args": ["--api-key=secret"]},
    ],
)
def test_nonportable_mcp_configuration_rejected(source, entry):
    (source[0] / ".mcp.json").write_text(json.dumps({"mcpServers": {"test": entry}}))
    with pytest.raises(ValueError):
        load_plugin(source[0])


def test_desktop_launcher_without_path_and_secret_forwarding(source, tmp_path):
    import subprocess
    from openstudio_si_harness.mcp import host_servers

    executable = tmp_path / "installed server"
    executable.write_text('#!/bin/sh\nprintf "%s:%s" "$TEST_KEY" "$1"\n')
    executable.chmod(0o755)
    entry = dict(
        command="unavailable-server", args=["stdio"], env={"TEST_KEY": "${TEST_SECRET}"}
    )
    (source[0] / ".mcp.json").write_text(json.dumps({"mcpServers": {"test": entry}}))
    load_plugin(source[0])
    server = host_servers({"test": entry})["test"]
    env = {
        "PATH": "",
        "HOME": str(tmp_path),
        "TEST_SECRET": "synthetic-value",
        "OPENSTUDIO_SI_TEST_COMMAND": str(executable),
    }
    result = subprocess.run(
        [server["command"], *server["args"]], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "synthetic-value:stdio"
    local_bin = tmp_path / ".local/bin"
    local_bin.mkdir(parents=True)
    installed = local_bin / "unavailable-server"
    installed.write_text(executable.read_text())
    installed.chmod(0o755)
    del env["OPENSTUDIO_SI_TEST_COMMAND"]
    result = subprocess.run(
        [server["command"], *server["args"]], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0 and result.stdout == "synthetic-value:stdio"
    del env["TEST_SECRET"]
    result = subprocess.run(
        [server["command"], *server["args"]], env=env, capture_output=True, text=True
    )
    assert result.returncode != 0 and "TEST_SECRET" in result.stderr


def test_installed_cli_outside_checkout(source, tmp_path):
    import subprocess
    import sys
    import importlib.metadata

    # The suite must import the wheel, not a source-tree sys.path override.
    import openstudio_si_harness

    assert "site-packages" in str(Path(openstudio_si_harness.__file__))
    assert openstudio_si_harness.__version__ == importlib.metadata.version(
        "openstudio-si"
    )
    assert "plugin-dev" in importlib.metadata.metadata("openstudio-si").get_all(
        "Provides-Extra", []
    )
    executable = Path(sys.executable).with_name("openstudio-si")
    result = subprocess.run(
        [str(executable), "plugin", "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0 and "plugin" in result.stdout
    command = [sys.executable, "-I", "-m", "openstudio_si_harness.cli"]
    for arguments, expected in [
        (["plugin", "validate", str(source[0])], 0),
        (["plugin", "validate", str(tmp_path / "missing")], 1),
        (["plugin", "unknown"], 2),
    ]:
        result = subprocess.run(
            command + arguments, cwd=tmp_path, capture_output=True, text=True
        )
        assert result.returncode == expected, result.stderr
    result = subprocess.run(
        command
        + ["plugin", "export", str(source[0]), "--output", str(tmp_path / "cli-out")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    exported = json.loads(result.stdout)["packages"]["codex"]
    result = subprocess.run(
        command + ["plugin", "validate", exported],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("kind", ["extra-file", "linked-parent"])
def test_selected_resource_boundaries(source, tmp_path, kind):
    root, data = source
    if kind == "extra-file":
        (root / "custom-config.json").write_text("{}")
        expected = "package.include"
    else:
        (root / "evals/cases").rmdir()
        (root / "evals").rmdir()
        outside = tmp_path / "outside"
        (outside / "cases").mkdir(parents=True)
        (root / "external").symlink_to(outside, target_is_directory=True)
        data["evaluation"]["cases"] = "external/cases"
        (root / "openstudio-si-plugin.yaml").write_text(yaml.safe_dump(data))
        expected = "symlink"
    with pytest.raises(ValueError, match=expected):
        load_plugin(root)


@pytest.mark.parametrize("change", ["stable-schema", "undefined-core-interface"])
def test_unfinalized_manifest_contracts_are_rejected(source, change):
    root, data = source
    if change == "stable-schema":
        data["schema_version"] = "1.0"
    else:
        data["dependencies"] = [
            dict(
                id="interface",
                kind="core-interface",
                target="core",
                required=True,
                probe="metadata",
            )
        ]
    (root / "openstudio-si-plugin.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError):
        load_plugin(root)


@pytest.mark.parametrize(
    "version,expression,expected",
    [
        ("0.5.0-rc.1", "<0.5.0", False),
        ("0.5.0-rc.2", "<=0.5.0-rc.1", False),
        ("0.5.0", "<=0.5.0-rc.1", False),
        ("0.5.0-rc.1", "<=0.5.0-rc.1", True),
        ("0.5.0-rc.0", "<=0.5.0-rc.1", True),
    ],
)
def test_npm_single_bound_regressions(version, expression, expected):
    assert satisfies(version, expression) is expected


def test_asset_build_directory_names_are_preserved(source, tmp_path):
    root, data = source
    for name in ("build", "dist", "venv"):
        asset = root / "skills/lookup/assets" / name / "app.js"
        asset.parent.mkdir(parents=True)
        asset.write_text("asset")
        (root / name).mkdir()
        (root / name / "development-only").write_text("excluded")
    report = export_plugin(root, tmp_path / "out")
    for path in report["packages"].values():
        package = Path(path)
        for name in ("build", "dist", "venv"):
            assert (
                package / "skills/lookup/assets" / name / "app.js"
            ).read_text() == "asset"
            assert not (package / name).exists()


def test_credential_filenames_are_never_exported(source, tmp_path):
    root, _ = source
    names = (
        "id_rsa",
        "private.pem",
        ".npmrc",
        ".netrc",
        "credentials.json",
        ".envrc",
        ".secrets",
    )
    for name in names:
        (root / "skills/lookup" / name).write_text("synthetic credential fixture")
    report = export_plugin(root, tmp_path / "out")
    for path in report["packages"].values():
        assert not any(p.name in names for p in Path(path).rglob("*"))


def test_mcp_argument_flags_and_override_collisions(source):
    root, _ = source
    path = root / ".mcp.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "test": {"command": "server", "args": ["--max-tokens", "100"]}
                }
            }
        )
    )
    load_plugin(root)
    path.write_text(
        json.dumps(
            {"mcpServers": {"test": {"command": "server", "args": ["--config=~/x"]}}}
        )
    )
    with pytest.raises(ValueError, match="nonportable"):
        load_plugin(root)
    path.write_text(
        json.dumps(
            {"mcpServers": {"a-b": {"command": "server"}, "a_b": {"command": "server"}}}
        )
    )
    with pytest.raises(ValueError, match="collide"):
        load_plugin(root)


def test_removing_descriptor_cannot_downgrade_export_to_source(source, tmp_path):
    from openstudio_si_harness.export import DESCRIPTOR

    report = export_plugin(source[0], tmp_path / "out")
    for path in report["packages"].values():
        package = Path(path)
        (package / DESCRIPTOR).unlink()
        with pytest.raises(ValueError, match="missing its export descriptor"):
            load_plugin(package)


def test_mcp_export_declares_platforms_and_rejects_windows(
    source, tmp_path, monkeypatch
):
    from openstudio_si_harness.export import DESCRIPTOR
    import openstudio_si_harness.export as exporter

    root, _ = source
    (root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"test": {"command": "server"}}})
    )
    report = export_plugin(root, tmp_path / "unix")
    for path in report["packages"].values():
        descriptor = json.loads((Path(path) / DESCRIPTOR).read_text())
        assert descriptor["supported_platforms"] == ["darwin", "linux"]
    monkeypatch.setattr(exporter.sys, "platform", "win32")
    with pytest.raises(ValueError, match="macOS and Linux"):
        export_plugin(root, tmp_path / "windows")


@pytest.mark.parametrize("failure", ["hash", "version"])
def test_released_doctor_gate_rejects_untrusted_baselines(tmp_path, failure):
    import hashlib
    import subprocess
    import sys
    import zipfile

    wheel = tmp_path / "baseline.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "openstudio_si-0.4.1.dist-info/METADATA",
            "Name: openstudio-si\nVersion: 0.4.1\n",
        )
    digest = (
        hashlib.sha256(wheel.read_bytes()).hexdigest()
        if failure == "version"
        else "0" * 64
    )
    script = Path(__file__).with_name("check_released_core_doctor.py")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(script),
            "--core-wheel",
            str(wheel),
            "--sha256",
            digest,
            "--example",
            str(tmp_path),
            "--report",
            str(tmp_path / "report.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert (
        "0.4.2 wheel" if failure == "version" else "pinned SHA-256"
    ) in result.stderr
