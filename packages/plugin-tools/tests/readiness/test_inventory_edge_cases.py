"""Host identity, corrupt entries and conservative runtime readiness regressions."""

import importlib.metadata
import json
import shutil
from pathlib import Path

import pytest
import yaml
from openstudio_si_harness.cli import main
from openstudio_si_harness.export import DESCRIPTOR, export_plugin, validate_export
from openstudio_si_harness.readiness import doctor

from ..support.plugins import capability, dependency, plugin


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("provider_host", ["claude", "codex"])
def test_provider_host_must_match_consumer(
    tmp_path, monkeypatch, cached, provider_host
):
    consumer = plugin(tmp_path, "consumer", [dependency()])
    provider = plugin(tmp_path, "provider")
    monkeypatch.setenv("HOME", str(tmp_path))
    base = (
        tmp_path / ".claude/plugins/cache/test"
        if cached
        else tmp_path / "consumer-export"
    )
    c = export_plugin(consumer, base / "consumer", ("claude",))["packages"]["claude"]
    base = (
        tmp_path / f".{provider_host}/plugins/cache/test"
        if cached
        else tmp_path / "provider-export"
    )
    p = export_plugin(provider, base / "provider", (provider_host,))["packages"][
        provider_host
    ]
    report = doctor([] if cached else [c, p], plugin_id="consumer")
    assert report["status"] == (
        "ready" if provider_host == "claude" else "unknown" if cached else "unavailable"
    )
    assert report["plugins"][0]["host"] == "claude"
    if provider_host != "claude":
        dep = report["plugins"][0]["dependencies"][0]
        assert dep["reason"] == "provider_host_mismatch"
        assert dep["evidence"]["candidates"][0]["host"] == "codex"


