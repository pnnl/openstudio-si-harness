"""Local evaluation contracts, host traces and advisory scoring without paid models."""

import json

import pytest
from openstudio_si_harness.evaluation.contracts import inside, load_cases, load_config


def write_case(root, **updates):
    cases = root / "evals/cases"
    cases.mkdir(parents=True, exist_ok=True)
    case = {
        "schema_version": "0.1",
        "case_id": "example",
        "description": "Example",
        "prompt": "Explain.",
        "inputs": [],
        **updates,
    }
    (cases / "example.json").write_text(json.dumps(case))
    return cases


def test_legacy_cases_and_optional_rubric(tmp_path):
    cases = write_case(tmp_path)
    path = cases / "example.json"
    case = json.loads(path.read_text())
    del case["schema_version"]
    path.write_text(json.dumps([case]))
    (record,) = load_cases(cases, tmp_path)
    assert record["legacy"] and record["spec"]["schema_version"] == "0.1"
    assert len(record["hash"]) == 64


def test_missing_reference_and_duplicate_cases_fail(tmp_path):
    cases = write_case(tmp_path, expected={"reference": "../outside"})
    with pytest.raises(ValueError):
        load_cases(cases, tmp_path)
    write_case(tmp_path)
    (cases / "duplicate.json").write_text((cases / "example.json").read_text())
    with pytest.raises(ValueError, match="Duplicate"):
        load_cases(cases, tmp_path)


def test_evidence_symlink_rejected(tmp_path):
    (tmp_path / "real").write_text("data")
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(ValueError, match="symlink"):
        inside(tmp_path, "link")


def test_unknown_cost_ceiling_and_credential_urls_rejected(tmp_path):
    path = tmp_path / "run.json"
    config = {
        "schema_version": "0.1",
        "host": "codex",
        "agent": {"model": "chosen"},
        "plugins": ["somewhere"],
        "limits": {"max_cost_usd": 1},
    }
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="cannot enforce"):
        load_config(path)
    config["limits"] = {}
    config["agent"]["base_url"] = "https://user:secret@example.com"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="credentials"):
        load_config(path)


def test_trace_preserves_qualified_skills_and_pairs_results():
    from openstudio_si_harness.evaluation.hosts import normalize

    rows = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "a",
                        "name": "Skill",
                        "input": {"skill": "lab-one:lookup"},
                    },
                    {
                        "type": "tool_use",
                        "id": "b",
                        "name": "mcp__plugin_lab-one_standards__query",
                        "input": {"table": "x"},
                    },
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "a", "content": "Loaded"},
                    {"type": "tool_result", "tool_use_id": "b", "content": "Value"},
                ]
            },
        },
        {
            "type": "result",
            "result": "Done",
            "usage": {"output_tokens": 12},
            "is_error": False,
        },
    ]
    result = normalize("claude", "\n".join(map(json.dumps, rows)))
    assert result["trace"][0]["arguments"]["skill"] == "lab-one:lookup"
    assert result["trace"][1]["provider"] == "plugin_lab-one_standards"
    assert result["trace"][1]["result"] == "Value" and not result["gaps"]


def test_codex_completion_and_malformed_traces_are_explicit():
    from openstudio_si_harness.evaluation.hosts import normalize

    rows = [
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "standards",
                "tool": "query",
                "arguments": {},
                "result": "x",
                "status": "completed",
            },
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Answer"}},
        {"type": "turn.completed", "usage": {"output_tokens": 4}},
    ]
    result = normalize("codex", "\n".join(map(json.dumps, rows)))
    assert result["finished"] and result["answer"] == "Answer"
    assert result["trace"][0]["name"] == "query"
    assert "unparseable_host_event" in normalize("claude", "broken\n")["gaps"]
    assert "missing_completion_event" in normalize("codex", "")["gaps"]


def test_process_limits_preserve_partial_evidence_and_kill_group(tmp_path):
    import os
    import sys

    from openstudio_si_harness.evaluation.hosts import process

    result = process(
        [
            sys.executable,
            "-c",
            'import time; print("partial",flush=True); time.sleep(5)',
        ],
        tmp_path,
        {"PATH": os.defpath},
        1,
    )
    assert "host_timeout" in result["gaps"] and "partial" in result["stdout"]
    result = process(
        [sys.executable, "-c", 'print("x"*10000)'],
        tmp_path,
        {"PATH": os.defpath},
        5,
        100,
    )
    assert "trace_limit_exceeded" in result["gaps"] and len(result["stdout"]) <= 100


