"""Version formats, safe metadata paths and reusable runtime profiles."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from openstudio_si_harness.cli import main
from openstudio_si_harness.runtime import RuntimeSelection
from test_readiness import dependency, plugin
from test_runtime_readiness import probe, profile, tool

pytest_plugins = ["test_runtime_readiness"]


def edit_profile(path, change):
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


def selected_site(python):
    result = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ],
        env={"PATH": os.defpath},
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return Path(result.stdout.strip())


def distribution(site, name, version):
    root = site / f"{name.replace('-', '_')}-{version}.dist-info"
    root.mkdir(parents=True)
    (root / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    )
    return root


@pytest.mark.parametrize(
    "output,fmt,spec,status,reason",
    [
        ("v20.11.0", None, ">=20.0.0", "ready", "command_version"),
        (
            "EnergyPlus, Version 24.1.0-9d7789a3ac",
            "dash-build",
            ">=24.1.0",
            "ready",
            "command_version",
        ),
        (
            "EnergyPlus, Version 24.1.0-9d7789a3ac",
            None,
            ">=24.1.0",
            "unknown",
            "command_version_format_required",
        ),
        (
            "24.1.0-9d7789a3ac",
            "semver",
            ">=24.1.0",
            "unavailable",
            "command_version_conflict",
        ),
        ("openstudio 3.11.0+241b8abb4e", None, ">=3.11.0", "ready", "command_version"),
        ("2.0.0-rc.1", "semver", "<2.0.0", "unavailable", "command_version_conflict"),
        (
            "24.1.0-rc.1",
            "dash-build",
            ">=24.1.0",
            "unknown",
            "command_version_format_mismatch",
        ),
    ],
)
def test_vendor_version_formats(
    tmp_path, selected_python, output, fmt, spec, status, reason
):
    deps = [
        dependency(
            "command",
            kind="command",
            target="version-tool",
            probe="command-version",
            version_spec=spec,
        )
    ]
    root = plugin(tmp_path, "consumer", deps)
    path = profile(tmp_path, selected_python, deps, allow_version=True)
    if fmt:
        edit_profile(path, lambda data: data["bindings"][0].update(version_format=fmt))
    tool(tmp_path, selected_python, f"print({output!r})")
    result = probe(root, path)["plugins"][0]["dependencies"][0]
    assert result["status"] == status and result["reason"] == reason
    if fmt == "dash-build" and status == "ready":
        assert result["evidence"]["version"] == "24.1.0+9d7789a3ac"


@pytest.mark.parametrize("stderr_only", [True, False])
def test_version_streams_are_combined_without_raw_output(
    tmp_path, selected_python, stderr_only
):
    deps = [
        dependency(
            "command", kind="command", target="version-tool", probe="command-version"
        )
    ]
    root = plugin(tmp_path, "consumer", deps)
    path = profile(tmp_path, selected_python, deps, allow_version=True)
    tool(
        tmp_path,
        selected_python,
        "import sys\nprint('v20.11.0', file=sys.stderr)\n"
        + ("print('unrelated-tool 1.2.3')" if not stderr_only else ""),
    )
    report = probe(root, path)
    assert report["status"] == ("ready" if stderr_only else "unknown")
    assert report["runtime_selection"]["probes_executed"] == 2
    assert "unrelated-tool" not in json.dumps(report)


def test_pth_hooks_are_not_executed_and_missing_scope_stays_unknown(
    tmp_path, selected_python
):
    site = selected_site(selected_python)
    marker = tmp_path / "pth-import-executed"
    hook = site / "si_runtime_probe.pth"
    hook.write_text(f"import pathlib; pathlib.Path({str(marker)!r}).touch()\n")
    try:
        deps = [
            dependency(
                "python", kind="python-distribution", target="si-runtime-fixture"
            ),
            dependency(
                "missing", kind="python-distribution", target="si-pth-only-package"
            ),
            dependency(
                "version",
                kind="python-distribution",
                target="si-runtime-fixture",
                version_spec=">=8",
            ),
        ]
        root = plugin(tmp_path, "consumer", deps)
        report = probe(root, profile(tmp_path, selected_python, deps))
        assert not marker.exists()
        checks = report["plugins"][0]["dependencies"]
        assert checks[0]["status"] == "ready"
        assert checks[1]["status"] == "unknown"
        assert checks[1]["reason"] == "distribution_missing_isolated_scope"
        assert checks[2]["status"] == "unknown"
        assert checks[2]["reason"] == "distribution_version_isolated_scope"
    finally:
        hook.unlink()


@pytest.mark.parametrize("explicit", [False, True])
def test_base_interpreter_user_site_and_pythonpath_are_not_false_missing(
    tmp_path, selected_python, monkeypatch, explicit
):
    base = selected_python.resolve()
    home = tmp_path / "synthetic-home"
    env = {"PATH": os.defpath, "HOME": str(home)}
    user = subprocess.run(
        [str(base), "-c", "import site; print(site.getusersitepackages())"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    user_site = Path(user.stdout.strip())
    pythonpath = tmp_path / "pythonpath-packages"
    distribution(user_site, "si-user-only", "1.0.0")
    distribution(pythonpath, "si-pythonpath-only", "1.0.0")
    env["PYTHONPATH"] = str(pythonpath)
    normal = subprocess.run(
        [
            str(base),
            "-c",
            "import importlib.metadata as m; print(m.version('si-user-only'), m.version('si-pythonpath-only'))",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert normal.stdout.strip() == "1.0.0 1.0.0"
    monkeypatch.setenv("SI_REVIEW_HOME", str(home))
    deps = [
        dependency(name, kind="python-distribution", target=target)
        for name, target in (
            ("user", "si-user-only"),
            ("pythonpath", "si-pythonpath-only"),
        )
    ]
    root = plugin(tmp_path, "consumer", deps)
    path = profile(tmp_path, base, deps, env={"HOME": "SI_REVIEW_HOME"})
    if explicit:
        edit_profile(
            path,
            lambda data: data["runtimes"]["selected"].update(
                metadata_paths=[str(user_site), str(pythonpath)]
            ),
        )
    report = probe(root, path)
    assert report["status"] == ("ready" if explicit else "unknown")
    for check in report["plugins"][0]["dependencies"]:
        assert check["status"] == ("ready" if explicit else "unknown")
        if not explicit:
            assert check["reason"] == "distribution_missing_isolated_scope"


@pytest.mark.parametrize(
    "version,spec",
    [
        ("1.3.0.dev12+g1234abc", ">=1.2"),
        ("2.0.0rc1", ">=1.2"),
        ("0.4.3.dev12+g1234abc", ">=0.4.2"),
    ],
)
def test_installed_distribution_prereleases_are_explicitly_allowed(
    tmp_path, selected_python, version, spec
):
    metadata = distribution(selected_site(selected_python), "si-dev-fixture", version)
    try:
        deps = [
            dependency(
                "python",
                kind="python-distribution",
                target="si-dev-fixture",
                version_spec=spec,
            )
        ]
        root = plugin(tmp_path, "consumer", deps)
        assert (
            probe(root, profile(tmp_path, selected_python, deps))["status"] == "ready"
        )
    finally:
        shutil.rmtree(metadata)


@pytest.mark.parametrize(
    "case", ["other-plugin", "unknown-dependency", "invalid-manifest"]
)
def test_shared_profile_and_invalid_plugin_do_not_raise(
    tmp_path, selected_python, capsys, case
):
    deps = [
        dependency("python", kind="python-distribution", target="si-runtime-fixture")
    ]
    root = plugin(tmp_path, "consumer", deps)
    path = profile(tmp_path, selected_python, deps)
    if case == "other-plugin":
        edit_profile(
            path,
            lambda data: data["bindings"].append(
                {
                    "plugin": "other-plugin",
                    "dependency": "python",
                    "runtime": "selected",
                }
            ),
        )
    elif case == "unknown-dependency":
        edit_profile(
            path, lambda data: data["bindings"][0].update(dependency="missing")
        )
    else:
        manifest = root / "openstudio-si-plugin.yaml"
        data = yaml.safe_load(manifest.read_text())
        data["dependencies"][0]["version_spec"] = "^7.2"
        manifest.write_text(yaml.safe_dump(data))
    assert main(
        [
            "plugin",
            "doctor",
            "--plugin-dir",
            str(root),
            "--runtime-profile",
            str(path),
            "--check-runtime",
        ]
    ) == (0 if case == "other-plugin" else 2)
    report = json.loads(capsys.readouterr().out)
    assert report["runtime_selection"]["unused_bindings"]
    if case == "invalid-manifest":
        assert report["plugins"][0]["reason"] == "invalid_or_unreadable_metadata"
    if case != "other-plugin":
        assert report["scope"] == "metadata_only"
        assert report["runtime_selection"]["probes_executed"] == 0


@pytest.mark.parametrize("value,ready", [("7", True), ("7.2.1", False)])
def test_short_forwarded_values_do_not_mask_version_components(
    tmp_path, selected_python, monkeypatch, value, ready
):
    monkeypatch.setenv("SI_REVIEW_FORWARDED", value)
    deps = [
        dependency("python", kind="python-distribution", target="si-runtime-fixture")
    ]
    root = plugin(tmp_path, "consumer", deps)
    path = profile(
        tmp_path, selected_python, deps, env={"OMP_NUM_THREADS": "SI_REVIEW_FORWARDED"}
    )
    report = probe(root, path)
    assert report["status"] == ("ready" if ready else "unknown")
    if not ready:
        assert "7.2.1" not in json.dumps(report)


def test_budget_starts_at_first_probe_not_profile_load(tmp_path, selected_python):
    deps = [
        dependency("python", kind="python-distribution", target="si-runtime-fixture")
    ]
    path = profile(tmp_path, selected_python, deps)
    selection = RuntimeSelection(path, check=True)
    assert selection.deadline is None and selection.probes_executed == 0
    entry = {
        "host": "codex",
        "id": "consumer",
        "_loaded": {"manifest": {"dependencies": deps}},
    }
    selection.validate_bindings([entry])
    assert selection.probe(deps[0], entry)["status"] == "ready"
    assert selection.deadline is not None and selection.probes_executed == 1


def test_targeted_invalid_manifest_stays_visible(tmp_path, selected_python):
    deps = [
        dependency("python", kind="python-distribution", target="si-runtime-fixture")
    ]
    root = plugin(tmp_path, "consumer", deps)
    path = profile(tmp_path, selected_python, deps)
    (root / "openstudio-si-plugin.yaml").write_text("schema_version: invalid\n")
    report = probe(root, path, plugin_id="consumer", capability="layout-demo")
    assert report["status"] == "unknown"
    assert report["plugins"][0]["reason"] == "invalid_or_unreadable_metadata"
    assert report["runtime_selection"]["unused_bindings"]
    assert report["runtime_selection"]["probes_executed"] == 0
