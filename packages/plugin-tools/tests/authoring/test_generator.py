"""Exercise generation from the installed wheel and use the resulting project."""

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from jsonschema import Draft202012Validator
from openstudio_si_harness.export import export_plugin, validate_export
from openstudio_si_harness.generator import init_project as _init_project


def init_project(*args, **kwargs):
    kwargs.setdefault("license_identifier", "BSD-3-Clause")
    return _init_project(*args, **kwargs)


from openstudio_si_harness.manifest import load_plugin


def run_script(project, name, *args, env=None):
    action = {
        "check_project.py": "check-project",
        "check_hosts.py": "check-hosts",
        "package_release.py": "package-release",
    }[name]
    command = [
        sys.executable,
        "-I",
        "-m",
        "openstudio_si_harness.cli",
        "plugin",
        action,
    ]
    if action == "check-project":
        command.append(str(project))
    elif action == "check-hosts":
        command.append(str(args[0]))
    else:
        command += [
            str(project),
            "--report",
            str(args[0]),
            "--tag",
            env["RELEASE_TAG"],
            "--repository",
            env["GITHUB_REPOSITORY"],
            "--source-sha",
            env["GITHUB_SHA"],
        ]
    return subprocess.run(
        command,
        check=False,
        cwd=project.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("with_mcp", [False, True])
def test_generated_project_is_relocatable_and_exports(tmp_path, with_mcp):
    project = tmp_path / "fresh project"
    result = init_project(
        project,
        identity="lab-demo",
        name='Lab "Δ" Demo',
        owner="example-lab",
        organization="Example Lab",
        maintainer="Researcher",
        contact="researcher@example.org",
        with_mcp=with_mcp,
    )
    assert result["created"] and not (project / ".git").exists()
    assert result["files"] == sorted(
        p.relative_to(project).as_posix() for p in project.rglob("*") if p.is_file()
    )
    data = load_plugin(project / "plugin")
    assert "kind" not in result and "kind" not in data["manifest"]
    provenance = json.loads((project / ".openstudio-si-template.json").read_text())
    assert "kind" not in provenance and provenance["features"] == {"mcp": with_mcp}
    assert data["manifest"]["name"] == 'Lab "Δ" Demo'
    assert (
        data["manifest"]["owner"]["maintainers"][0]["contact"]
        == "researcher@example.org"
    )
    assert data["inventory"] == {"skill_count": 1, "mcp_server_count": int(with_mcp)}
    pyproject = tomllib.loads((project / "pyproject.toml").read_text())
    assert pyproject["project"]["dynamic"] == ["version"]
    assert "optional-dependencies" not in pyproject["project"]
    assert pyproject["project"]["license"] == "BSD-3-Clause"
    assert not (project / "scripts").exists()
    version = re.search(
        pyproject["tool"]["hatch"]["version"]["pattern"],
        (project / "plugin/openstudio-si-plugin.yaml").read_text(),
    )["version"]
    assert version == data["manifest"]["version"]
    for path in project.rglob("*.py"):
        ast.parse(path.read_text())
    assert (project / "CLAUDE.md").read_text().startswith("@AGENTS.md")
    for path in (project / ".github/workflows").glob("*.yml"):
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        assert "jobs" in workflow and "on" in workflow
        assert "eval run" not in path.read_text() and "[eval]" not in path.read_text()
    case = json.loads((project / "plugin/evals/cases/layout-demo.json").read_text())
    schema = json.loads(
        files("openstudio_si_harness")
        .joinpath("schemas/evaluation-case.schema.json")
        .read_text()
    )
    Draft202012Validator(schema).validate(case)
    relocated = tmp_path / "relocated"
    project.rename(relocated)
    check = run_script(relocated, "check_project.py")
    assert check.returncode == 0, check.stderr
    report = export_plugin(relocated / "plugin", tmp_path / "exports")
    for path in report["packages"].values():
        package = Path(path)
        assert (package / "README.md").is_file() and (package / "LICENSE").is_file()
        assert (package / "evals/reference/layout.md").is_file()
        validate_export(package)
    assert "site-packages" in str(files("openstudio_si_harness"))


def test_dry_run_has_no_destination_side_effects_and_matches_generation(tmp_path):
    target = tmp_path / "new-parent/project"
    preview = init_project(target, identity="personal-plugin", dry_run=True)
    assert not preview["created"] and not target.parent.exists()
    target.mkdir(parents=True)
    result = init_project(target, identity="personal-plugin")
    assert result["files"] == preview["files"]
    assert load_plugin(target / "plugin")["manifest"]["owner"]["id"] == "individual"


@pytest.mark.parametrize("collision", ["file", "nonempty", "symlink", "broken-symlink"])
def test_existing_files_and_symlinks_are_preserved(tmp_path, collision):
    target = tmp_path / "target"
    if collision == "file":
        target.write_text("preserve")
    elif collision == "nonempty":
        target.mkdir()
        (target / "keep").write_text("preserve")
    else:
        other = tmp_path / "other"
        if collision == "symlink":
            other.mkdir()
        target.symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError, match="new or empty"):
        init_project(target, identity="my-plugin")
    if collision == "file":
        assert target.read_text() == "preserve"
    elif collision == "nonempty":
        assert (target / "keep").read_text() == "preserve"
    else:
        assert target.is_symlink()


