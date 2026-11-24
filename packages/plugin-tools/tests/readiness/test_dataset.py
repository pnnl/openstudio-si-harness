"""Dataset identity requires an explicitly trusted selected command."""

import json
import time
from pathlib import Path

import pytest
from openstudio_si_harness.export import export_plugin
from openstudio_si_harness.readiness import doctor

from ..support.plugins import capability, dependency, plugin
from ..support.runtime import profile


def metadata():
    return {
        "schema_version": "0.1",
        "dataset": "building_energy_standards",
        "version": None,
        "package": {"name": "building-energy-standards-data", "version": "0.3.3"},
        "packaged_data": {
            "algorithm": "sha256-path-content-v1",
            "sha256": "a" * 64,
            "files": 169,
        },
        "cache": {
            "selection": "environment",
            "state": "ready",
            "sha256": "b" * 64,
            "source_data_sha256": "a" * 64,
        },
    }


def setup_dataset(
    tmp_path, selected_python, *, payload=None, allow=True, mode="normal"
):
    command = dependency(
        "besd-command", kind="command", target="besd-mcp", required=False
    )
    dataset = dependency(
        "data",
        kind="dataset",
        target="building_energy_standards",
        probe="dataset-metadata",
    )
    root = plugin(
        tmp_path, "consumer", [command, dataset], [capability("lookup", ["data"])]
    )
    path = profile(tmp_path, selected_python, [dataset])
    data = json.loads(path.read_text())
    del data["runtimes"]["selected"]["python"]
    data["bindings"][0].update(
        dataset_command="besd-command", allow_dataset_metadata=allow
    )
    path.write_text(json.dumps(data))
    executable = tmp_path / "bin/besd-mcp"
    marker = tmp_path / "called.json"
    payload = metadata() if payload is None else payload
    executable.write_text(f"""#!{selected_python}
import json,sys,os,time,subprocess
from pathlib import Path
Path({str(marker)!r}).write_text(json.dumps({{'args':sys.argv[1:],'ambient':'SI_D4_AMBIENT' in os.environ}}))
if {mode!r}=='hang': time.sleep(30)
if {mode!r}=='flood': print('PRIVATE'*100000); sys.exit(0)
if {mode!r}=='invalid': print('PRIVATE invalid json'); sys.exit(0)
if {mode!r}=='background':
 subprocess.Popen([sys.executable,'-c',"import time; from pathlib import Path; time.sleep(1); Path({str(tmp_path / 'escaped')!r}).touch()"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
print({json.dumps(payload)!r})
""")
    executable.chmod(0o755)
    return root, path, marker


def check(root, path):
    report = doctor([root], runtime_profile=path, check_runtime=True)
    dependency = next(
        d for d in report["plugins"][0]["dependencies"] if d["id"] == "data"
    )
    return report, dependency


def test_metadata_operation_is_separate_from_mcp(
    tmp_path, selected_python, monkeypatch
):
    monkeypatch.setenv("SI_D4_AMBIENT", "not forwarded")
    root, path, marker = setup_dataset(tmp_path, selected_python)
    report, dep = check(root, path)
    assert report["status"] == "ready", dep
    assert dep["reason"] == "dataset_identity_observed"
    assert dep["evidence"]["dataset_metadata"] == metadata()
    assert dep["evidence"]["measurement_scope"] == "selected_runtime"
    assert json.loads(marker.read_text()) == {
        "args": ["--si-dataset-metadata"],
        "ambient": False,
    }
    entry = report["plugins"][0]
    assert entry["host_enabled"] == entry["service_connected"] == "unknown"


@pytest.mark.parametrize("passive", [True, False])
def test_both_authorizations_required(tmp_path, selected_python, passive):
    root, path, marker = setup_dataset(tmp_path, selected_python, allow=passive)
    report = doctor([root], runtime_profile=path, check_runtime=not passive)
    assert report["status"] == "unknown"
    assert report["runtime_selection"]["probes_executed"] == 0
    assert not marker.exists()