def test_qualified_skill_requirements_do_not_merge_plugins():
    from openstudio_si_harness.evaluation.checks import ToolCall, check_skills

    calls = [
        ToolCall("Skill", "agent", {"skill": "lab-one:lookup"}),
        ToolCall("Skill", "agent", {"skill": "lab-two:lookup"}),
    ]
    assert check_skills(calls, ["lab-one:lookup"])[0].passed
    assert not check_skills(calls, ["lookup"])[0].passed


def fake_claude(tmp_path, mode="success"):
    import sys

    command = tmp_path / "claude-fake"
    command.write_text(f"""#!{sys.executable}
import json,sys,pathlib,os
if '--version' in sys.argv:print('test-host 1.0');sys.exit(0)
plugins=[pathlib.Path(sys.argv[i+1]).name for i,x in enumerate(sys.argv) if x=='--plugin-dir']
if {mode!r}=='error':print('host error');sys.exit(1)
if {mode!r}=='artifact':pathlib.Path('answer.json').write_text(json.dumps({{'value':42}}))
print(json.dumps({{'type':'system','subtype':'init','plugins':[{{'name':p}} for p in plugins],'mcp_servers':[]}}))
for p in plugins:
 print(json.dumps({{'type':'assistant','message':{{'content':[{{'type':'tool_use','id':p,'name':'Skill','input':{{'skill':p+':'+p}}}}]}}}}))
 print(json.dumps({{'type':'user','message':{{'content':[{{'type':'tool_result','tool_use_id':p,'content':'Loaded'}}]}}}}))
print(json.dumps({{'type':'result','result':'The result is 42. '+os.environ.get('SI_EVAL_AGENT_KEY',''),'is_error':False,'usage':{{'output_tokens':10}},'total_cost_usd':.01}}))
""")
    command.chmod(0o755)
    return command


def run_fixture(
    tmp_path,
    monkeypatch,
    mode="success",
    case_updates=None,
    config_updates=None,
    legacy=False,
    before_run=None,
):
    from openstudio_si_harness.evaluation.runner import run
    from openstudio_si_harness.export import export_plugin
    from openstudio_si_harness.generator import init_project

    project = tmp_path / "project"
    init_project(project, identity="eval-demo", license_identifier="MIT")
    source = project / "plugin"
    cases = source / "evals/cases"
    (cases / "layout-demo.json").unlink()
    write_case(
        source, **{"required_skills": ["eval-demo:eval-demo"], **(case_updates or {})}
    )
    if legacy:
        path = cases / "example.json"
        case = json.loads(path.read_text())
        del case["schema_version"]
        path.write_text(json.dumps([case]))
    export = export_plugin(source, tmp_path / "export")
    command = fake_claude(tmp_path, mode)
    config = {
        "schema_version": "0.1",
        "host": "claude",
        "agent": {
            "model": "local-fake",
            "command": str(command),
            "api_key_env": "SI_EVAL_AGENT_KEY",
        },
        "plugins": [export["packages"][(config_updates or {}).get("host", "claude")]],
        **(config_updates or {}),
    }
    config_path = tmp_path / "run.json"
    config_path.write_text(json.dumps(config))
    monkeypatch.setenv("SI_EVAL_AGENT_KEY", "secret-local-agent-value")
    if before_run:
        cases = before_run(project, source, export, config_path) or cases
    result = run(source, cases, config_path, tmp_path / "run")
    return result, tmp_path / "run", config_path


def test_end_to_end_staging_hashes_and_no_secret_leak(tmp_path, monkeypatch):
    result, root, _ = run_fixture(
        tmp_path, monkeypatch, "artifact", {"expected": {"artifacts": ["answer.json"]}}
    )
    assert result["status"] == "passed"
    session = root / "sessions/example--default--1"
    assert json.loads((session / "artifacts/answer.json").read_text())["value"] == 42
    assert "[REDACTED]" in (session / "answer.txt").read_text()
    for path in root.rglob("*"):
        if path.is_file() and "host-home" not in path.parts:
            assert b"secret-local-agent-value" not in path.read_bytes(), path
    manifest = json.loads((session / "result.json").read_text())
    assert manifest["evidence_hashes"]["artifacts/answer.json"]
    (session / "artifacts/answer.json").write_text("tampered")
    from openstudio_si_harness.evaluation.runner import report

    with pytest.raises(ValueError, match="Evidence changed"):
        report(root)


