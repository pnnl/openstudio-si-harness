"""Contract and real G-Eval integration tests; no cloud/model calls."""

import copy
import json

import pytest
from openstudio_si_harness.evaluation.contracts import load_cases, validate


@pytest.fixture(autouse=True)
def offline_deepeval(monkeypatch):
    monkeypatch.setenv("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
    monkeypatch.setenv("DEEPEVAL_DISABLE_DOTENV", "1")
    monkeypatch.setenv("DEEPEVAL_FILE_SYSTEM", "READ_ONLY")


def metric(**updates):
    return {
        "id": "correctness",
        "type": "geval",
        "criteria": "Compare actual_output to expected_output.",
        "evaluation_params": ["actual_output", "expected_output"],
        **updates,
    }


def case(**updates):
    return {
        "schema_version": "0.1",
        "case_id": "lookup",
        "description": "A standards lookup.",
        "prompt": "Find the standard value.",
        "inputs": [],
        "expected": {"output": "42, with a source citation."},
        "judge": {"framework": "deepeval", "metrics": [metric()], "threshold": 0.8},
        **updates,
    }


@pytest.mark.parametrize(
    "change",
    [
        "missing-criteria",
        "both-modes",
        "unknown-field",
        "bad-param",
        "duplicate-id",
        "overlap",
        "reversed",
        "missing-evidence",
        "artifact-without-context",
    ],
)
def test_case_contract_rejects_invalid_geval_before_models(tmp_path, change):
    value = case()
    item = value["judge"]["metrics"][0]
    if change == "artifact-without-context":
        item["required_artifacts"] = ["*.sql"]
    if change == "missing-criteria":
        item.pop("criteria")
    if change == "both-modes":
        item["evaluation_steps"] = ["Compare outputs."]
    if change == "unknown-field":
        item["python_callable"] = "something"
    if change == "bad-param":
        item["evaluation_params"] = ["made_up"]
    if change == "duplicate-id":
        value["judge"]["metrics"].append(copy.deepcopy(item))
    if change == "overlap":
        item["rubric"] = [
            {"score_range": [0, 5], "expected_outcome": "Poor"},
            {"score_range": [5, 10], "expected_outcome": "Good"},
        ]
    if change == "reversed":
        item["rubric"] = [{"score_range": [9, 2], "expected_outcome": "Invalid"}]
    if change == "missing-evidence":
        value.pop("expected")
    (tmp_path / "case.json").write_text(json.dumps(value))
    with pytest.raises(ValueError):
        load_cases(tmp_path, tmp_path)


@pytest.mark.parametrize(
    "provider",
    ["openai", "azure-openai", "anthropic", "gemini", "ollama", "openai-compatible"],
)
def test_provider_schema_and_native_deepeval_model_mapping(monkeypatch, provider):
    from deepeval import models
    from openstudio_si_harness.evaluation.deepeval_backend import build_model

    settings = {
        "framework": "deepeval",
        "provider": provider,
        "model": "chosen",
        "max_output_tokens": 1024,
        "temperature": 0.2,
    }
    if provider != "ollama":
        settings["api_key_env"] = "LOCAL_TEST_KEY"
    if provider in {"azure-openai", "openai-compatible"}:
        settings["base_url"] = "https://models.example.test/v1"
    if provider == "azure-openai":
        settings.update(deployment_name="reviewed-deployment", api_version="2024-10-21")
    validate("evaluation-judge-config", settings)
    observed = {}
    names = {
        "openai": "OpenAIModel",
        "azure-openai": "AzureOpenAIModel",
        "anthropic": "AnthropicModel",
        "gemini": "GeminiModel",
        "ollama": "OllamaModel",
        "openai-compatible": "LocalModel",
    }

    def constructor(**kwargs):
        observed.update(kwargs)
        return object()

    monkeypatch.setattr(models, names[provider], constructor)
    build_model(settings, "fake-provider-key" if provider != "ollama" else None)
    assert observed["model"] == "chosen" and observed["temperature"] == 0.2
    if provider != "ollama":
        assert observed["api_key"] == "fake-provider-key"
    if provider == "azure-openai":
        assert observed["deployment_name"] == "reviewed-deployment"
    expected_key = {
        "anthropic": "max_tokens",
        "gemini": "max_output_tokens",
        "ollama": "num_predict",
    }.get(provider, "max_completion_tokens")
    assert observed["generation_kwargs"][expected_key] == 1024


@pytest.mark.parametrize(
    "settings",
    [
        {"provider": "azure-openai", "api_key_env": "KEY"},
        {"provider": "openai-compatible", "api_key_env": "KEY"},
        {"provider": "ollama", "api_key_env": "KEY"},
        {"provider": "openai", "api_key_env": "KEY", "deployment_name": "wrong"},
        {
            "provider": "gemini",
            "api_key_env": "KEY",
            "base_url": "https://ignored.example",
        },
    ],
)
def test_provider_contract_rejects_missing_or_ignored_options(settings):
    with pytest.raises(ValueError):
        validate(
            "evaluation-judge-config",
            {"framework": "deepeval", "model": "chosen", **settings},
        )


def test_real_geval_handles_generation_steps_score_bands_and_strict_mode(monkeypatch):
    from deepeval.models import DeepEvalBaseLLM
    from openstudio_si_harness.evaluation import deepeval_backend

    prompts = []

    class OfflineModel(DeepEvalBaseLLM):
        def load_model(self):
            return self

        def get_model_name(self):
            return "offline-deterministic"

        def generate(self, prompt, schema=None):
            prompts.append(prompt)
            if schema and "steps" in schema.model_fields:
                return schema(steps=["Compare actual_output against expected_output."])
            return schema(
                score=0 if len(prompts) == 3 else 8,
                reason="Compared outputs under the requested score scale.",
            )

        async def a_generate(self, prompt, schema=None):
            return self.generate(prompt, schema)

    monkeypatch.setattr(
        deepeval_backend, "build_model", lambda *_: OfflineModel("offline")
    )
    explicit = metric(
        id="strict",
        strict_mode=True,
        threshold=0.6,
        evaluation_steps=["Compare actual_output with expected_output."],
        rubric=[
            {"score_range": [0, 5], "expected_outcome": "Incorrect"},
            {"score_range": [6, 10], "expected_outcome": "Correct"},
        ],
    )
    explicit.pop("criteria")
    result = deepeval_backend.evaluate(
        {"geval": {"top_logprobs": 5}},
        {
            "test_case": {
                "input": "Find value",
                "actual_output": "42",
                "expected_output": "42",
                "context": [],
            },
            "metrics": [metric(), explicit],
        },
        None,
    )
    assert result["framework"] == "deepeval" and result["framework_version"].startswith(
        "4.3."
    )
    first, strict = result["metrics"]
    assert first["score"] == 0.8 and first["evaluation_steps"]
    assert strict["threshold"] == 1 and strict["score"] == 0 and not strict["passed"]
    assert strict["rubric"] == explicit["rubric"] and strict["top_logprobs"] == 5
    assert len(prompts) == 3  # one generated-steps call + two scoring calls


def test_worker_environment_is_explicit_and_timeout_is_preserved(tmp_path, monkeypatch):
    from openstudio_si_harness.evaluation import judge

    monkeypatch.setenv("UNRELATED_TEST_SECRET", "must-not-pass")

    def process(command, cwd, environment, timeout, cap, payload):
        assert "UNRELATED_TEST_SECRET" not in environment
        assert environment["OPENSTUDIO_SI_EVAL_WORKER_KEY"] == "test-key"
        assert environment["DEEPEVAL_TELEMETRY_OPT_OUT"] == "YES"
        assert environment["DEEPEVAL_DISABLE_DOTENV"] == "1"
        assert "test-key" not in payload
        return {"gaps": ["host_timeout"], "returncode": -9, "stdout": "", "stderr": ""}

    monkeypatch.setattr(judge, "process", process)
    with pytest.raises(ValueError, match="judge_timeout"):
        judge.invoke({"framework": "deepeval"}, {}, "test-key", 0.001)


def test_run_embeds_the_same_judge_configuration_contract():
    from importlib.resources import files

    root = files("openstudio_si_harness").joinpath("schemas")
    judge = json.loads(root.joinpath("evaluation-judge-config.schema.json").read_text())
    run = json.loads(root.joinpath("evaluation-run.schema.json").read_text())
    assert run["properties"]["judge"] == {
        k: v for k, v in judge.items() if k not in {"$schema", "title"}
    }


@pytest.mark.parametrize(
    "provider,model",
    [
        ("openai", "gpt-4o"),
        ("azure-openai", "gpt-4o"),
        ("anthropic", "claude-sonnet-4-20250514"),
        ("gemini", "gemini-2.5-flash"),
        ("ollama", "llama3.2"),
        ("openai-compatible", "lab-custom-model"),
    ],
)
def test_native_provider_clients_construct_without_network(
    monkeypatch, provider, model
):
    import socket

    from openstudio_si_harness.evaluation.deepeval_backend import build_model

    def no_network(*args, **kwargs):
        raise AssertionError("No model/provider network calls in constructor checks")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    settings = {"framework": "deepeval", "provider": provider, "model": model}
    if provider in {"azure-openai", "openai-compatible"}:
        settings["base_url"] = "https://models.example.test"
    if provider == "azure-openai":
        settings.update(deployment_name="offline-test", api_version="2024-10-21")
    model_instance = build_model(settings, "fake-credential-for-client-construction")
    assert model_instance.get_model_name()