@pytest.mark.parametrize(
    "state,status",
    [
        ("missing", "unknown"),
        ("invalid", "unavailable"),
        ("changing", "unknown"),
        ("unreadable", "unknown"),
        ("wal_present", "unknown"),
    ],
)
def test_nonready_cache_states(tmp_path, selected_python, state, status):
    payload = metadata()
    payload["cache"].update(state=state, sha256=None)
    root, path, _ = setup_dataset(tmp_path, selected_python, payload=payload)
    _, dep = check(root, path)
    assert dep["status"] == status and dep["reason"] == "dataset_cache_" + state


@pytest.mark.parametrize(
    "bad", ["schema", "target", "digest", "ready_without_hash", "extra"]
)
def test_invalid_responses_never_become_ready(tmp_path, selected_python, bad):
    payload = metadata()
    if bad == "schema":
        del payload["package"]
    elif bad == "target":
        payload["dataset"] = "wrong"
    elif bad == "digest":
        payload["packaged_data"]["sha256"] = "not a hash"
    elif bad == "ready_without_hash":
        payload["cache"]["sha256"] = None
    else:
        payload["PRIVATE"] = "do not report"
    root, path, _ = setup_dataset(tmp_path, selected_python, payload=payload)
    report, dep = check(root, path)
    assert dep["status"] == "unknown"
    assert "PRIVATE" not in json.dumps(report)


@pytest.mark.parametrize(
    "version,status", [(None, "unknown"), ("1.2.3", "ready"), ("2.0.0", "unavailable")]
)
def test_dataset_version_is_not_package_version(
    tmp_path, selected_python, version, status
):
    import yaml

    payload = metadata()
    payload["version"] = version
    root, path, _ = setup_dataset(tmp_path, selected_python, payload=payload)
    manifest_path = root / "openstudio-si-plugin.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["dependencies"][1]["version_spec"] = ">=1.0.0 <2.0.0"
    manifest_path.write_text(yaml.safe_dump(manifest))
    _, dep = check(root, path)
    assert dep["status"] == status


@pytest.mark.parametrize("mode", ["hang", "flood", "invalid", "background"])
def test_bounded_provider_and_child_cleanup(tmp_path, selected_python, mode):
    root, path, _ = setup_dataset(tmp_path, selected_python, mode=mode)
    data = json.loads(path.read_text())
    data["limits"] = {"timeout_seconds": 0.5, "max_output_bytes": 2048}
    path.write_text(json.dumps(data))
    report, dep = check(root, path)
    assert dep["status"] == ("ready" if mode == "background" else "unknown")
    assert "PRIVATE" not in json.dumps(report)
    time.sleep(1.1)
    assert not (tmp_path / "escaped").exists()


def test_missing_command_and_invalid_binding(tmp_path, selected_python):
    root, path, marker = setup_dataset(tmp_path, selected_python)
    (tmp_path / "bin/besd-mcp").unlink()
    _, dep = check(root, path)
    assert dep["reason"] == "dataset_command_not_found"
    data = json.loads(path.read_text())
    data["bindings"][0]["dataset_command"] = "missing-dependency"
    path.write_text(json.dumps(data))
    report, dep = check(root, path)
    assert dep["status"] == "unknown"
    assert (
        report["runtime_selection"]["unused_bindings"][0]["reason"]
        == "dataset_command_dependency_missing"
    )
    assert not marker.exists()


def test_only_selected_capability_is_probed(tmp_path, selected_python):
    import yaml

    root, path, marker = setup_dataset(tmp_path, selected_python)
    manifest_path = root / "openstudio-si-plugin.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["dependencies"][1]["required"] = False
    manifest["capabilities"].append(capability("without-data"))
    manifest_path.write_text(yaml.safe_dump(manifest))
    report = doctor(
        [root],
        runtime_profile=path,
        check_runtime=True,
        plugin_id="consumer",
        capability="without-data",
    )
    assert report["status"] == "ready" and not marker.exists()
    assert report["runtime_selection"]["probes_executed"] == 0