def test_agent_claim_does_not_pass_missing_artifact(tmp_path, monkeypatch):
    result, _, _ = run_fixture(
        tmp_path,
        monkeypatch,
        case_updates={"expected": {"artifacts": ["missing.json"]}},
    )
    assert result["status"] == "failed"
    assert any(
        c["check"] == "artifact:missing.json" and c["passed"] is False
        for c in result["sessions"][0]["checks"]
    )


def test_host_failure_remains_incomplete(tmp_path, monkeypatch):
    result, root, _ = run_fixture(tmp_path, monkeypatch, mode="error")
    assert result["status"] == "incomplete"
    assert "host_execution_failed" in result["sessions"][0]["gaps"]
    assert (root / "sessions/example--default--1/stdout.jsonl").exists()


def test_case_selection_repeats_and_interruption_inventory(tmp_path, monkeypatch):
    result, root, _ = run_fixture(
        tmp_path,
        monkeypatch,
        config_updates={
            "arms": {
                "one": {"plugins": ["eval-demo"]},
                "two": {"plugins": ["eval-demo"]},
            },
            "repeat": 2,
        },
    )
    assert len(result["sessions"]) == 4 and result["status"] == "passed"
    metadata = json.loads((root / "run.json").read_text())
    metadata["planned_sessions"].append("example--other--1")
    (root / "run.json").write_text(json.dumps(metadata))
    from openstudio_si_harness.evaluation.runner import report

    assert report(root)["status"] == "incomplete"


def judged_case():
    return {
        "judge": {
            "threshold": 0.8,
            "framework": "deepeval",
            "metrics": [
                {
                    "id": "correctness",
                    "type": "geval",
                    "evaluation_steps": [
                        "Check that actual_output answers input accurately."
                    ],
                    "evaluation_params": ["input", "actual_output"],
                    "weight": 1,
                }
            ],
        }
    }


def fake_judge_response(score=0.1, identity="correctness"):
    return {
        "framework": "deepeval",
        "framework_version": "4.3.4",
        "metrics": [
            {
                "id": identity,
                "score": score,
                "reason": "Evidence was compared.",
                "threshold": 0.8,
                "passed": score >= 0.8,
            }
        ],
    }


def test_judge_score_advisory_history_and_tamper_detection(tmp_path, monkeypatch):
    from openstudio_si_harness.evaluation import judge
    from openstudio_si_harness.evaluation.runner import rejudge

    _, root, _ = run_fixture(tmp_path, monkeypatch, case_updates=judged_case())
    monkeypatch.setenv("SI_EVAL_JUDGE_KEY", "private-judge-key-value")

    def provider(**kwargs):
        assert kwargs["api_key"] == "private-judge-key-value"
        assert "private-judge-key-value" not in json.dumps(kwargs["request"])
        return fake_judge_response(0.1)

    monkeypatch.setattr(
        judge,
        "invoke",
        lambda settings, request, key, timeout: provider(
            request=request, api_key=key, timeout=timeout
        ),
    )
    settings = {
        "framework": "deepeval",
        "provider": "openai",
        "model": "fake",
        "api_key_env": "SI_EVAL_JUDGE_KEY",
    }
    report = rejudge(root, settings)
    assert report["status"] == "passed"
    assert report["sessions"][0]["judge"]["score"] == 0.1
    assert not report["sessions"][0]["judge"]["above_threshold"]
    monkeypatch.setattr(judge, "invoke", lambda *args: fake_judge_response(0.9))
    assert rejudge(root, settings)["sessions"][0]["judge"]["score"] == 0.9
    assert len(list((root / "judgments").glob("*/index.json"))) == 2
    for path in (root / "judgments").glob("**/*.json"):
        assert "private-judge-key-value" not in path.read_text()
    metadata = json.loads((root / "run.json").read_text())
    session = root / "sessions" / metadata["sessions"][0]
    captured = json.loads((session / "result.json").read_text())
    captured["status"] = "failed"
    (session / "result.json").write_text(json.dumps(captured))
    with pytest.raises(ValueError, match="Captured result changed"):
        rejudge(root, settings)


