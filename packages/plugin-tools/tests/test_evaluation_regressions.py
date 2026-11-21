"""E-1..E-7 regressions with disposable hosts, never real model credentials."""

import json
import sys

import pytest
from openstudio_si_harness.evaluation.hosts import normalize
from test_evaluation import fake_codex, fake_judge_response, judged_case, run_fixture


def host(tmp_path, body):
    command = tmp_path / "regression-host"
    command.write_text(f"""#!{sys.executable}
import json,sys,pathlib,os
if '--version' in sys.argv: print('test 2.1.309');sys.exit(0)
packages=[pathlib.Path(sys.argv[i+1]) for i,x in enumerate(sys.argv) if x=='--plugin-dir']
plugins=[{{'name':p.name,'path':str(p),'source':p.name+'@inline'}} for p in packages]
plugins.extend([{{'name':'cc-plugin-agents-md','path':'builtin'}},{{'name':'cc-plugin-telemetry','source':'cc-plugin-telemetry@builtin'}},{{'name':'cc-plugin-plugin-authoring','path':'builtin'}}])
def emit(e): print(json.dumps(e),flush=True)
emit({{'type':'system','subtype':'init','plugins':plugins,'mcp_servers':[],'tools':['Read','Glob','Grep','Skill']}})
{body}
emit({{'type':'result','result':'Observed answer.','is_error':False,'usage':{{'output_tokens':10}},'total_cost_usd':.01}})
""")
    command.chmod(0o755)
    return {
        "model": "offline",
        "command": str(command),
        "api_key_env": "SI_EVAL_AGENT_KEY",
    }


def test_real_startup_builtins_are_excluded_but_extra_plugins_are_rejected(
    tmp_path, monkeypatch
):
    agent = host(tmp_path, "")
    result, _, _ = run_fixture(
        tmp_path,
        monkeypatch,
        case_updates={"required_skills": []},
        config_updates={"agent": agent},
    )
    assert result["status"] == "passed"
    raw = "\n".join(
        map(
            json.dumps,
            [
                {
                    "type": "system",
                    "subtype": "init",
                    "plugins": [
                        {"name": "expected"},
                        {"name": "unexpected", "path": "/tmp/plugin"},
                        {"name": "builtin", "source": "builtin@builtin"},
                    ],
                },
                {"type": "result", "result": "Done"},
            ],
        )
    )
    assert normalize("claude", raw)["plugins"] == ["expected", "unexpected"]


def test_holdouts_absent_from_all_agent_views_and_progress_records(
    tmp_path, monkeypatch
):
    sentinel = "HOLDOUT_VALUE_MUST_STAY_PRIVATE"
    root = tmp_path / "run"
    body = f"""
for p in packages:
 assert not (p/'evals').exists()
 assert not (p/'openstudio-si-export.json').exists()
root=pathlib.Path({str(root)!r})
assert not list(root.rglob('reference.txt'))
for path in root.glob('sessions/*/result.json'):
 value=json.loads(path.read_text())
 assert 'judge' not in value['case'] and 'expected' not in value['case']
 assert value['holdout_pending'] and value['status']=='incomplete'
 assert {sentinel!r} not in path.read_text()
"""
    agent = host(tmp_path, body)
    spec = {
        **judged_case(),
        "required_skills": [],
        "expected": {"reference": "evals/reference/layout.md", "output": sentinel},
    }
    result, root, _ = run_fixture(
        tmp_path,
        monkeypatch,
        case_updates=spec,
        config_updates={"agent": agent, "repeat": 2},
    )
    assert result["status"] == "passed"
    assert len(result["sessions"]) == 2
    for directory in (root / "sessions").iterdir():
        assert (directory / "reference.txt").is_file()
        stored = json.loads((directory / "result.json").read_text())
        assert stored["case"]["expected"]["output"] == sentinel
        assert not stored["holdout_pending"]
        assert stored["plugins"][0]["withheld_evaluation_files"]
        assert not any(
            p.startswith("evals/")
            for p in stored["plugins"][0]["agent_view_file_hashes"]
        )


