"""Substantial tool-result leaks block judging; limited overlap stays advisory."""

import json

import pytest
from openstudio_si_harness.evaluation.isolation import holdout_content

from .test_reference_matching import REFERENCE


@pytest.mark.parametrize("count,blocking", [(1, False), (2, True), (3, True)])
def test_private_line_coverage_across_tool_results(count, blocking):
    trace = [
        {"id": f"trace:{i}", "result": line}
        for i, line in enumerate(REFERENCE.splitlines()[:count])
    ]
    findings = holdout_content(trace, "Done.", [REFERENCE])
    assert len(findings) == count
    assert all(f["blocking"] is blocking for f in findings)


def test_exactly_half_or_repeated_single_line_is_only_advisory():
    lines = REFERENCE.splitlines()
    reference = REFERENCE + "The roof has a reviewed insulation construction.\n"
    trace = [{"id": "trace:0", "result": "\n".join(lines[:2])}]
    assert not holdout_content(trace, "", [reference])[0]["blocking"]
    trace[0]["result"] = "\n".join([lines[0]] * 10)
    assert not holdout_content(trace, "", [REFERENCE])[0]["blocking"]


@pytest.mark.parametrize(
    "reference", [REFERENCE, "Floor count: 2\nZone count: 5\nSpace type: office\n"]
)
def test_public_lines_do_not_create_a_majority_or_whole_reference_match(reference):
    trace = [{"id": "trace:0", "result": reference}]
    assert not holdout_content(
        trace, reference, [reference], public_texts=reference.splitlines()
    )


@pytest.mark.parametrize("mode", ["whole", "most", "single", "answer"])
def test_parent_folder_leak_severity_controls_judging(tmp_path, monkeypatch, mode):
    from openstudio_si_harness.evaluation import judge
    from openstudio_si_harness.evaluation.runner import rejudge
    from openstudio_si_harness.export import export_plugin

    from .test_host_sessions import host
    from .test_runner import fake_judge_response, judged_case, run_fixture

    # Read the actual key through a parent directory, without naming evals in tool arguments.
    body = f"""
parent=pathlib.Path({str(tmp_path)!r})
ref=next((parent/'export-new').rglob('layout.md')).read_text()
lines=ref.splitlines()
selected=lines if {mode!r}=='whole' else lines[:2] if {mode!r}=='most' else lines[:1] if {mode!r}=='single' else []
payload=chr(10).join('/parent/layout.md:'+str(i+1)+':'+line for i,line in enumerate(selected))
emit({{'type':'assistant','message':{{'content':[{{'type':'tool_use','id':'grep','name':'Grep','input':{{'pattern':'.','path':str(parent)}}}}]}}}})
emit({{'type':'user','message':{{'content':[{{'type':'tool_result','tool_use_id':'grep','content':payload}}]}}}})
emit({{'type':'result','result':ref,'is_error':False,'usage':{{}},'total_cost_usd':.01}})
sys.exit(0)
"""

    def setup(project, source, export, config_path):
        (source / "evals/reference/layout.md").write_text(REFERENCE)
        fresh = export_plugin(source, tmp_path / "export-new")
        config = json.loads(config_path.read_text())
        config["plugins"] = [fresh["packages"]["claude"]]
        config_path.write_text(json.dumps(config))

    result, root, _ = run_fixture(
        tmp_path,
        monkeypatch,
        case_updates={
            **judged_case(),
            "required_skills": [],
            "expected": {"reference": "evals/reference/layout.md"},
        },
        config_updates={"agent": host(tmp_path, body), "repeat": 2},
        before_run=setup,
    )
    blocked = mode in {"whole", "most"}
    assert result["status"] == ("failed" if blocked else "passed")
    calls = []
    monkeypatch.setenv("R7_JUDGE_KEY", "fake-round7-judge-key")
    monkeypatch.setattr(
        judge, "invoke", lambda *args: calls.append(args) or fake_judge_response(0.9)
    )
    scored = rejudge(
        root,
        {
            "framework": "deepeval",
            "provider": "openai",
            "model": "fake",
            "api_key_env": "R7_JUDGE_KEY",
        },
    )
    assert len(calls) == (0 if blocked else 2)
    assert scored["status"] == ("failed" if blocked else "passed")
    assert scored["warning_count"] == 2
    assert json.loads((root / "summary.json").read_text())["warning_count"] == 2
    for session in scored["sessions"]:
        judgment = session["judge"]
        assert judgment["status"] == ("skipped" if blocked else "scored")
        if blocked:
            assert "score" not in judgment
            assert any(
                c["check"] == "holdout_access" and not c["passed"]
                for c in session["checks"]
            )
        else:
            assert judgment["score"] == 0.9
        assert session["warnings"][0]["confidence"] == (
            "medium" if mode == "single" else "low"
        )