@pytest.mark.parametrize(
    "kind", ["unknown-metric", "malformed", "invalid-score", "timeout", "missing-key"]
)
def test_judge_failures_never_become_scores(tmp_path, monkeypatch, kind):
    from openstudio_si_harness.evaluation import judge
    from openstudio_si_harness.evaluation.runner import rejudge

    _, root, _ = run_fixture(tmp_path, monkeypatch, case_updates=judged_case())
    if kind != "missing-key":
        monkeypatch.setenv("SI_EVAL_JUDGE_KEY", "judge-test-secret")

    def provider(**kwargs):
        if kind == "timeout":
            raise ValueError("judge_timeout")
        return (
            {"metrics": [{"id": "correctness"}]}
            if kind == "malformed"
            else (
                fake_judge_response(score=8)
                if kind == "invalid-score"
                else fake_judge_response(identity="nonexistent")
            )
        )

    monkeypatch.setattr(
        judge,
        "invoke",
        lambda settings, request, key, timeout: provider(
            request=request, api_key=key, timeout=timeout
        ),
    )
    report = rejudge(
        root,
        {
            "framework": "deepeval",
            "provider": "openai",
            "model": "fake",
            "api_key_env": "SI_EVAL_JUDGE_KEY",
        },
        {"judge_timeout_seconds": 0.1},
    )
    assert report["status"] == "incomplete"
    assert report["sessions"][0]["judge"]["status"] == "incomplete"
    assert "score" not in report["sessions"][0]["judge"]


def fake_codex(tmp_path):
    import sys

    command = tmp_path / "codex-fake"
    command.write_text(f"""#!{sys.executable}
import json,os,pathlib,sys
if '--version' in sys.argv:print('test-codex 1.0');sys.exit(0)
home=pathlib.Path(os.environ['CODEX_HOME']);record=home/'selection.json'
if sys.argv[1:3]==['plugin','marketplace']:print('{{}}');sys.exit(0)
if sys.argv[1:3]==['plugin','add']:
 values=json.loads(record.read_text()) if record.exists() else []
 values.append(sys.argv[3].split('@')[0]);record.write_text(json.dumps(values));print('{{}}');sys.exit(0)
if sys.argv[1:3]==['plugin','list']:
 print(json.dumps({{'installed':[{{'name':x,'enabled':True}} for x in json.loads(record.read_text())]}}));sys.exit(0)
assert '--dangerously-bypass-approvals-and-sandbox' not in sys.argv
pathlib.Path('result.json').write_text('{{"answer":42}}')
print(json.dumps({{'type':'item.completed','item':{{'type':'agent_message','text':'Saved the result.'}}}}))
print(json.dumps({{'type':'turn.completed','usage':{{'input_tokens':5,'output_tokens':3}}}}))
""")
    command.chmod(0o755)
    return command


@pytest.mark.parametrize("required_skill", [False, True])
def test_codex_driver_uses_isolated_plugins_and_marks_skill_gaps(
    tmp_path, monkeypatch, required_skill
):
    command = fake_codex(tmp_path)
    result, root, _ = run_fixture(
        tmp_path,
        monkeypatch,
        case_updates={
            "required_skills": ["eval-demo:eval-demo"] if required_skill else [],
            "expected": {"artifacts": ["result.json"]},
        },
        config_updates={
            "host": "codex",
            "agent": {
                "model": "fake-codex",
                "command": str(command),
                "api_key_env": "SI_EVAL_AGENT_KEY",
            },
        },
    )
    assert result["status"] == "passed"
    session = root / "sessions/example--default--1"
    assert (
        json.loads((session / "host-inventory.json").read_text())["installed"][0][
            "name"
        ]
        == "eval-demo"
    )
    assert not (session / "host-home").exists()
    if required_skill:
        assert (
            "skill_activation_not_observable_in_codex_exec"
            in result["sessions"][0]["notes"]
        )