@pytest.mark.parametrize(
    "arguments",
    [
        {"identity": "../escape"},
        {"identity": "openstudio-si"},
        {"identity": "openstudio-si-hvac"},
        {"identity": "valid-plugin", "owner": "Invalid Owner"},
        {"identity": "valid-plugin", "name": "Two\nlines"},
        {"identity": "valid-plugin", "copyright_holder": ""},
        {"identity": "valid-plugin", "version": "invalid"},
        {"identity": "valid-plugin", "version": "0.1.0-custom.1"},
        {"identity": "valid-plugin", "with_mcp": "unknown"},
    ],
)
def test_invalid_options_leave_no_project(tmp_path, arguments):
    target = tmp_path / "project"
    with pytest.raises(ValueError):
        init_project(target, **arguments)
    assert not target.exists()


def test_installed_console_init_command(tmp_path):
    executable = Path(sys.executable).with_name("openstudio-si")
    target = tmp_path / "cli project"
    command = [
        str(executable),
        "plugin",
        "init",
        str(target),
        "--id",
        "cli-demo",
        "--license",
        "MIT",
        "--with-mcp",
        "--maintainer",
        "Developer",
    ]
    preview = subprocess.run(
        command + ["--dry-run"],
        check=False,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert preview.returncode == 0, preview.stderr
    assert not target.exists()
    generated = subprocess.run(
        command, check=False, cwd=tmp_path, capture_output=True, text=True
    )
    assert generated.returncode == 0, generated.stderr
    assert json.loads(generated.stdout)["inventory"]["mcp_server_count"] == 1
    collision = subprocess.run(
        command, check=False, cwd=tmp_path, capture_output=True, text=True
    )
    assert collision.returncode == 1 and "never overwritten" in collision.stderr


def test_mcp_starter_initialization_discovery_and_tool_call(tmp_path):
    project = tmp_path / "mcp"
    init_project(project, identity="mcp-demo", with_mcp=True)
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "plugin_info", "arguments": {}},
        },
        {"jsonrpc": "2.0", "id": 4, "method": "unknown"},
    ]
    result = subprocess.run(
        [sys.executable, "-I", str(project / "src/si_mcp_demo/server.py")],
        check=False,
        input="".join(json.dumps(r) + "\n" for r in requests),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert [r["id"] for r in responses] == [1, 2, 3, 4]
    assert responses[0]["result"]["serverInfo"]["name"] == "mcp-demo"
    assert responses[1]["result"]["tools"][0]["name"] == "plugin_info"
    assert json.loads(responses[2]["result"]["content"][0]["text"])["id"] == "mcp-demo"
    assert responses[3]["error"]["code"] == -32601


@pytest.mark.parametrize("with_mcp", [False, True])
def test_release_archives_are_reproducible_and_provenance_is_recorded(
    tmp_path, with_mcp
):
    project = tmp_path / "project"
    init_project(project, identity="release-demo", with_mcp=with_mcp)
    report = export_plugin(project / "plugin", project / "dist/plugin")
    report_path = project / "dist/export-report.json"
    report_path.write_text(json.dumps(report))
    environment = {
        **os.environ,
        "RELEASE_TAG": "v0.1.0",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_REPOSITORY": "example/plugin",
    }
    first = run_script(project, "package_release.py", report_path, env=environment)
    assert first.returncode == 0, first.stderr
    record = json.loads(first.stdout)
    assert (
        record["source_sha"] == "a" * 40
        and record["evaluation"]["status"] == "not_provided"
    )
    assert set(record["packages"]) == {"claude", "codex"}
    assert record["destination"] == {
        "repository": "pnnl/openstudio-si-plugins",
        "operation": "plugin-scoped-pull-request",
        "status": "importer-pending",
    }
    for package in record["packages"].values():
        archive = project / "dist/release" / package["archive"]
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == package["sha256"]
    shutil.rmtree(project / "dist/release")
    second = run_script(project, "package_release.py", report_path, env=environment)
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout) == record


