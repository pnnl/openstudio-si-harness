"""Multiline and partial reference-text matching."""

import pytest
from openstudio_si_harness.evaluation.isolation import holdout_content

REFERENCE = (
    "Reviewed layout for the demo building.\n"
    "Story 1 has four perimeter zones and one core zone.\n"
    "All zones use the office space type.\n"
)


def formatted_reference(style):
    lines = REFERENCE.splitlines()
    if style == "read-tab":
        return "\n".join(f"     {index}\t{line}" for index, line in enumerate(lines, 1))
    if style == "read-arrow":
        return "\n".join(f"     {index}→{line}" for index, line in enumerate(lines, 1))
    if style == "grep":
        return "\n".join(
            f"/parent/reference/layout.md:{index}:{line}"
            for index, line in enumerate(lines, 1)
        )
    if style == "grep-context":
        return "\n".join(
            f"/parent/reference/layout.md-{index}-{line}"
            for index, line in enumerate(lines, 1)
        )
    if style == "drop-first":
        return "\n".join(lines[1:])
    if style == "drop-middle":
        return "\n".join([lines[0], lines[2]])
    if style == "drop-last":
        return "\n".join(lines[:-1])
    if style == "one-word":
        return REFERENCE.replace("four", "4")
    if style == "single-line":
        return lines[1]
    raise AssertionError(style)


@pytest.mark.parametrize(
    "style,where",
    [
        (style, where)
        for style in [
            "read-tab",
            "read-arrow",
            "grep",
            "grep-context",
            "drop-first",
            "drop-middle",
            "drop-last",
            "one-word",
            "single-line",
        ]
        for where in ["answer", "tool"]
        if where == "tool"
        or style not in {"read-tab", "read-arrow", "grep", "grep-context"}
    ],
)
def test_multiline_or_partial_reference_is_flagged(style, where):
    text = formatted_reference(style)
    trace = (
        [{"id": "trace:0", "result": {"content": [{"type": "text", "text": text}]}}]
        if where == "tool"
        else []
    )
    answer = text if where == "answer" else "Done."
    assert holdout_content(trace, answer, [REFERENCE]) == [
        {
            "location": f'{"answer" if where=="answer" else "trace:0"}:holdout_content',
            "blocking": where == "tool" and style != "single-line",
        }
    ]


def test_short_facts_or_unrelated_similar_prose_are_not_flagged():
    assert not holdout_content([], "Story 1 has 4 zones.", ["4", "Story 1", REFERENCE])
    assert not holdout_content(
        [], "The model has a layout with several buildings and zones.", [REFERENCE]
    )


@pytest.mark.parametrize("style", ["read-tab", "grep", "drop-middle", "one-word"])
def test_multiline_parent_read_fails_and_skips_judging(tmp_path, monkeypatch, style):
    from openstudio_si_harness.evaluation import judge
    from openstudio_si_harness.evaluation.runner import rejudge
    from openstudio_si_harness.export import export_plugin

    from .test_host_sessions import host
    from .test_runner import fake_judge_response, judged_case, run_fixture

    payload = formatted_reference(style)
    body = f"""
emit({{'type':'assistant','message':{{'content':[{{'type':'tool_use','id':'read','name':'Grep','input':{{'path':{str(tmp_path)!r},'pattern':'.'}}}}]}}}})
emit({{'type':'user','message':{{'content':[{{'type':'tool_result','tool_use_id':'read','content':{payload!r}}}]}}}})
emit({{'type':'result','result':{payload!r},'is_error':False,'usage':{{}},'total_cost_usd':.01}})
sys.exit(0)
"""

    def reference(project, source, export, config_path):
        (source / "evals/reference/layout.md").write_text(REFERENCE)
        fresh = export_plugin(source, tmp_path / "multiline-export")
        import json

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
        before_run=reference,
    )
    assert result["status"] == "failed"
    assert any(
        c["check"] == "holdout_access" and not c["passed"]
        for c in result["sessions"][0]["checks"]
    )
    calls = []
    monkeypatch.setenv("R5_JUDGE_KEY", "fake-round5-judge-key")
    monkeypatch.setattr(
        judge, "invoke", lambda *args: calls.append(args) or fake_judge_response(0.9)
    )
    scored = rejudge(
        root,
        {
            "framework": "deepeval",
            "provider": "openai",
            "model": "fake",
            "api_key_env": "R5_JUDGE_KEY",
        },
    )
    assert not calls and scored["status"] == "failed"
    assert scored["sessions"][0]["judge"]["status"] == "skipped"
    assert "score" not in scored["sessions"][0]["judge"]
