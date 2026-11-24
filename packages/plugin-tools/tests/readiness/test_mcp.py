"""Native SDK MCP probes are explicit, host-scoped and supervised."""

import json
import time

import pytest
import yaml
from openstudio_si_harness.export import export_plugin
from openstudio_si_harness.mcp import override_variable
from openstudio_si_harness.readiness import doctor

from ..support.plugins import capability, dependency, plugin
from ..support.runtime import profile


def setup_service(
    tmp_path,
    selected_python,
    *,
    probe="mcp-tools",
    mode="normal",
    allow=True,
    required_tools=None,
    env=None,
    version_spec=None,
    limits=None,
):
    dep = dependency(
        "service", kind="mcp-server", target="standards_service", probe=probe
    )
    if version_spec:
        dep["version_spec"] = version_spec
    root = plugin(tmp_path, "consumer", [dep], [capability("lookup", ["service"])])
    declaration = {"command": "test-mcp-service", "args": []}
    (root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"standards_service": declaration}})
    )
    path = profile(tmp_path, selected_python, [dep], env=env, limits=limits)
    data = json.loads(path.read_text())
    binding = data["bindings"][0]
    if allow:
        binding["allow_mcp_initialize"] = True
    if required_tools is not None:
        binding["required_tools"] = required_tools
    path.write_text(json.dumps(data))
    log = tmp_path / "methods.jsonl"
    forbidden = tmp_path / "tool-was-called"
    background = tmp_path / "background-ran"
    executable = tmp_path / "bin/test-mcp-service"
    executable.write_text(f"""#!{selected_python}
import json,sys,pathlib,os,time,subprocess
mode={mode!r}
log=pathlib.Path({str(log)!r})
if mode=='background':
 child="import time; from pathlib import Path; time.sleep(1); Path({str(background)!r}).touch()"
 subprocess.Popen([sys.executable,'-c',child],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
for line in sys.stdin:
 request=json.loads(line); method=request['method']
 with log.open('a') as f: f.write(json.dumps({{'method':method,'cwd':os.getcwd(),'ambient_present':'SI_D3_AMBIENT' in os.environ,'declared_token_present':bool(os.environ.get('MCP_TOKEN'))}})+'\\n')
 if method=='tools/call':
  pathlib.Path({str(forbidden)!r}).touch()
 if 'id' not in request: continue
 if method=='initialize':
  if mode=='hang': time.sleep(30)
  if mode=='flood':
   os.write(1,b'PRIVATE_SERVER_OUTPUT'*10000); time.sleep(30)
  if mode=='invalid':
   print('not-json',flush=True); continue
  info={{'name':os.environ.get('MCP_TOKEN','standards-fixture') if mode=='echo-secret' else 'standards-fixture','version':'1.2.3'}}
  result={{'protocolVersion':request['params']['protocolVersion'],'capabilities':{{}} if mode=='no-tools' else {{'tools':{{}}}},'serverInfo':info,'instructions':'UNTRUSTED SERVER INSTRUCTIONS MUST NOT BE REPORTED'}}
 elif method=='tools/list':
  cursor=request.get('params',{{}}).get('cursor')
  names=['query_standards'] if not cursor else ['dataset_info']
  result={{'tools':[{{'name':name,'description':'untrusted description','inputSchema':{{'type':'object'}}}} for name in names]}}
  if mode=='pages' and not cursor: result['nextCursor']='second'
  if mode=='cursor-loop': result['nextCursor']='repeat'
 else: result={{}}
 print(json.dumps({{'jsonrpc':'2.0','id':request['id'],'result':result}}),flush=True)
""")
    executable.chmod(0o755)
    return root, path, log, forbidden, background


def checked(root, path, **kwargs):
    return doctor([root], runtime_profile=path, check_runtime=True, **kwargs)