def test_forwarded_secret_is_not_reported(tmp_path, selected_python, monkeypatch):
    payload = metadata()
    payload["package"]["name"] = "fake-secret-only-for-test"
    monkeypatch.setenv("SI_D4_TOKEN", payload["package"]["name"])
    root, path, _ = setup_dataset(tmp_path, selected_python, payload=payload)
    data = json.loads(path.read_text())
    data["runtimes"]["selected"]["env"] = {"TOKEN": "SI_D4_TOKEN"}
    path.write_text(json.dumps(data))
    report, dep = check(root, path)
    assert dep["reason"] == "dataset_response_contains_environment_value"
    assert payload["package"]["name"] not in json.dumps(report)


def test_numeric_environment_value_in_digest_is_not_a_secret_echo(
    tmp_path, selected_python, monkeypatch
):
    payload = metadata()
    payload["packaged_data"]["sha256"] = "8080" + "a" * 60
    payload["cache"]["source_data_sha256"] = payload["packaged_data"]["sha256"]
    payload["cache"]["sha256"] = "b" * 60 + "8080"
    monkeypatch.setenv("SI_D4_PORT", "8080")
    root, path, _ = setup_dataset(tmp_path, selected_python, payload=payload)
    data = json.loads(path.read_text())
    data["runtimes"]["selected"]["env"] = {"PORT": "SI_D4_PORT"}
    path.write_text(json.dumps(data))
    _, dep = check(root, path)
    assert dep["status"] == "ready"


@pytest.mark.parametrize(
    "source,status,reason",
    [
        (None, "unknown", "dataset_cache_provenance_unknown"),
        ("c" * 64, "unavailable", "dataset_cache_stale"),
    ],
)
def test_cache_build_provenance_must_match_packaged_json(
    tmp_path, selected_python, source, status, reason
):
    payload = metadata()
    payload["cache"]["source_data_sha256"] = source
    root, path, _ = setup_dataset(tmp_path, selected_python, payload=payload)
    _, dep = check(root, path)
    assert dep["status"] == status and dep["reason"] == reason


def link_service(root, path, *, env=None, args=None):
    import yaml

    manifest_path = root / "openstudio-si-plugin.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["dependencies"].append(
        dependency(
            "service",
            kind="mcp-server",
            target="standards_service",
            probe="mcp-tools",
            required=False,
        )
    )
    manifest_path.write_text(yaml.safe_dump(manifest))
    (root / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "standards_service": {
                        "command": "besd-mcp",
                        "args": args or [],
                        "env": env or {},
                    }
                }
            }
        )
    )
    data = json.loads(path.read_text())
    data["bindings"][0].pop("dataset_command")
    data["bindings"][0]["dataset_mcp"] = "service"
    path.write_text(json.dumps(data))


@pytest.mark.parametrize("host", [None, "claude", "codex"])
def test_linked_server_uses_same_override_arguments_and_ordered_environment(
    tmp_path, selected_python, monkeypatch, host
):
    root, path, original_marker = setup_dataset(tmp_path, selected_python)
    link_service(
        root,
        path,
        args=["--db-path", "selected.db"],
        env={"FIRST_DB": "${SOURCE_DB}", "BESD_DB_PATH": "${FIRST_DB}"},
    )
    override = tmp_path / "provider with spaces"
    log = tmp_path / "override-called.json"
    override.write_text(f"""#!{selected_python}
import json,sys,os
from pathlib import Path
Path({str(log)!r}).write_text(json.dumps({{'args':sys.argv[1:],'db':os.environ.get('BESD_DB_PATH')}}))
print({json.dumps(metadata())!r})
""")
    override.chmod(0o755)
    monkeypatch.setenv("SI_D4_DB", "existing-selected-database")
    monkeypatch.setenv("SI_D4_EXECUTABLE", str(override))
    data = json.loads(path.read_text())
    data["runtimes"]["selected"]["env"] = {
        "SOURCE_DB": "SI_D4_DB",
        "OPENSTUDIO_SI_STANDARDS_SERVICE_COMMAND": "SI_D4_EXECUTABLE",
    }
    if host:
        data["host"] = host
        root = Path(export_plugin(root, tmp_path / "export", (host,))["packages"][host])
    path.write_text(json.dumps(data))
    _, dep = check(root, path)
    assert dep["status"] == "ready", dep
    assert not original_marker.exists()
    assert json.loads(log.read_text()) == {
        "args": ["--db-path", "selected.db", "--si-dataset-metadata"],
        "db": "existing-selected-database",
    }
    assert dep["evidence"]["dataset_executable"] == str(override)