@pytest.mark.parametrize("failure", ["tag", "stale-source"])
def test_release_refuses_bad_tag_and_stale_export(tmp_path, failure):
    project = tmp_path / "project"
    init_project(project, identity="release-demo")
    report = export_plugin(project / "plugin", project / "dist/plugin")
    report_path = project / "dist/export-report.json"
    report_path.write_text(json.dumps(report))
    environment = {
        **os.environ,
        "RELEASE_TAG": "v9.0.0" if failure == "tag" else "v0.1.0",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_REPOSITORY": "example/plugin",
    }
    if failure == "stale-source":
        skill = project / "plugin/skills/release-demo/SKILL.md"
        skill.write_text(skill.read_text() + "\nChanged after export.\n")
    result = run_script(project, "package_release.py", report_path, env=environment)
    assert result.returncode != 0
    assert (
        "tag must match" if failure == "tag" else "differs from the current source"
    ) in result.stderr
    assert not (project / "dist/release").exists()


def test_existing_plugin_can_gain_skills_and_mcp_without_reclassification(tmp_path):
    project = tmp_path / "project"
    init_project(project, identity="combined-plugin")
    source = project / "plugin"
    original = source / "skills/combined-plugin/SKILL.md"
    added = source / "skills/additional-task/SKILL.md"
    added.parent.mkdir()
    added.write_text(
        original.read_text().replace("name: combined-plugin", "name: additional-task")
    )
    (source / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"extra_service": {"command": "extra-service"}}})
    )
    inspected = load_plugin(source)
    assert inspected["manifest"]["id"] == "combined-plugin"
    assert "kind" not in inspected["manifest"]
    assert inspected["inventory"] == {"skill_count": 2, "mcp_server_count": 1}
    report = export_plugin(source, tmp_path / "exports")
    assert report["inventory"] == inspected["inventory"]


def test_initialization_preserves_current_directory_inode(tmp_path, monkeypatch):
    target = tmp_path / "cwd"
    target.mkdir()
    inode = target.stat().st_ino
    monkeypatch.chdir(target)
    result = init_project(Path("."), identity="cwd-demo", license_identifier="MIT")
    assert Path.cwd() == target and target.stat().st_ino == inode
    assert (Path(".") / "plugin/openstudio-si-plugin.yaml").is_file()
    assert result["created"]


def test_every_exported_source_file_is_git_tracked(tmp_path):
    from openstudio_si_harness.project_tools import check_project

    project = tmp_path / "project"
    init_project(project, identity="tracked-demo", with_mcp=True)
    for directory in ("dist", "build", "venv"):
        file = project / f"plugin/skills/tracked-demo/assets/{directory}/app.js"
        file.parent.mkdir(parents=True)
        file.write_text("nested asset")
    subprocess.run(["git", "init", "--quiet", str(project)], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "core.excludesFile=/dev/null",
            "-C",
            str(project),
            "add",
            "--all",
        ],
        check=True,
    )
    check_project(project, require_tracked=True)
    export = export_plugin(project / "plugin", project / "dist/plugin")
    tracked = set(
        subprocess.check_output(["git", "-C", str(project), "ls-files", "-z"])
        .decode()
        .split("\0")
    )
    source = load_plugin(project / "plugin")
    for relative in [
        *source["files"],
        Path("openstudio-si-plugin.yaml"),
        Path(".mcp.json"),
    ]:
        assert (Path("plugin") / relative).as_posix() in tracked
        for package in export["packages"].values():
            assert (Path(package) / relative).is_file()
            if relative.as_posix() != ".mcp.json":
                assert (Path(package) / relative).read_bytes() == (
                    project / "plugin" / relative
                ).read_bytes()
    (project / "plugin/skills/tracked-demo/untracked.txt").write_text("not committed")
    with pytest.raises(ValueError, match="must be tracked"):
        check_project(project, require_tracked=True)


@pytest.mark.parametrize("license_identifier", ["MIT", "BSD-2-Clause", "BSD-3-Clause"])
def test_explicit_license_and_copyright_owner(tmp_path, license_identifier):
    project = tmp_path / "project"
    _init_project(
        project,
        identity="license-demo",
        license_identifier=license_identifier,
        organization="Laboratory Owner",
    )
    assert load_plugin(project / "plugin")["manifest"]["license"] == license_identifier
    assert "Laboratory Owner" in (project / "LICENSE").read_text()
    assert (project / "LICENSE").read_bytes() == (
        project / "plugin/LICENSE"
    ).read_bytes()
    other = tmp_path / "other"
    _init_project(
        other,
        identity="other-demo",
        license_identifier=license_identifier,
        organization="Lab",
        copyright_holder="Explicit Holder",
    )
    assert "Explicit Holder" in (other / "LICENSE").read_text()


def test_license_is_required_and_case_schema_remains_draft(tmp_path):
    with pytest.raises(TypeError):
        _init_project(tmp_path / "missing", identity="missing-license")
    project = tmp_path / "project"
    init_project(project, identity="draft-demo")
    case = json.loads((project / "plugin/evals/cases/layout-demo.json").read_text())
    assert case["schema_version"] == "0.1"
    case["schema_version"] = "1.0"
    (project / "plugin/evals/cases/layout-demo.json").write_text(json.dumps(case))
    result = run_script(project, "check_project.py")
    assert result.returncode != 0
