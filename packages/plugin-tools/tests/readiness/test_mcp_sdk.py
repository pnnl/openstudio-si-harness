"""Official SDK server regressions for D3 review X-1 through X-6."""

import json
import shlex
import sys
import time

import pytest
from openstudio_si_harness import mcp as launchers

from .test_mcp import checked, setup_service


def sdk_service(
    tmp_path,
    selected_python,
    *,
    tools=3,
    version=None,
    version_spec=None,
    limits=None,
    chatty=False,
    wrapper_utilities=False,
):
    root, profile, _, canary, _ = setup_service(
        tmp_path, selected_python, version_spec=version_spec, limits=limits
    )
    server = tmp_path / "sdk_server.py"
    server.write_text(f'''
import json,os,pathlib,sys
from mcp.server.mcpserver import MCPServer
app = MCPServer('official-sdk-fixture', **({{'version':{version!r}}} if {version is not None!r} else {{}}))
for i in range({tools}):
    def query(x: int, y: str = 'a', z: float = 1.0) -> str:
        """A typical tool description that explains the lookup task, its input arguments, and the reviewed output expected by the user."""
        pathlib.Path({str(canary)!r}).touch()
        return 'not expected to run'
    app.add_tool(query, name=f'tool_{{i:03d}}')
if {chatty!r}:
    sys.stderr.write('INFO private server startup detail\\n' * 10000)
    sys.stderr.flush()
app.run()
''')
    command = tmp_path / "bin/test-mcp-service"
    command.write_text(
        "#!/bin/sh\nset -eu\n"
        + ('directory=$(dirname "$0")\n' if wrapper_utilities else "")
        + f"exec {shlex.quote(sys.executable)} {shlex.quote(str(server))}\n"
    )
    command.chmod(0o755)
    return root, profile, command, canary


@pytest.mark.parametrize(
    "version,spec,status",
    [
        (None, None, "ready"),
        (None, ">=1.0.0", "unknown"),
        ("1.2.3", ">=1.0.0", "ready"),
    ],
)
def test_official_sdk_server_versions(tmp_path, selected_python, version, spec, status):
    root, profile, _, canary = sdk_service(
        tmp_path, selected_python, version=version, version_spec=spec
    )
    report = checked(root, profile)
    dep = report["plugins"][0]["dependencies"][0]
    assert report["status"] == status, dep
    assert dep["evidence"]["server_version"] == version
    if spec and version is None:
        assert dep["reason"] == "mcp_server_version_invalid"
    assert not canary.exists()


@pytest.mark.parametrize("count", [40, 150, 200])
def test_official_sdk_large_inventory_fits_default(tmp_path, selected_python, count):
    root, profile, _, canary = sdk_service(tmp_path, selected_python, tools=count)
    report = checked(root, profile)
    dep = report["plugins"][0]["dependencies"][0]
    assert report["status"] == "ready", dep
    assert len(dep["evidence"]["tools"]) == count
    assert not canary.exists()


def test_explicit_protocol_limit_has_actionable_hint(tmp_path, selected_python):
    root, profile, _, _ = sdk_service(
        tmp_path, selected_python, tools=200, limits={"max_output_bytes": 65536}
    )
    dep = checked(root, profile)["plugins"][0]["dependencies"][0]
    assert dep["status"] == "unknown"
    assert dep["reason"] == "mcp_output_limit"
    assert "limits.max_output_bytes" in dep["next_action"]


def test_missing_executable_is_unavailable_without_startup(
    tmp_path, selected_python, monkeypatch
):
    root, profile, log, _, _ = setup_service(tmp_path, selected_python)
    (tmp_path / "bin/test-mcp-service").unlink()
    monkeypatch.setattr(launchers, "HOME_COMMAND_DIRS", ())
    monkeypatch.setattr(launchers, "SYSTEM_COMMAND_DIRS", ())
    report = checked(root, profile)
    dep = report["plugins"][0]["dependencies"][0]
    assert report["status"] == "unavailable"
    assert dep["reason"] == "mcp_server_command_not_found"
    assert report["runtime_selection"]["probes_executed"] == 0
    assert not log.exists()


