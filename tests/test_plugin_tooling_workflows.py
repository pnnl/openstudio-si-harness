"""Repository workflow contracts stay out of the standalone package test suite."""

import re
from pathlib import Path

import yaml


def test_reusable_build_upload_permission_boundary():
    contracts = Path(__file__).resolve().parents[1] / ".github/workflows"
    build = yaml.load(
        (contracts / "plugin-conformance.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    upload = yaml.load(
        (contracts / "plugin-candidate-upload.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert build["permissions"]["contents"] == "read"
    assert upload["permissions"]["contents"] == "write"
    assert "workflow_call" in build["on"] and "workflow_call" in upload["on"]
    for step in build["jobs"]["build"]["steps"]:
        if "uses" in step:
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])
        if step.get("uses", "").startswith("actions/checkout"):
            assert step["with"]["persist-credentials"] == "false"
        if "npm install" in step.get("run", ""):
            assert "--ignore-scripts" in step["run"]
    commands = "\n".join(s.get("run", "") for s in build["jobs"]["build"]["steps"])
    assert '"openstudio-si[plugin-dev]==$TOOLING_VERSION"' in commands
    assert "--index-url https://pypi.org/simple" in commands
    assert "tooling.lock" not in commands
    assert "--only-binary=:all:" in commands
    assert "--require-tracked" in commands
    assert "GH_TOKEN" not in commands and ".[dev]" not in commands
    for step in upload["jobs"]["upload"]["steps"]:
        assert not step.get("uses", "").startswith("actions/checkout")
        assert not any(
            needle in step.get("run", "")
            for needle in ("pip install", "npm install", "python ")
        )


def test_privileged_upload_actions_are_immutable_and_retries_replace_assets():
    root = Path(__file__).resolve().parents[1] / ".github/workflows"
    for name in ("plugin-candidate-upload.yml",):
        workflow = yaml.load((root / name).read_text(), Loader=yaml.BaseLoader)
        steps = workflow["jobs"]["upload"]["steps"]
        downloads = [
            step["uses"]
            for step in steps
            if step.get("uses", "").startswith("actions/download-artifact@")
        ]
        assert downloads and all(
            re.fullmatch(r"actions/download-artifact@[0-9a-f]{40}", ref)
            for ref in downloads
        )
        uploads = [
            step["run"] for step in steps if "gh release upload" in step.get("run", "")
        ]
        assert uploads and all("--clobber" in command for command in uploads)
        assert not any(
            step.get("uses", "").startswith("actions/checkout") for step in steps
        )
        assert not any(
            "pip install" in step.get("run", "") or "npm install" in step.get("run", "")
            for step in steps
        )


def test_tools_share_the_registered_pypi_release():
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    assert project["name"] == "openstudio-si"
    assert "plugin-dev" in project["optional-dependencies"]
    assert not (root / ".github/workflows/publish-plugin-tooling.yml").exists()
    assert "project" not in tomllib.loads(
        (root / "packages/plugin-tools/pyproject.toml").read_text()
    )


def test_released_doctor_baseline_is_independently_pinned():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.load(
        (root / ".github/workflows/plugin-tools.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    job = workflow["jobs"]["released-core-doctor"]
    assert job["env"]["CORE_BASELINE_URL"].endswith(
        "/openstudio_si-0.4.2-py3-none-any.whl"
    )
    assert (
        job["env"]["CORE_BASELINE_SHA256"]
        == "c81b9bae11269c4c09f4bb4c9b538934888e234abf5b5ac7d3b41d02d3c4caa0"
    )


def test_installed_tooling_preflight_detects_missing_commands(tmp_path):
    import json
    import os
    import subprocess
    import venv

    root = Path(__file__).resolve().parents[1]
    workflow = yaml.load(
        (root / ".github/workflows/plugin-conformance.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    steps = workflow["jobs"]["build"]["steps"]
    preflight = next(
        s for s in steps if s.get("name") == "Verify installed tooling command support"
    )
    assert steps.index(preflight) < next(
        i for i, s in enumerate(steps) if "npm install" in s.get("run", "")
    )
    script = preflight["run"].split("<<'PYTHON'\n", 1)[1].rsplit("PYTHON", 1)[0]
    environment = tmp_path / "tooling"
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    paths = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "import sysconfig,json; print(json.dumps(sysconfig.get_paths()))",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    site = Path(json.loads(paths.stdout)["purelib"])
    metadata = site / "openstudio_si-0.4.3.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "METADATA").write_text("Name: openstudio-si\nVersion: 0.4.3\n")
    command = python.with_name("openstudio-si")
    command.write_text(
        f"#!{python}\nimport os,sys\nsys.exit(2 if os.environ.get('MISSING_ACTION') == sys.argv[2] else 0)\n"
    )
    command.chmod(0o755)
    for version, missing, message in [
        ("0.4.3", "", ""),
        ("0.4.2", "", "requires the selected openstudio-si>=0.4.3"),
        ("0.4.3", "export", "lacks required plugin command: export"),
    ]:
        result = subprocess.run(
            [str(python), "-I", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "TOOLING_VERSION": version, "MISSING_ACTION": missing},
        )
        assert (result.returncode == 0) == (not message), result.stderr
        assert message in result.stderr