def test_native_model_checks_reject_unchanged_input_copies(tmp_path, monkeypatch):
    from openstudio_si_harness.evaluation.contracts import digest
    from openstudio_si_harness.evaluation.runner import observe

    session = tmp_path
    path = session / "artifacts/model.osm"
    path.parent.mkdir()
    data = b"OS:ThermalZone,\n  zone-id; !- Handle\n"
    path.write_bytes(data)
    (session / "inputs.json").write_text(
        json.dumps({"inputs/original.osm": digest(data)})
    )
    checks, _ = observe(
        {"expected": {"model": {"counts": {"OS:ThermalZone": 1}}}},
        False,
        "default",
        "claude",
        {"trace": [], "answer": "Saved"},
        {"model.osm": digest(data)},
        session,
    )
    assert not all(c["passed"] for c in checks)


def test_judge_cannot_hide_missing_mandatory_artifact(tmp_path, monkeypatch):
    from openstudio_si_harness.evaluation import judge
    from openstudio_si_harness.evaluation.runner import rejudge

    _, root, _ = run_fixture(
        tmp_path,
        monkeypatch,
        case_updates={**judged_case(), "expected": {"artifacts": ["absent.json"]}},
    )
    monkeypatch.setenv("SI_JUDGE_KEY", "local-judge-secret")
    monkeypatch.setattr(judge, "invoke", lambda *args: fake_judge_response(1.0))
    result = rejudge(
        root,
        {
            "framework": "deepeval",
            "provider": "openai",
            "model": "fake",
            "api_key_env": "SI_JUDGE_KEY",
        },
    )
    assert result["status"] == "failed" and result["sessions"][0]["judge"]["score"] == 1


def test_evidence_truncation_is_an_incomplete_judgment(tmp_path, monkeypatch):
    from openstudio_si_harness.evaluation import judge
    from openstudio_si_harness.evaluation.runner import rejudge

    case = judged_case()
    case["judge"]["metrics"][0]["evaluation_params"].append("context")
    _, root, _ = run_fixture(tmp_path, monkeypatch, case_updates=case)
    monkeypatch.setenv("SI_JUDGE_KEY", "local-judge-secret")
    monkeypatch.setattr(judge, "invoke", lambda *args: fake_judge_response(1))
    result = rejudge(
        root,
        {
            "framework": "deepeval",
            "provider": "openai",
            "model": "fake",
            "api_key_env": "SI_JUDGE_KEY",
        },
        {"max_evidence_chars": 100},
    )
    assert result["status"] == "incomplete"
    assert result["sessions"][0]["judge"]["evidence_gaps"]


def test_run_limits_fail_before_starting_models(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="max_sessions"):
        run_fixture(
            tmp_path,
            monkeypatch,
            config_updates={"repeat": 2, "limits": {"max_sessions": 1}},
        )
    assert not (tmp_path / "run").exists()


def test_claude_agent_cost_budget_is_shared_between_repeats(tmp_path, monkeypatch):
    result, _root, _ = run_fixture(
        tmp_path,
        monkeypatch,
        config_updates={"repeat": 2, "limits": {"max_cost_usd": 0.01}},
    )
    assert result["sessions"][0]["status"] == "passed"
    assert result["sessions"][1]["status"] == "incomplete"
    assert "agent_cost_budget_exhausted" in result["sessions"][1]["gaps"]


def test_report_can_be_regenerated_without_credentials(tmp_path, monkeypatch):
    _, root, _ = run_fixture(tmp_path, monkeypatch)
    monkeypatch.delenv("SI_EVAL_AGENT_KEY")
    from openstudio_si_harness.evaluation.runner import report

    assert report(root)["status"] == "passed"


def test_failed_workspaces_and_private_host_state_are_not_retained(
    tmp_path, monkeypatch
):
    _, root, _ = run_fixture(tmp_path, monkeypatch, mode="error")
    session = root / "sessions/example--default--1"
    assert not (session / "workspace").exists()
    assert not (session / "host-home").exists()
    assert (session / "result.json").exists()


