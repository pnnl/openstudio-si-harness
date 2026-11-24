"""Compare exported shell launching with the actual direct-probe path."""

import json
import os
import subprocess

import pytest
from openstudio_si_harness import mcp

from .test_mcp import checked, setup_service


@pytest.mark.parametrize(
    "mode",
    [
        "path",
        "override",
        "home-local",
        "home-cargo",
        "system-first",
        "system-second",
        "missing",
        "relative-override",
        "chained-env",
        "introduced-env",
        "missing-ref",
        "empty-ref",
    ],
)
def test_exported_and_direct_launch_have_matching_inputs(
    tmp_path, selected_python, monkeypatch, mode
):
    receipt = tmp_path / "launch.json"
    home = tmp_path / "home"
    directories = [
        tmp_path / "path-first",
        tmp_path / "path-second",
        home / ".local/bin",
        home / ".cargo/bin",
        tmp_path / "system-first",
        tmp_path / "system-second",
    ]
    monkeypatch.setattr(
        mcp, "SYSTEM_COMMAND_DIRS", tuple(str(p) for p in directories[4:])
    )
    original_env = {
        "PATH": os.pathsep.join(map(str, directories[:2])),
        "HOME": str(home),
        "CAPTURE": str(receipt),
        "SOURCE_A": "" if mode == "empty-ref" else "original 'quoted' value",
        "SOURCE_B": "mapped \\ value",
    }
    forwards = {}
    for key, value in original_env.items():
        if key != "PATH":
            source = "SI_PARITY_" + key
            monkeypatch.setenv(source, value)
            forwards[key] = source
    root, profile, _, _, _ = setup_service(tmp_path, selected_python, env=forwards)
    actor = (tmp_path / "bin/test-mcp-service").read_text()
    actor = actor.replace(
        "for line in sys.stdin:",
        "pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps({'executable':sys.argv[0],'argv':sys.argv[1:],'cwd':os.getcwd(),'env':{key:os.environ.get(key) for key in ['HOME','SOURCE_A','SOURCE_B','RESULT']}}))\nfor line in sys.stdin:",
        1,
    )
    skip = {
        "home-local": 2,
        "home-cargo": 3,
        "system-first": 4,
        "system-second": 5,
        "missing": 6,
    }.get(mode, 0)
    for folder in directories[skip:]:
        folder.mkdir(parents=True)
        executable = folder / "test-mcp-service"
        executable.write_text(actor)
        executable.chmod(0o755)
    if mode in {"override", "relative-override"}:
        override = tmp_path / "override with spaces"
        override.write_text(actor)
        override.chmod(0o755)
        value = str(override) if mode == "override" else "relative-command"
        original_env[mcp.override_variable("standards_service")] = value
        monkeypatch.setenv("SI_PARITY_OVERRIDE", value)
        forwards[mcp.override_variable("standards_service")] = "SI_PARITY_OVERRIDE"
    declaration = {
        "command": "test-mcp-service",
        "args": ["--label", "two words", "quoted'argument"],
        "env": {"RESULT": "${SOURCE_A}"},
    }
    if mode == "chained-env":
        declaration["env"] = {"SOURCE_A": "${SOURCE_B}", "RESULT": "${SOURCE_A}"}
    if mode == "introduced-env":
        declaration["env"] = {"DERIVED": "${SOURCE_B}", "RESULT": "${DERIVED}"}
    if mode == "missing-ref":
        declaration["env"] = {"RESULT": "${NOT_FORWARDED}"}
    (root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"standards_service": declaration}})
    )
    data = json.loads(profile.read_text())
    data["runtimes"]["selected"]["path"] = [str(p) for p in directories[:2]]
    data["runtimes"]["selected"]["env"] = forwards
    profile.write_text(json.dumps(data))
    launcher = mcp.host_servers({"standards_service": declaration})["standards_service"]
    host = subprocess.run(
        [launcher["command"], *launcher["args"]],
        cwd=tmp_path,
        env=original_env,
        input="",
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    expected = json.loads(receipt.read_text()) if receipt.exists() else None
    receipt.unlink(missing_ok=True)
    report = checked(root, profile)
    if mode in {"missing", "relative-override", "missing-ref", "empty-ref"}:
        assert host.returncode != 0
        assert report["status"] == "unavailable"
        assert not receipt.exists()
    else:
        assert host.returncode == 0, host.stderr
        assert report["status"] == "ready", report["plugins"][0]["dependencies"][0]
        assert json.loads(receipt.read_text()) == expected
        assert (
            report["plugins"][0]["dependencies"][0]["evidence"]["server_executable"]
            == expected["executable"]
        )