def test_source_binding_and_cli_host_selection(tmp_path, capsys):
    provider = plugin(tmp_path, "provider")
    consumer = plugin(tmp_path, "consumer", [dependency()])
    c = export_plugin(consumer, tmp_path / "consumer-export")["packages"]["claude"]
    assert doctor([c, provider], plugin_id="consumer")["status"] == "unavailable"
    assert (
        main(
            [
                "plugin",
                "doctor",
                "--host",
                "claude",
                "--plugin-id",
                "consumer",
                "--plugin-dir",
                c,
                "--plugin-dir",
                str(provider),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["selection"]["host"] == "claude"
    codex = export_plugin(provider, tmp_path / "provider-export")["packages"]["codex"]
    assert main(["plugin", "doctor", "--plugin-dir", c, "--plugin-dir", codex]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "unavailable"


@pytest.mark.parametrize("required", [False, True])
def test_legacy_core_is_unknown_not_an_impossible_install_hint(tmp_path, required):
    root = plugin(
        tmp_path,
        "standards",
        [
            dependency(
                "core",
                target="openstudio-si",
                required=required,
                version_spec=">=0.4.3 <0.5.0",
            )
        ],
        [capability("lookup"), capability("model", ["core"])],
    )
    report = doctor([root], plugin_id="standards", capability="model")
    assert report["status"] == "unknown"
    dep = report["plugins"][0]["dependencies"][0]
    assert dep["reason"] == "legacy_core_not_ingested"
    assert "openstudio-si doctor" in dep["next_action"]
    assert "--plugin-dir" not in dep["next_action"]
    assert doctor([root], plugin_id="standards", capability="lookup")["status"] == (
        "unknown" if required else "ready"
    )


@pytest.mark.parametrize("cached", [False, True])
def test_missing_export_evaluation_is_isolated(tmp_path, monkeypatch, capsys, cached):
    monkeypatch.setenv("HOME", str(tmp_path))
    healthy = plugin(tmp_path, "healthy")
    broken = plugin(tmp_path, "broken")
    base = tmp_path / ".claude/plugins/cache/test" if cached else tmp_path / "exports"
    h = export_plugin(healthy, base / "healthy", ("claude",))["packages"]["claude"]
    b = Path(export_plugin(broken, base / "broken", ("claude",))["packages"]["claude"])
    path = b / DESCRIPTOR
    data = json.loads(path.read_text())
    del data["manifest"]["evaluation"]
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="evaluation"):
        validate_export(b)
    report = doctor([] if cached else [h, b])
    assert report["status"] == "unknown" and len(report["plugins"]) == 2
    assert (
        next(e for e in report["plugins"] if e.get("id") == "healthy")["status"]
        == "ready"
    )
    assert (
        next(e for e in report["plugins"] if e.get("reason"))["reason"]
        == "invalid_or_unreadable_metadata"
    )
    assert main(["plugin", "doctor", "--plugin-dir", h, "--plugin-dir", str(b)]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "unknown"


@pytest.mark.parametrize("present", [False, True])
def test_terminal_environment_is_never_used_as_runtime_evidence(
    tmp_path, monkeypatch, present
):
    if present:
        monkeypatch.setenv("SI_REVIEW_CONFIG", "do-not-record-this")
    else:
        monkeypatch.delenv("SI_REVIEW_CONFIG", raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("Must not probe the doctor's runtime")

    monkeypatch.setattr(importlib.metadata, "version", forbidden)
    monkeypatch.setattr(shutil, "which", forbidden)
    root = plugin(
        tmp_path,
        "runtime-consumer",
        [
            dependency("python", kind="python-distribution", target="pyyaml"),
            dependency("command", kind="command", target="python"),
            dependency(
                "config",
                kind="configuration",
                probe="configuration-present",
                target="SI_REVIEW_CONFIG",
            ),
        ],
    )
    report = doctor([root])
    assert report["status"] == "unknown"
    for dep in report["plugins"][0]["dependencies"]:
        assert dep["reason"] == "runtime_not_selected"
        assert dep["evidence"]["measurement_scope"] == "not_measured"
    assert "do-not-record-this" not in json.dumps(report)


@pytest.mark.parametrize("reverse", [False, True])
def test_optional_back_edge_does_not_block_capability(tmp_path, reverse):
    consumer = plugin(
        tmp_path,
        "consumer",
        [dependency(required=False)],
        [capability("base"), capability("extra", ["provider"])],
    )
    provider = plugin(tmp_path, "provider", [dependency("consumer")])
    report = doctor([provider, consumer] if reverse else [consumer, provider])
    assert report["status"] == "ready"
    assert report["cycles"]
    assert all(
        cap["status"] == "ready" for e in report["plugins"] for cap in e["capabilities"]
    )


def test_ambiguous_cache_entries_list_versions_paths_and_host(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    root = plugin(tmp_path, "provider")
    cache = tmp_path / ".claude/plugins/cache/test/provider"
    first = export_plugin(root, cache / "0.1.0", ("claude",))["packages"]["claude"]
    path = root / "openstudio-si-plugin.yaml"
    data = yaml.safe_load(path.read_text())
    data["version"] = "0.2.0"
    path.write_text(yaml.safe_dump(data))
    second = export_plugin(root, cache / "0.2.0", ("claude",))["packages"]["claude"]
    report = doctor()
    assert report["status"] == "unknown"
    for entry in report["plugins"]:
        assert entry["reason"] == "ambiguous_plugin_candidates"
        assert {c["path"] for c in entry["candidates"]} == {first, second}
        assert {c["version"] for c in entry["candidates"]} == {"0.1.0", "0.2.0"}
        assert {c["host"] for c in entry["candidates"]} == {"claude"}
    assert doctor([first], host="claude")["status"] == "ready"


def test_shared_required_subgraphs_are_not_expanded_exponentially(
    tmp_path, monkeypatch
):
    from openstudio_si_harness import readiness

    roots = []
    for layer in range(6):
        deps = (
            [
                dependency(f"layer-{layer+1}-a", version_spec=">=0.1.0"),
                dependency(f"layer-{layer+1}-b", version_spec=">=0.1.0"),
            ]
            if layer < 5
            else []
        )
        for suffix in ("a", "b"):
            roots.append(plugin(tmp_path, f"layer-{layer}-{suffix}", deps))
    calls = []
    original = readiness.satisfies

    def counted(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(readiness, "satisfies", counted)
    assert doctor(roots)["status"] == "ready"
    assert len(calls) <= 4 * len(roots)