def test_sigterm_ignoring_server_preserves_completed_probe(tmp_path, selected_python):
    root, profile, _, _, _ = setup_service(tmp_path, selected_python)
    executable = tmp_path / "bin/test-mcp-service"
    text = executable.read_text().replace(
        "import json,sys",
        "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN)\nimport json,sys",
        1,
    )
    executable.write_text(text + "\ntime.sleep(30)\n")
    start = time.monotonic()
    report = checked(root, profile)
    assert report["status"] == "ready", report["plugins"][0]["dependencies"][0]
    assert time.monotonic() - start < 3


def test_official_sdk_startup_logs_do_not_consume_protocol_budget(
    tmp_path, selected_python
):
    root, profile, _, _ = sdk_service(
        tmp_path,
        selected_python,
        tools=3,
        chatty=True,
        limits={"max_output_bytes": 16384},
    )
    report = checked(root, profile)
    assert report["status"] == "ready", report["plugins"][0]["dependencies"][0]
    assert "private server startup" not in json.dumps(report)


@pytest.mark.parametrize("mode", ["absent", "profile", "declaration"])
def test_home_is_explicit_and_recorded(tmp_path, selected_python, monkeypatch, mode):
    root, profile, _, _ = sdk_service(tmp_path, selected_python)
    if mode != "absent":
        home = tmp_path / "selected-home"
        home.mkdir()
        monkeypatch.setenv("SI_NATIVE_HOME", str(home))
        data = json.loads(profile.read_text())
        data["runtimes"]["selected"]["env"][
            "HOME" if mode == "profile" else "SERVICE_HOME"
        ] = "SI_NATIVE_HOME"
        profile.write_text(json.dumps(data))
        if mode == "declaration":
            source = root / ".mcp.json"
            config = json.loads(source.read_text())
            config["mcpServers"]["standards_service"]["env"] = {
                "HOME": "${SERVICE_HOME}"
            }
            source.write_text(json.dumps(config))
    report = checked(root, profile)
    dep = report["plugins"][0]["dependencies"][0]
    assert report["status"] == "ready"
    assert dep["evidence"]["home_present"] is (mode != "absent")
    assert dep["evidence"]["resolution_source"] == "profile_path"
    assert dep["evidence"]["server_executable"] == str(
        tmp_path / "bin/test-mcp-service"
    )


def test_desktop_fallback_records_actual_path(tmp_path, selected_python, monkeypatch):
    root, profile, command, _ = sdk_service(tmp_path, selected_python)
    fallback = tmp_path / "desktop-bin"
    fallback.mkdir()
    moved = fallback / command.name
    command.rename(moved)
    monkeypatch.setattr(launchers, "HOME_COMMAND_DIRS", ())
    monkeypatch.setattr(launchers, "SYSTEM_COMMAND_DIRS", (str(fallback),))
    report = checked(root, profile)
    dep = report["plugins"][0]["dependencies"][0]
    assert report["status"] == "ready", dep
    assert dep["evidence"]["resolution_source"] == "desktop_fallback"
    assert dep["evidence"]["server_executable"] == str(moved)


@pytest.mark.parametrize("system_path", [False, True])
def test_shell_wrapper_utilities_need_explicit_system_path(
    tmp_path, selected_python, system_path
):
    root, profile, _, _ = sdk_service(tmp_path, selected_python, wrapper_utilities=True)
    if system_path:
        data = json.loads(profile.read_text())
        data["runtimes"]["selected"]["path"].extend(["/usr/bin", "/bin"])
        profile.write_text(json.dumps(data))
    dep = checked(root, profile)["plugins"][0]["dependencies"][0]
    assert dep["status"] == ("ready" if system_path else "unknown")
    if not system_path:
        assert "/usr/bin" in dep["next_action"]
