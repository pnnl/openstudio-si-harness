import json
from pathlib import Path

import pytest
import yaml
from openstudio_si_harness import __version__
from openstudio_si_harness.ci import configure_ci
from openstudio_si_harness.generator import init_project


def test_configure_and_upgrade_thin_pinned_workflows(tmp_path):
    project = tmp_path / "project"
    init_project(project, identity="ci-demo", license_identifier="MIT")
    pending = yaml.load(
        (project / ".github/workflows/release-plugin.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert pending["on"] == "workflow_dispatch" and pending["permissions"] == {}
    first = "1" * 40
    preview = configure_ci(
        project, workflow_ref=first, tooling_version="0.4.4", dry_run=True
    )
    assert not preview["configured"]
    assert (
        json.loads((project / ".openstudio-si/ci.json").read_text())["configured"]
        is False
    )
    configure_ci(project, workflow_ref=first, tooling_version="0.4.4")
    release = yaml.load(
        (project / ".github/workflows/release-plugin.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert release["permissions"]["contents"] == "read"
    assert release["jobs"]["build"]["uses"].endswith("@" + first)
    assert release["jobs"]["build"]["with"]["tooling-version"] == "0.4.4"
    assert release["jobs"]["upload"]["permissions"]["contents"] == "write"
    assert "steps" not in release["jobs"]["upload"]
    assert release["jobs"]["upload"]["needs"] == "build"
    assert not (project / ".openstudio-si/tooling.lock").exists()
    configure_ci(project, workflow_ref="2" * 40)
    state = json.loads((project / ".openstudio-si/ci.json").read_text())
    assert state["tooling_version"] == __version__
    assert "@" + "2" * 40 in (project / ".github/workflows/validate.yml").read_text()
    edited = project / ".github/workflows/validate.yml"
    edited.write_text(edited.read_text() + "\n# author change\n")
    with pytest.raises(ValueError, match="was edited"):
        configure_ci(project, workflow_ref="3" * 40)
    assert "# author change" in edited.read_text()


@pytest.mark.parametrize(
    "version",
    [
        "latest",
        ">=0.4.4",
        "0.4.4 --extra-index-url x",
        "https://example.com/a.whl",
        "0.4.4\n",
        "04.4.4",
        "0.4.4rc1",
    ],
)
def test_invalid_install_versions_leave_project_unchanged(tmp_path, version):
    project = tmp_path / "project"
    init_project(project, identity="ci-demo", license_identifier="MIT")
    before = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="exact stable release"):
        configure_ci(project, workflow_ref="1" * 40, tooling_version=version)
    assert before == {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}


def test_prepare_hosts_uses_native_package_without_lifecycle_script(
    tmp_path, monkeypatch
):
    import json
    import platform
    import sys

    from openstudio_si_harness.project_tools import prepare_hosts

    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(sys, "platform", "linux")
    package = tmp_path / "npm/node_modules/@anthropic-ai/claude-code-linux-x64"
    package.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"version": "2.1.309"}))
    binary = package / "claude"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    output = tmp_path / "bin"
    result = prepare_hosts(tmp_path / "npm", output)
    assert result["claude"] == str(binary)
    assert str(binary) in (output / "claude").read_text()
    assert (output / "claude").stat().st_mode & 0o111
    assert not (package / "install.cjs").exists()


def test_installed_configure_ci_command(tmp_path):
    import subprocess
    import sys

    project = tmp_path / "project"
    init_project(project, identity="ci-demo", license_identifier="MIT")
    result = subprocess.run(
        [
            str(Path(sys.executable).with_name("openstudio-si")),
            "plugin",
            "configure-ci",
            str(project),
            "--workflow-ref",
            "a" * 40,
            "--tooling-version",
            "0.4.4",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["tooling_version"] == "0.4.4"
    assert (
        "tooling-version: '0.4.4'"
        in (project / ".github/workflows/validate.yml").read_text()
    )


def test_optional_tools_explain_missing_extra(tmp_path):
    import subprocess
    import sys

    script = """
import builtins
from openstudio_si.cli import main
original = builtins.__import__
def without_tools(name, *args, **kwargs):
    if name == "openstudio_si_harness.cli":
        raise ModuleNotFoundError("No module named 'jsonschema'", name="jsonschema")
    return original(name, *args, **kwargs)
builtins.__import__ = without_tools
raise SystemExit(main(["plugin", "--help"]))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert 'pip install "openstudio-si[plugin-dev]"' in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("version", ["0.0.1", "0.3.99", "0.4.0", "0.4.2"])
def test_tooling_releases_before_plugin_commands_are_rejected(tmp_path, version):
    project = tmp_path / "project"
    init_project(project, identity="ci-demo", license_identifier="MIT")
    before = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match=r"openstudio-si>=0\.4\.3"):
        configure_ci(project, workflow_ref="1" * 40, tooling_version=version)
    assert before == {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}


def test_generation_never_imports_sdk_or_needs_native_cli(tmp_path):
    import os
    import subprocess
    import sys

    script = """
import sys
from openstudio_si.cli import main
assert main(["plugin", "init", sys.argv[1], "--id", "no-sdk-import", "--license", "MIT"]) == 0
assert "openstudio" not in sys.modules
assert "openstudio_si_mcp.server" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(tmp_path / "project")],
        env={**os.environ, "PATH": ""},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