def test_attempt_to_read_holdout_fails_and_never_receives_a_judge_score(
    tmp_path, monkeypatch
):
    from openstudio_si_harness.evaluation import judge
    from openstudio_si_harness.evaluation.runner import rejudge

    agent = host(
        tmp_path,
        """
path=packages[0]/'evals/reference/layout.md'
assert not path.exists()
emit({'type':'assistant','message':{'content':[{'type':'tool_use','id':'leak','name':'Read','input':{'file_path':str(path)}}]}})
emit({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'leak','content':'Missing reference','is_error':True}]}})
""",
    )
    result, root, _ = run_fixture(
        tmp_path,
        monkeypatch,
        case_updates={
            **judged_case(),
            "required_skills": [],
            "expected": {"reference": "evals/reference/layout.md"},
        },
        config_updates={"agent": agent},
    )
    assert result["status"] == "failed"
    assert any(
        c["check"] == "holdout_access" and not c["passed"]
        for c in result["sessions"][0]["checks"]
    )
    monkeypatch.setenv("SI_TEST_JUDGE_KEY", "fake-judge-key")
    calls = []
    monkeypatch.setattr(
        judge, "invoke", lambda *args: calls.append(args) or fake_judge_response(0.9)
    )
    judged = rejudge(
        root,
        {
            "framework": "deepeval",
            "provider": "openai",
            "model": "fake",
            "api_key_env": "SI_TEST_JUDGE_KEY",
        },
    )
    assert not calls
    assert judged["status"] == "failed"
    assert judged["sessions"][0]["judge"]["status"] == "skipped"
    assert "score" not in judged["sessions"][0]["judge"]


