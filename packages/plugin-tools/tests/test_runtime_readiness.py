"""Selected-runtime probes use isolated environments and bounded real processes."""

import json
import os
import subprocess
import sys
import time
import venv
from pathlib import Path

import pytest
from openstudio_si_harness.cli import main
from openstudio_si_harness.readiness import doctor
from test_readiness import capability, dependency, plugin


@pytest.fixture(scope="module")
def selected_python(tmp_path_factory):
    root = tmp_path_factory.mktemp("selected-python")
    venv.EnvBuilder(with_pip=False, symlinks=True).create(root)
    python = root / "bin/python"
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
        timeout=20,
        check=True,
    )
    site = Path(result.stdout.strip())
    metadata = site / "si_runtime_fixture-7.2.1.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: si-runtime-fixture\nVersion: 7.2.1\n"
    )
    (site / "si_runtime_fixture.py").write_text(
        "raise RuntimeError('Target package must never be imported')\n"
    )
    return python


def profile(
    tmp_path,
    selected_python,
    dependencies,
    *,
    env=None,
    allow_version=False,
    limits=None,
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    data = {
        "schema_version": "0.1",
        "host": "codex",
        "runtimes": {
            "selected": {
                "python": str(selected_python),
                "cwd": str(tmp_path),
                "path": [str(bin_dir)],
                "env": env or {},
            }
        },
        "bindings": [
            {
                "plugin": "consumer",
                "dependency": d["id"],
                "runtime": "selected",
                **(
                    {"allow_command_version": True}
                    if allow_version and d["kind"] == "command"
                    else {}
                ),
            }
            for d in dependencies
        ],
    }
    if limits:
        data["limits"] = limits
    path = tmp_path / "runtime.local.json"
    path.write_text(json.dumps(data))
    return path


def probe(root, path, **kwargs):
    return doctor([root], runtime_profile=path, check_runtime=True, **kwargs)


def tool(tmp_path, selected_python, body):
    path = tmp_path / "bin/version-tool"
    path.write_text(f"#!{selected_python}\n" + body + "\n")
    path.chmod(0o755)
    return path


def test_profile_without_flag_is_passive(tmp_path, selected_python, monkeypatch):
    from openstudio_si_harness import runtime

    deps = [
        dependency("python", kind="python-distribution", target="si-runtime-fixture")
    ]
    root = plugin(tmp_path, "consumer", deps)
    path = profile(tmp_path, selected_python, deps)

    def forbidden(*args, **kwargs):
        raise AssertionError("Inventory must not launch subprocesses")

    monkeypatch.setattr(runtime, "process", forbidden)
    report = doctor([root], runtime_profile=path)
    assert report["scope"] == "metadata_only" and report["status"] == "unknown"
    assert (
        report["plugins"][0]["dependencies"][0]["reason"]
        == "runtime_check_not_requested"
    )


@pytest.mark.parametrize(
    "target,spec,status,reason",
    [
        ("si-runtime-fixture", ">=7,<8", "ready", "distribution_metadata"),
        ("si-runtime-fixture", ">=8", "unavailable", "distribution_version_conflict"),
        ("jsonschema", ">=1", "unavailable", "distribution_missing"),
    ],
)
def test_distribution_in_selected_venv_without_tooling_or_target_imports(
    tmp_path, selected_python, target, spec, status, reason
):
    deps = [
        dependency(
            "python", kind="python-distribution", target=target, version_spec=spec
        )
    ]
    root = plugin(tmp_path, "consumer", deps)
    path = profile(tmp_path, selected_python, deps)
    report = probe(root, path)
    result = report["plugins"][0]["dependencies"][0]
    assert report["status"] == status and result["reason"] == reason
    assert result["evidence"]["python"] == str(selected_python)
    assert result["evidence"]["measurement_scope"] == "selected_runtime"
    assert report["plugins"][0]["host_enabled"] == "unknown"
    assert report["plugins"][0]["service_connected"] == "unknown"
    assert "si_runtime_fixture" not in sys.modules


@pytest.mark.parametrize(
    "forward,present", [(False, True), (True, True), (True, False)]
)
def test_configuration_uses_only_explicit_environment_references(
    tmp_path, selected_python, monkeypatch, forward, present
):
    monkeypatch.setenv("TOKEN", "ambient-token-must-not-be-used")
    if present:
        monkeypatch.setenv("SI_D2_TEST_TOKEN", "explicit-token-must-not-be-recorded")
    else:
        monkeypatch.delenv("SI_D2_TEST_TOKEN", raising=False)
    deps = [
        dependency(
            "config",
            kind="configuration",
            target="TOKEN",
            probe="configuration-present",
        )
    ]
    root = plugin(tmp_path, "consumer", deps)
    path = profile(
        tmp_path,
        selected_python,
        deps,
        env={"TOKEN": "SI_D2_TEST_TOKEN"} if forward else {},
    )
    report = probe(root, path)
    assert report["status"] == ("ready" if forward and present else "unavailable")
    assert "must-not-be" not in json.dumps(report)
    assert report["plugins"][0]["dependencies"][0]["evidence"]["present"] is (
        forward and present
    )


def test_command_resolution_does_not_execute_or_use_terminal_path(
    tmp_path, selected_python, monkeypatch
):
    deps = [dependency("command", kind="command", target="version-tool")]
    root = plugin(tmp_path, "consumer", deps)
    path = profile(tmp_path, selected_python, deps)
    marker = tmp_path / "executed"
    executable = tool(
        tmp_path,
        selected_python,
        f"from pathlib import Path; Path({str(marker)!r}).touch()",
    )
    assert probe(root, path)["status"] == "ready"
    assert not marker.exists()
    executable.unlink()
    monkeypatch.setenv("PATH", os.defpath)
    assert probe(root, path)["status"] == "unavailable"


@pytest.mark.parametrize(
    "allow,output,spec,status,reason",
    [
        (
            False,
            "tool 1.2.3",
            ">=1.0.0 <2.0.0",
            "unknown",
            "command_version_not_authorized",
        ),
        (True, "tool 1.2.3", ">=1.0.0 <2.0.0", "ready", "command_version"),
        (True, "tool 1.2.3", ">=2.0.0", "unavailable", "command_version_conflict"),
        (True, "tool 2.0.0-rc.1", "<2.0.0", "unavailable", "command_version_conflict"),
        (
            True,
            "tool 1.2.3 uses library 2.3.4",
            ">=1.0.0",
            "unknown",
            "command_version_ambiguous",
        ),
    ],
)
def test_command_version_requires_permission_and_has_strict_parser(
    tmp_path, selected_python, allow, output, spec, status, reason
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
    path = profile(tmp_path, selected_python, deps, allow_version=allow)
    marker = tmp_path / "executed"
    tool(
        tmp_path,
        selected_python,
        f"import sys; from pathlib import Path\nassert sys.argv[1:] == ['--version']\nPath({str(marker)!r}).touch()\nprint({output!r})",
    )
    result = probe(root, path)["plugins"][0]["dependencies"][0]
    assert result["status"] == status and result["reason"] == reason
    assert marker.exists() is allow


@pytest.mark.parametrize(
    "mode,reason", [("timeout", "runtime_timeout"), ("output", "runtime_output_limit")]
)
def test_command_probe_is_bounded_and_raw_output_is_not_reported(
    tmp_path, selected_python, mode, reason
):
    deps = [
        dependency(
            "command", kind="command", target="version-tool", probe="command-version"
        )
    ]
    root = plugin(tmp_path, "consumer", deps)
    path = profile(
        tmp_path,
        selected_python,
        deps,
        allow_version=True,
        limits={"timeout_seconds": 0.5, "max_output_bytes": 1024},
    )
    body = (
        "import time; time.sleep(30)"
        if mode == "timeout"
        else "import os,time; os.write(1, b'D2_PRIVATE_OUTPUT' * 10000); time.sleep(30)"
    )
    tool(tmp_path, selected_python, body)
    started = time.monotonic()
    report = probe(root, path)
    assert time.monotonic() - started < 4
    assert report["plugins"][0]["dependencies"][0]["reason"] == reason
    assert "D2_PRIVATE_OUTPUT" not in json.dumps(report)


def test_successful_probe_cleans_ordinary_child_processes(tmp_path, selected_python):
    deps = [
        dependency(
            "command", kind="command", target="version-tool", probe="command-version"
        )
    ]
    root = plugin(tmp_path, "consumer", deps)
    path = profile(tmp_path, selected_python, deps, allow_version=True)
    marker = tmp_path / "orphan-ran"
    child = f"import time; from pathlib import Path; time.sleep(.4); Path({str(marker)!r}).touch()"
    tool(
        tmp_path,
        selected_python,
        f"import subprocess,sys\nsubprocess.Popen([sys.executable, '-c', {child!r}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\nprint('tool 1.2.3')",
    )
    assert probe(root, path)["status"] == "ready"
    time.sleep(0.6)
    assert not marker.exists()


def test_targeted_capability_does_not_run_unneeded_bindings(tmp_path, selected_python):
    deps = [
        dependency(
            "optional-command",
            required=False,
            kind="command",
            target="version-tool",
            probe="command-version",
        )
    ]
    root = plugin(
        tmp_path,
        "consumer",
        deps,
        [capability("lookup"), capability("version", ["optional-command"])],
    )
    path = profile(tmp_path, selected_python, deps, allow_version=True)
    marker = tmp_path / "unneeded-command"
    tool(
        tmp_path,
        selected_python,
        f"from pathlib import Path; Path({str(marker)!r}).touch(); print('1.2.3')",
    )
    report = probe(root, path, plugin_id="consumer", capability="lookup")
    assert report["status"] == "ready" and not marker.exists()
    assert (
        report["plugins"][0]["dependencies"][0]["reason"] == "runtime_outside_selection"
    )


@pytest.mark.parametrize(
    "change",
    [
        "relative-python",
        "duplicate-binding",
        "host-mismatch",
        "python-env",
        "literal-env",
        "nonfinite-limit",
    ],
)
def test_invalid_profile_is_rejected_before_execution(
    tmp_path, selected_python, change, monkeypatch
):
    from openstudio_si_harness import runtime

    deps = [
        dependency(
            "config",
            kind="configuration",
            target="TOKEN",
            probe="configuration-present",
        )
    ]
    root = plugin(tmp_path, "consumer", deps)
    path = profile(tmp_path, selected_python, deps)
    data = json.loads(path.read_text())
    if change == "relative-python":
        data["runtimes"]["selected"]["python"] = "python"
    elif change == "duplicate-binding":
        data["bindings"].append(data["bindings"][0])
    elif change == "python-env":
        data["runtimes"]["selected"]["env"]["PYTHONPATH"] = "SI_PATH"
    elif change == "literal-env":
        data["runtimes"]["selected"]["env"]["TOKEN"] = "not a variable name"
    elif change == "nonfinite-limit":
        data["limits"] = {"timeout_seconds": float("nan")}
    path.write_text(json.dumps(data))

    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid profile executed a probe")

    monkeypatch.setattr(runtime, "process", forbidden)
    with pytest.raises(ValueError):
        probe(root, path, host="claude" if change == "host-mismatch" else "codex")


def test_cli_requires_profile_for_runtime_checks(capsys):
    assert main(["plugin", "doctor", "--check-runtime"]) == 1
    assert "requires --runtime-profile" in capsys.readouterr().err


def test_selected_provider_runtime_is_followed_transitively(tmp_path, selected_python):
    deps = [
        dependency("python", kind="python-distribution", target="si-runtime-fixture")
    ]
    provider = plugin(tmp_path, "provider", deps)
    consumer = plugin(tmp_path, "consumer", [dependency()])
    path = profile(tmp_path, selected_python, deps)
    data = json.loads(path.read_text())
    data["bindings"][0]["plugin"] = "provider"
    path.write_text(json.dumps(data))
    report = doctor(
        [consumer, provider],
        plugin_id="consumer",
        runtime_profile=path,
        check_runtime=True,
    )
    assert report["status"] == "ready"
    assert report["plugins"][0]["dependencies"][0]["status"] == "ready"


def test_total_budget_prevents_later_probes_from_starting(tmp_path, selected_python):
    deps = [
        dependency(name, kind="command", target="version-tool", probe="command-version")
        for name in ("first", "second")
    ]
    root = plugin(tmp_path, "consumer", deps)
    path = profile(
        tmp_path,
        selected_python,
        deps,
        allow_version=True,
        limits={"total_seconds": 0.5, "timeout_seconds": 5},
    )
    marker = tmp_path / "executions"
    tool(
        tmp_path,
        selected_python,
        f"import time; from pathlib import Path; p=Path({str(marker)!r}); p.write_text(p.read_text()+'1' if p.exists() else '1'); time.sleep(30)",
    )
    report = probe(root, path)
    assert all(
        d["reason"] == "runtime_timeout" for d in report["plugins"][0]["dependencies"]
    )
    assert marker.read_text() == "1"