@pytest.mark.parametrize("kind", ["mcp-initialize", "mcp-tools"])
def test_native_mcp_probe_does_not_call_tools_or_claim_host_connection(
    tmp_path, selected_python, kind
):
    root, path, log, forbidden, _ = setup_service(tmp_path, selected_python, probe=kind)
    report = checked(root, path)
    entry = report["plugins"][0]
    dep = entry["dependencies"][0]
    assert report["status"] == "ready", dep
    assert dep["reason"] == (
        "mcp_initialized" if kind == "mcp-initialize" else "mcp_tools_available"
    )
    assert dep["evidence"]["probe_connection"] == "initialized_then_closed"
    assert dep["evidence"]["server_version"] == "1.2.3"
    assert entry["host_enabled"] == entry["service_connected"] == "unknown"
    methods = [json.loads(line)["method"] for line in log.read_text().splitlines()]
    assert "initialize" in methods and "notifications/initialized" in methods
    assert ("tools/list" in methods) is (kind == "mcp-tools")
    assert not forbidden.exists()
    assert "UNTRUSTED SERVER INSTRUCTIONS" not in json.dumps(report)
    assert "untrusted description" not in json.dumps(report)


@pytest.mark.parametrize("passive", [True, False])
def test_mcp_startup_requires_both_explicit_permissions(
    tmp_path, selected_python, passive
):
    root, path, log, _, _ = setup_service(tmp_path, selected_python, allow=passive)
    report = doctor([root], runtime_profile=path, check_runtime=not passive)
    assert report["status"] == "unknown" and not log.exists()
    assert report["runtime_selection"]["probes_executed"] == 0
    assert report["plugins"][0]["dependencies"][0]["reason"] == (
        "runtime_check_not_requested"
        if passive
        else "mcp_initialization_not_authorized"
    )


@pytest.mark.parametrize(
    "required,status",
    [(["query_standards", "dataset_info"], "ready"), (["missing_tool"], "unavailable")],
)
def test_paginated_tools_and_missing_requirement(
    tmp_path, selected_python, required, status
):
    root, path, log, forbidden, _ = setup_service(
        tmp_path, selected_python, mode="pages", required_tools=required
    )
    report = checked(root, path)
    dep = report["plugins"][0]["dependencies"][0]
    assert report["status"] == status, dep
    assert dep["evidence"]["tools"] == ["query_standards", "dataset_info"]
    assert (
        sum(
            json.loads(line)["method"] == "tools/list"
            for line in log.read_text().splitlines()
        )
        == 2
    )
    assert not forbidden.exists()


@pytest.mark.parametrize(
    "kind,status", [("mcp-initialize", "ready"), ("mcp-tools", "unavailable")]
)
def test_tools_capability_is_required_only_for_tools_probe(
    tmp_path, selected_python, kind, status
):
    root, path, _, _, _ = setup_service(
        tmp_path, selected_python, mode="no-tools", probe=kind
    )
    assert checked(root, path)["status"] == status


@pytest.mark.parametrize(
    "mode,reasons",
    [
        ("invalid", {"mcp_protocol_error"}),
        ("cursor-loop", {"mcp_pagination_limit"}),
        ("hang", {"mcp_timeout", "runtime_timeout"}),
        ("flood", {"mcp_output_limit", "runtime_output_limit"}),
    ],
)
def test_bad_or_unbounded_servers_are_unknown(tmp_path, selected_python, mode, reasons):
    root, path, _, forbidden, _ = setup_service(
        tmp_path,
        selected_python,
        mode=mode,
        limits={"timeout_seconds": 2, "max_output_bytes": 1024},
    )
    start = time.monotonic()
    report = checked(root, path)
    assert report["status"] == "unknown"
    assert report["plugins"][0]["dependencies"][0]["reason"] in reasons
    assert time.monotonic() - start < 5
    assert "PRIVATE_SERVER_OUTPUT" not in json.dumps(report)
    assert not forbidden.exists()


@pytest.mark.parametrize(
    "spec,status", [(">=1.0.0 <2.0.0", "ready"), (">=2.0.0", "unavailable")]
)
def test_mcp_server_version_range(tmp_path, selected_python, spec, status):
    root, path, _, _, _ = setup_service(tmp_path, selected_python, version_spec=spec)
    assert checked(root, path)["status"] == status