def test_default_environment_omits_shell_secrets_and_isolates_all_homes(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-github-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-aws-secret")
    monkeypatch.setenv("SI_TEST_EXTRA", "explicit-local-value")
    agent = host(
        tmp_path,
        """
assert 'GITHUB_TOKEN' not in os.environ and 'AWS_SECRET_ACCESS_KEY' not in os.environ
assert os.environ['SI_TEST_EXTRA']=='explicit-local-value'
assert os.environ['HOME'] != str(pathlib.Path.cwd())
assert pathlib.Path(os.environ['XDG_DATA_HOME']).is_relative_to(pathlib.Path.cwd())
""",
    )
    agent["env"] = ["SI_TEST_EXTRA"]
    result, root, _ = run_fixture(
        tmp_path,
        monkeypatch,
        case_updates={"required_skills": []},
        config_updates={"agent": agent},
    )
    assert result["status"] == "passed"
    stored = json.loads(next((root / "sessions").glob("*/result.json")).read_text())
    assert "SI_TEST_EXTRA" in stored["environment_names"]
    assert not {"GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY"} & set(
        stored["environment_names"]
    )
    assert "explicit-local-value" not in json.dumps(stored)


@pytest.mark.parametrize("change", ["skill", "version"])
def test_stale_export_fails_before_host_execution(tmp_path, monkeypatch, change):
    def stale(project, source, export, config):
        if change == "skill":
            path = next((source / "skills").glob("*/SKILL.md"))
            path.write_text(path.read_text() + "\nChanged after export.\n")
        else:
            path = source / "openstudio-si-plugin.yaml"
            path.write_text(path.read_text().replace("0.1.0", "0.2.0", 1))

    with pytest.raises(ValueError, match="stale"):
        run_fixture(tmp_path, monkeypatch, before_run=stale)
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("sandbox", ["read-only", "workspace-write"])
def test_codex_sandbox_profile_is_recorded_and_default_is_read_only(
    tmp_path, monkeypatch, sandbox
):
    agent = {
        "model": "fake",
        "command": str(fake_codex(tmp_path)),
        "api_key_env": "SI_EVAL_AGENT_KEY",
    }
    if sandbox != "read-only":
        agent["sandbox"] = sandbox
    result, root, _ = run_fixture(
        tmp_path,
        monkeypatch,
        case_updates={"required_skills": ["eval-demo:eval-demo"]},
        config_updates={"host": "codex", "agent": agent},
    )
    assert result["status"] == "passed"
    row = result["sessions"][0]
    assert row["capabilities"]["sandbox"] == sandbox
    assert any(
        c.get("status") == "not_observable" and c["passed"] is None
        for c in row["checks"]
    )
    assert "Execution capabilities" in (root / "report.md").read_text()


def test_home_model_outputs_are_captured_before_cleanup(tmp_path, monkeypatch):
    agent = host(
        tmp_path,
        """
path=pathlib.Path(os.environ['HOME'])/'.local/share/example-mcp/models/final.osm'
path.parent.mkdir(parents=True,exist_ok=True)
path.write_text('OS:ThermalZone,\\n  zone-id; !- Handle\\n')
emit({'type':'assistant','message':{'content':[{'type':'tool_use','id':'model','name':'mcp__server__save','input':{}}]}})
emit({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'model','content':str(path)}]}})
""",
    )
    result, root, _ = run_fixture(
        tmp_path,
        monkeypatch,
        case_updates={
            "required_skills": [],
            "expected": {"model": {"counts": {"OS:ThermalZone": 1}}},
        },
        config_updates={"agent": agent},
    )
    assert result["status"] == "passed"
    assert next((root / "sessions").glob("*/artifacts/_host_home/**/*.osm")).is_file()


def test_uncaptured_external_model_location_is_blocking(tmp_path, monkeypatch):
    agent = host(
        tmp_path,
        """
emit({'type':'assistant','message':{'content':[{'type':'tool_use','id':'model','name':'mcp__server__save','input':{}}]}})
emit({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'model','content':'/outside-capture/model.osm'}]}})
""",
    )
    result, _, _ = run_fixture(
        tmp_path,
        monkeypatch,
        case_updates={"required_skills": []},
        config_updates={"agent": agent},
    )
    assert result["status"] == "incomplete"
    assert any(
        g.startswith("artifact_outside_capture_roots:")
        for g in result["sessions"][0]["gaps"]
    )


def test_binary_metadata_is_a_note_unless_required_by_a_metric(tmp_path, monkeypatch):
    from openstudio_si_harness.evaluation import judge
    from openstudio_si_harness.evaluation.runner import rejudge

    agent = host(
        tmp_path,
        "pathlib.Path('eplusout.sql').write_bytes(b'\\x00SQLite test fixture')",
    )
    spec = {**judged_case(), "required_skills": []}
    _result, root, _ = run_fixture(
        tmp_path, monkeypatch, case_updates=spec, config_updates={"agent": agent}
    )
    monkeypatch.setenv("SI_JUDGE_KEY", "fake-local-key")
    calls = []
    monkeypatch.setattr(
        judge, "invoke", lambda *args: calls.append(args) or fake_judge_response(0.9)
    )
    settings = {
        "framework": "deepeval",
        "provider": "openai",
        "model": "fake",
        "api_key_env": "SI_JUDGE_KEY",
    }
    rendered = rejudge(root, settings)
    assert rendered["status"] == "passed" and calls
    assert rendered["sessions"][0]["judge"]["notes"]
    from openstudio_si_harness.evaluation.contracts import digest, json_hash

    session = next((root / "sessions").iterdir())
    path = session / "result.json"
    captured = json.loads(path.read_text())
    metric = captured["case"]["judge"]["metrics"][0]
    metric["evaluation_params"].append("context")
    metric["required_artifacts"] = ["*.sql"]
    captured["case_hash"] = json_hash(captured["case"])
    path.write_text(json.dumps(captured))
    metadata = json.loads((root / "run.json").read_text())
    metadata["result_hashes"][captured["session_id"]] = digest(path.read_bytes())
    (root / "run.json").write_text(json.dumps(metadata))
    calls.clear()
    unavailable = rejudge(root, settings)
    assert unavailable["status"] == "incomplete" and not calls
    assert any(
        "required_judge_artifact_unavailable" in g
        for g in unavailable["sessions"][0]["judge"]["evidence_gaps"]
    )


def test_mcp_declared_environment_and_launcher_override_are_the_only_extra_inheritance(
    tmp_path, monkeypatch
):
    from openstudio_si_harness.evaluation.hosts import capture
    from openstudio_si_harness.mcp import host_servers

    package = tmp_path / "plugin"
    package.mkdir()
    declaration = {
        "server": {
            "command": "example-server",
            "env": {"TOKEN": "${SI_DECLARED_TOKEN}"},
        }
    }
    (package / ".mcp.json").write_text(
        json.dumps({"mcpServers": host_servers(declaration)})
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    agent = host(tmp_path, "")
    monkeypatch.setenv("SI_EVAL_AGENT_KEY", "fake-agent-key")
    monkeypatch.setenv("SI_DECLARED_TOKEN", "fake-declared-token")
    monkeypatch.setenv("OPENSTUDIO_SI_SERVER_COMMAND", "/tmp/explicit-server")
    monkeypatch.setenv("UNRELATED_TOKEN", "must-not-pass")
    prepared = capture(
        {"host": "claude", "agent": agent}, workspace, [package], private, 30
    )
    assert prepared["env"]["SI_DECLARED_TOKEN"] == "fake-declared-token"
    assert prepared["env"]["OPENSTUDIO_SI_SERVER_COMMAND"] == "/tmp/explicit-server"
    assert "UNRELATED_TOKEN" not in prepared["env"]
    settings = json.loads(
        prepared["command"][prepared["command"].index("--settings") + 1]
    )
    assert settings["permissions"]["blockReadsOutsideWorkingDirectories"]
    assert str(package) in prepared["command"]
    assert "--bare" not in prepared["command"]
    assert (
        prepared["command"][prepared["command"].index("--tools") + 1]
        == "Glob,Grep,Read,Skill"
    )


def test_observation_notes_do_not_mask_real_failures(tmp_path, monkeypatch):
    agent = host(
        tmp_path, "pathlib.Path('credentials.json').write_text('dummy-fixture')"
    )
    result, _, _ = run_fixture(
        tmp_path,
        monkeypatch,
        case_updates={
            "required_skills": [],
            "expected": {
                "metrics": [{"name": "metric", "value": 10, "tolerance_pct": 1}]
            },
        },
        config_updates={"agent": agent},
    )
    assert result["status"] == "failed"
    row = result["sessions"][0]
    assert not row["gaps"]
    assert any(n.startswith("credential_file_omitted:") for n in row["notes"])
    assert "answer_reported_metrics_are_not_artifact_verified" in row["notes"]


def test_known_codex_web_search_is_an_observed_tool_not_a_protocol_gap():
    raw = "\n".join(
        map(
            json.dumps,
            [
                {
                    "type": "item.completed",
                    "item": {
                        "type": "web_search",
                        "action": {"query": "public question"},
                    },
                },
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Done"},
                },
                {"type": "turn.completed"},
            ],
        )
    )
    assert not normalize("codex", raw)["gaps"]
    assert normalize("codex", raw)["trace"][0]["name"] == "web_search"


def test_external_model_path_audit_preserves_spaces(tmp_path):
    from openstudio_si_harness.evaluation.isolation import external_model_paths

    path = "/outside capture/model with spaces.osm"
    trace = [{"provider": "server", "result": {"model_path": path}}]
    assert external_model_paths(trace, tmp_path / "work", tmp_path / "home") == [path]


def test_custom_case_directory_and_reference_outside_evals_are_withheld(
    tmp_path, monkeypatch
):
    from openstudio_si_harness.export import export_plugin

    agent = host(
        tmp_path, "assert not (packages[0]/'assets/custom-answer.md').exists()"
    )

    def configure(project, source, export, config_path):
        (source / "assets").mkdir(exist_ok=True)
        (source / "assets/custom-answer.md").write_text("PRIVATE_CUSTOM_REFERENCE")
        fresh = export_plugin(source, tmp_path / "fresh-export")
        config = json.loads(config_path.read_text())
        config["plugins"] = [fresh["packages"]["claude"]]
        config_path.write_text(json.dumps(config))
        custom = project / "manual-cases"
        custom.mkdir()
        spec = {
            "schema_version": "0.1",
            "case_id": "custom",
            "description": "Custom case",
            "prompt": "Answer the public question",
            "inputs": [],
            "expected": {"reference": "assets/custom-answer.md"},
        }
        (custom / "custom.json").write_text(json.dumps(spec))
        return custom

    result, root, _ = run_fixture(
        tmp_path,
        monkeypatch,
        case_updates={"required_skills": []},
        config_updates={"agent": agent},
        before_run=configure,
    )
    assert result["status"] == "passed"
    stored = json.loads(next((root / "sessions").glob("*/result.json")).read_text())
    assert (
        "assets/custom-answer.md" in stored["plugins"][0]["withheld_evaluation_files"]
    )
    assert (
        next((root / "sessions").glob("*/reference.txt")).read_text()
        == "PRIVATE_CUSTOM_REFERENCE"
    )


def test_missing_native_tools_are_blocking_and_observed_profile_is_retained(
    tmp_path, monkeypatch
):
    agent = host(
        tmp_path,
        "emit({'type':'system','subtype':'init','plugins':plugins,'mcp_servers':[],'tools':['Read']})",
    )
    result, _, _ = run_fixture(
        tmp_path,
        monkeypatch,
        case_updates={"required_skills": []},
        config_updates={"agent": agent},
    )
    assert result["status"] == "incomplete"
    assert "host_tool_profile_missing_requested_tools" in result["sessions"][0]["gaps"]
    assert result["sessions"][0]["capabilities"]["observed_host_tools"] == ["Read"]