def test_model_field_all_requires_every_object_to_have_the_field(tmp_path):
    from openstudio_si_harness.evaluation.checks import check_model

    path = tmp_path / "model.osm"
    path.write_text("OS:Thing,\n  yes; !- Required\nOS:Thing,\n  value; !- Other\n")
    checks = check_model(
        {
            "fields": [
                {
                    "type": "OS:Thing",
                    "field": "Required",
                    "equals": "yes",
                    "match": "all",
                }
            ]
        },
        path,
    )
    assert not checks[-1].passed


def test_non_finite_configuration_is_rejected(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        '{"schema_version":"0.1","host":"claude","agent":{"model":"fake"},"plugins":["path"],"limits":{"case_timeout_seconds":NaN}}'
    )
    with pytest.raises(ValueError):
        load_config(path)


def test_installed_eval_commands_run_and_report_outside_checkout(tmp_path, monkeypatch):
    import subprocess
    import sys
    from pathlib import Path

    _, _root, config = run_fixture(tmp_path, monkeypatch)
    command = str(Path(sys.executable).with_name("openstudio-si"))
    second = tmp_path / "cli-run"
    executed = subprocess.run(
        [
            command,
            "eval",
            "run",
            "--plugin",
            str(tmp_path / "project/plugin"),
            "--cases",
            str(tmp_path / "project/plugin/evals/cases"),
            "--config",
            str(config),
            "--output",
            str(second),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert executed.returncode == 0, executed.stderr
    assert json.loads(executed.stdout)["status"] == "passed"
    monkeypatch.delenv("SI_EVAL_AGENT_KEY")
    rendered = subprocess.run(
        [command, "eval", "report", "--run", str(second)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert rendered.returncode == 0, rendered.stderr
    assert (second / "report.md").is_file()


def test_generated_eval_config_uses_project_paths_and_is_valid(tmp_path):
    from openstudio_si_harness.generator import init_project

    project = tmp_path / "project"
    init_project(project, identity="eval-template", license_identifier="MIT")
    path = project / "eval-run.example.yaml"
    value = load_config(path)
    assert value["plugins"] == ["./dist/plugin/claude/eval-template"]
    assert value["judge"]["api_key_env"] == "JUDGE_API_KEY"
    assert not (project / "plugin/evals/run.example.yaml").exists()


def test_interrupt_stops_later_agent_sessions(tmp_path, monkeypatch):
    from openstudio_si_harness.evaluation import runner

    calls = []

    def interrupted(*args, **kwargs):
        calls.append(1)
        return {
            "stdout": "",
            "stderr": "",
            "gaps": ["host_interrupted"],
            "returncode": -9,
            "duration_seconds": 0.01,
        }

    monkeypatch.setattr(runner, "process", interrupted)
    result, _, _ = run_fixture(tmp_path, monkeypatch, config_updates={"repeat": 3})
    assert len(calls) == 1 and len(result["sessions"]) == 3
    assert result["status"] == "incomplete"
    assert result["sessions"][1]["gaps"] == ["session_not_executed"]


def test_missing_configured_judge_key_fails_before_agent_spend(tmp_path, monkeypatch):
    monkeypatch.delenv("NONEXISTENT_JUDGE_TEST_KEY", raising=False)
    with pytest.raises(
        ValueError, match="Judge credential environment variable is missing"
    ):
        run_fixture(
            tmp_path,
            monkeypatch,
            config_updates={
                "judge": {
                    "framework": "deepeval",
                    "provider": "openai",
                    "model": "fake",
                    "api_key_env": "NONEXISTENT_JUDGE_TEST_KEY",
                }
            },
        )
    assert not (tmp_path / "run").exists()


def test_complete_agent_and_judge_workflow_uses_local_gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,::1")
    import http.server
    import threading

    class Gateway(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert "max_completion_tokens" in body and "tools" not in body
            assert "Check that actual_output" in body["messages"][0]["content"]
            data = json.dumps(
                {
                    "id": "local-completion",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "local-mock",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {
                                        "score": 2,
                                        "reason": "Compared captured evidence.",
                                    }
                                ),
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 4,
                        "completion_tokens": 4,
                        "total_tokens": 8,
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SI_EVAL_JUDGE_KEY", "local-judge-gateway-secret")
    try:
        result, root, _ = run_fixture(
            tmp_path,
            monkeypatch,
            case_updates=judged_case(),
            config_updates={
                "judge": {
                    "framework": "deepeval",
                    "provider": "openai-compatible",
                    "model": "local-mock",
                    "api_key_env": "SI_EVAL_JUDGE_KEY",
                    "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                }
            },
        )
        assert result["status"] == "passed"
        assert result["sessions"][0]["judge"]["score"] == 0.2
        assert result["sessions"][0]["judge"]["framework_version"].startswith("4.3.")
        assert result["sessions"][0]["judge"]["metrics"][0]["evaluation_steps"]
        assert json.loads((root / "run.json").read_text())["judgment_history"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_unknown_host_tool_events_are_explicitly_incomplete():
    from openstudio_si_harness.evaluation.hosts import normalize

    raw = "\n".join(
        map(
            json.dumps,
            [
                {
                    "type": "item.completed",
                    "item": {"type": "new_tool_protocol", "tool": "mutate"},
                },
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Done"},
                },
                {"type": "turn.completed"},
            ],
        )
    )
    assert (
        "unrecognized_codex_item_type:new_tool_protocol"
        in normalize("codex", raw)["gaps"]
    )
    claude = json.dumps(
        {"type": "result", "subtype": "error_budget", "result": "Stopped"}
    )
    assert normalize("claude", claude)["failed"]


def test_legacy_default_arm_does_not_drop_required_tool_checks(tmp_path, monkeypatch):
    result, _, _ = run_fixture(
        tmp_path,
        monkeypatch,
        legacy=True,
        case_updates={"required_tools": [{"tool": "required_but_not_called"}]},
    )
    assert result["status"] == "failed"
    assert result["sessions"][0]["session_id"] == "example--both--1"
    assert any(
        not check["passed"] and check["check"].startswith("required:")
        for check in result["sessions"][0]["checks"]
    )


def test_normal_completion_does_not_signal_a_reaped_process(tmp_path, monkeypatch):
    import os
    import sys

    from openstudio_si_harness.evaluation import hosts

    signals = []
    monkeypatch.setattr(hosts.os, "killpg", lambda *args: signals.append(args))
    result = hosts.process(
        [sys.executable, "-c", 'print("complete")'], tmp_path, {"PATH": os.defpath}, 5
    )
    assert result["returncode"] == 0 and not result["gaps"]
    assert not signals


def test_case_maps_reference_trace_context_and_tools_into_deepeval(
    tmp_path, monkeypatch
):
    from openstudio_si_harness.evaluation import judge
    from openstudio_si_harness.evaluation.runner import rejudge

    spec = judged_case()
    spec.update(
        expected={"output": "The correct answer is 42."},
        context=["A reviewed unit convention."],
        retrieval_context=["A reviewed retrieval snippet."],
        required_tools=[],
    )
    spec["judge"]["metrics"][0]["evaluation_params"] = [
        "input",
        "actual_output",
        "expected_output",
        "context",
        "retrieval_context",
        "tools_called",
        "expected_tools",
    ]
    _, root, _ = run_fixture(tmp_path, monkeypatch, case_updates=spec)
    monkeypatch.setenv("LOCAL_MAPPING_KEY", "only-a-fake-test-key")

    def invoke(settings, request, key, timeout):
        assert settings["provider"] == "anthropic"
        data = request["test_case"]
        assert data["input"] == "Explain."
        assert data["expected_output"] == "The correct answer is 42."
        assert data["context"][0] == "A reviewed unit convention."
        assert data["retrieval_context"] == ["A reviewed retrieval snippet."]
        assert data["tools_called"][0]["name"] == "agent:Skill"
        assert (
            data["tools_called"][0]["input_parameters"]["skill"]
            == "eval-demo:eval-demo"
        )
        assert data["expected_tools"] == []
        assert "messages" not in request  # DeepEval constructs its own prompts.
        assert request["metrics"][0]["threshold"] == 0.7
        return fake_judge_response(0.9)

    monkeypatch.setattr(judge, "invoke", invoke)
    result = rejudge(
        root,
        {
            "framework": "deepeval",
            "provider": "anthropic",
            "model": "fake",
            "api_key_env": "LOCAL_MAPPING_KEY",
            "geval": {"threshold": 0.7},
        },
    )
    assert result["status"] == "passed"