def test_linked_server_missing_environment_stops_before_launch(
    tmp_path, selected_python
):
    root, path, marker = setup_dataset(tmp_path, selected_python)
    link_service(root, path, env={"BESD_DB_PATH": "${SI_D4_UNFORWARDED_DB}"})
    _, dep = check(root, path)
    assert dep["reason"] == "mcp_environment_missing"
    assert not marker.exists()


def test_linked_server_cannot_use_a_different_bound_runtime(tmp_path, selected_python):
    root, path, marker = setup_dataset(tmp_path, selected_python)
    link_service(root, path)
    data = json.loads(path.read_text())
    data["runtimes"]["other"] = dict(data["runtimes"]["selected"])
    data["bindings"].append(
        {"plugin": "consumer", "dependency": "service", "runtime": "other"}
    )
    path.write_text(json.dumps(data))
    report, dep = check(root, path)
    assert dep["status"] == "unknown"
    assert (
        report["runtime_selection"]["unused_bindings"][0]["reason"]
        == "dataset_mcp_runtime_mismatch"
    )
    assert not marker.exists()


@pytest.mark.parametrize("dataset_first", [True, False])
def test_startup_created_cache_never_marks_dataset_unavailable(
    tmp_path, selected_python, dataset_first
):
    import yaml

    root, path, _ = setup_dataset(tmp_path, selected_python)
    link_service(root, path)
    manifest_path = root / "openstudio-si-plugin.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    deps = {dep["id"]: dep for dep in manifest["dependencies"]}
    manifest["dependencies"] = [
        deps["besd-command"],
        *(
            [deps["data"], deps["service"]]
            if dataset_first
            else [deps["service"], deps["data"]]
        ),
    ]
    manifest["capabilities"][0]["requires"] = ["data", "service"]
    manifest_path.write_text(yaml.safe_dump(manifest))
    data = json.loads(path.read_text())
    data["bindings"].append(
        {
            "plugin": "consumer",
            "dependency": "service",
            "runtime": "selected",
            "allow_mcp_initialize": True,
        }
    )
    path.write_text(json.dumps(data))
    cache = tmp_path / "created-by-initialize"
    executable = tmp_path / "bin/besd-mcp"
    executable.write_text(f"""#!{selected_python}
import json,sys
from pathlib import Path
cache=Path({str(cache)!r})
if '--si-dataset-metadata' in sys.argv:
 result=json.loads({json.dumps(metadata())!r})
 if not cache.exists(): result['cache'].update(state='missing',sha256=None)
 print(json.dumps(result)); sys.exit(0)
for line in sys.stdin:
 request=json.loads(line)
 if 'id' not in request: continue
 if request['method']=='initialize':
  cache.touch()
  result={{'protocolVersion':request['params']['protocolVersion'],'capabilities':{{'tools':{{}}}},'serverInfo':{{'name':'besd-fixture','version':'1.0.0'}}}}
 else: result={{'tools':[]}}
 print(json.dumps({{'jsonrpc':'2.0','id':request['id'],'result':result}}),flush=True)
""")
    report, dep = check(root, path)
    assert cache.exists()
    assert dep["status"] == ("unknown" if dataset_first else "ready"), dep
    assert report["status"] != "unavailable"