def test_profile_only_environment_and_source_references(
    tmp_path, selected_python, monkeypatch
):
    monkeypatch.setenv("SI_D3_AMBIENT", "ambient-must-not-leak")
    monkeypatch.setenv("SI_D3_TOKEN", "explicit-service-token")
    root, path, log, _, _ = setup_service(
        tmp_path, selected_python, env={"SERVICE_SOURCE": "SI_D3_TOKEN"}
    )
    config = json.loads((root / ".mcp.json").read_text())
    config["mcpServers"]["standards_service"]["env"] = {
        "MCP_TOKEN": "${SERVICE_SOURCE}"
    }
    (root / ".mcp.json").write_text(json.dumps(config))
    report = checked(root, path)
    assert report["status"] == "ready"
    observations = [json.loads(line) for line in log.read_text().splitlines()]
    assert all(
        not row["ambient_present"] and row["declared_token_present"]
        for row in observations
    )
    assert all(row["cwd"] == str(tmp_path) for row in observations)
    assert "explicit-service-token" not in json.dumps(report)


def test_missing_mcp_environment_prevents_startup(tmp_path, selected_python):
    root, path, log, _, _ = setup_service(tmp_path, selected_python)
    config = json.loads((root / ".mcp.json").read_text())
    config["mcpServers"]["standards_service"]["env"] = {
        "MCP_TOKEN": "${SI_D3_UNSET_IN_PROFILE}"
    }
    (root / ".mcp.json").write_text(json.dumps(config))
    report = checked(root, path)
    assert report["status"] == "unavailable" and not log.exists()
    assert report["runtime_selection"]["probes_executed"] == 0


@pytest.mark.parametrize("secret", ["explicit-service-token", 'explicit\\secret"value'])
def test_untrusted_metadata_cannot_echo_forwarded_values(
    tmp_path, selected_python, monkeypatch, secret
):
    monkeypatch.setenv("SI_D3_TOKEN", secret)
    root, path, _, _, _ = setup_service(
        tmp_path, selected_python, mode="echo-secret", env={"MCP_TOKEN": "SI_D3_TOKEN"}
    )
    report = checked(root, path)
    assert report["status"] == "unknown"
    assert (
        report["plugins"][0]["dependencies"][0]["reason"]
        == "mcp_response_contains_environment_value"
    )
    assert "explicit-service-token" not in json.dumps(report)


def test_absolute_desktop_override_and_non_python_runtime(
    tmp_path, selected_python, monkeypatch
):
    root, path, _, _, _ = setup_service(tmp_path, selected_python)
    command = tmp_path / "bin/test-mcp-service"
    moved = tmp_path / "outside-path-service"
    command.rename(moved)
    monkeypatch.setenv("SI_D3_OVERRIDE", str(moved))
    data = json.loads(path.read_text())
    data["runtimes"]["selected"].pop("python")
    data["runtimes"]["selected"]["path"] = []
    data["runtimes"]["selected"]["env"] = {
        override_variable("standards_service"): "SI_D3_OVERRIDE"
    }
    path.write_text(json.dumps(data))
    assert checked(root, path)["status"] == "ready"


def test_success_cleans_background_server_children(tmp_path, selected_python):
    root, path, _, _, background = setup_service(
        tmp_path, selected_python, mode="background"
    )
    assert checked(root, path)["status"] == "ready"
    time.sleep(1.1)
    assert not background.exists()


@pytest.mark.parametrize("provider_host", ["codex", "claude"])
def test_external_provider_declaration_is_used_in_same_host(
    tmp_path, selected_python, provider_host
):
    root, path, log, _, _ = setup_service(tmp_path, selected_python)
    provider = plugin(tmp_path, "provider")
    (provider / ".mcp.json").write_text((root / ".mcp.json").read_text())
    (root / ".mcp.json").unlink()
    manifest = root / "openstudio-si-plugin.yaml"
    data = yaml.safe_load(manifest.read_text())
    data["dependencies"].insert(0, dependency())
    data["dependencies"][1]["provider"] = "provider"
    manifest.write_text(yaml.safe_dump(data))
    c = export_plugin(root, tmp_path / "consumer-export", ("codex",))["packages"][
        "codex"
    ]
    p = export_plugin(provider, tmp_path / "provider-export", (provider_host,))[
        "packages"
    ][provider_host]
    report = doctor(
        [c, p], plugin_id="consumer", runtime_profile=path, check_runtime=True
    )
    assert report["status"] == ("ready" if provider_host == "codex" else "unavailable")
    if provider_host == "codex":
        assert (
            report["plugins"][0]["dependencies"][1]["evidence"]["provider"]
            == "provider"
        )
    else:
        assert not log.exists()
        assert report["runtime_selection"]["probes_executed"] == 0
