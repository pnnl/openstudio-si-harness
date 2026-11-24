"""Reference text is an advisory signal, distinct from observed path access."""

import json

import pytest
from openstudio_si_harness.evaluation.isolation import holdout_content

FACT = "All thermal zones use the office space type."


def test_generated_readme_explains_literal_answers_and_warnings(tmp_path):
    from openstudio_si_harness.generator import init_project

    project = tmp_path / "plugin-project"
    init_project(project, identity="eval-demo", license_identifier="MIT")
    readme = (project / "README.md").read_text()
    assert "Use `expected.output` for literal answers" in readme
    assert "Single-line tool-result overlap is an advisory" in readme
    assert "fails the session and skips judging" in readme


@pytest.mark.parametrize("where", ["answer", "tool"])
def test_public_reference_text_is_not_flagged(where):
    trace = [{"id": "trace:0", "result": FACT}] if where == "tool" else []
    answer = FACT if where == "answer" else "Done."
    assert not holdout_content(
        trace, answer, [FACT], public_texts=["Known fact: " + FACT]
    )


@pytest.mark.parametrize("where", ["answer", "tool"])
def test_date_in_reference_is_preserved(where):
    # Stripping the date would leave fewer than 32 characters and miss the match.
    reference = "Reviewed building layout on 2024-11-20."
    assert len(reference) >= 32
    trace = (
        [{"id": "trace:0", "result": "/tmp/report.md:1:" + reference}]
        if where == "tool"
        else []
    )
    assert holdout_content(
        trace, reference if where == "answer" else "Done.", [reference]
    )


@pytest.mark.parametrize("public_location", [None, "prompt", "input", "plugin"])
def test_correct_fact_can_be_scored_with_advisory_warning(
    tmp_path, monkeypatch, public_location
):
    from openstudio_si_harness.evaluation import judge
    from openstudio_si_harness.evaluation.runner import rejudge
    from openstudio_si_harness.export import export_plugin

    from .test_host_sessions import host
    from .test_runner import fake_judge_response, judged_case, run_fixture

    body = f"""
emit({{'type':'result','result':{'Answer: ' + FACT!r},'is_error':False,'usage':{{}},'total_cost_usd':.01}})
sys.exit(0)
"""

    def setup(project, source, export, config_path):
        (source / "evals/reference/layout.md").write_text(FACT)
        path = source / "evals/cases/example.json"
        case = json.loads(path.read_text())
        if public_location == "prompt":
            case["prompt"] += " Known facts: " + FACT
        elif public_location == "input":
            (source / "evals/fixtures/facts.txt").write_text(FACT)
            case["inputs"] = ["evals/fixtures/facts.txt"]
        elif public_location == "plugin":
            skill = source / "skills/eval-demo/SKILL.md"
            skill.write_text(skill.read_text() + "\nKnown facts: " + FACT)
        path.write_text(json.dumps(case))
        fresh = export_plugin(source, tmp_path / "fresh")
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
        config_updates={"agent": host(tmp_path, body)},
        before_run=setup,
    )
    assert result["status"] == "passed"
    session = result["sessions"][0]
    warnings = session["warnings"]
    if public_location:
        assert not warnings
    else:
        assert warnings[0]["code"] == "reference_text_match"
        assert warnings[0]["confidence"] == "low"
        assert warnings[0]["locations"] == ["answer:holdout_content"]
    assert next(c for c in session["checks"] if c["check"] == "holdout_access")[
        "passed"
    ]
    stored = json.loads(next((root / "sessions").glob("*/result.json")).read_text())
    assert stored["warnings"] == warnings
    calls = []
    monkeypatch.setenv("R6_JUDGE_KEY", "fake-round6-judge-key")
    monkeypatch.setattr(
        judge, "invoke", lambda *args: calls.append(args) or fake_judge_response(0.9)
    )
    scored = rejudge(
        root,
        {
            "framework": "deepeval",
            "provider": "openai",
            "model": "fake",
            "api_key_env": "R6_JUDGE_KEY",
        },
    )
    assert len(calls) == 1
    assert scored["sessions"][0]["judge"]["score"] == 0.9
    assert scored["sessions"][0]["warnings"] == warnings
    report = (root / "report.md").read_text()
    assert "Reference text warnings" in report
    if not public_location:
        assert "0.9 | low confidence" in report
        assert "not proof of access" in report
