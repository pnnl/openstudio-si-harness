"""Marketplace imports bind releases to source/ownership and preserve neighbors."""

import json
import shutil
import zipfile

import pytest
from openstudio_si_harness.export import DESCRIPTOR, export_plugin
from openstudio_si_harness.marketplace import (
    CATALOGS,
    REGISTRY,
    digest,
    load_registry,
    merge_entries,
    read_json,
    stage_release,
    unpack_package,
    validate_marketplace,
    verify_release,
    write_json,
)
from openstudio_si_harness.project_tools import package_release

from ..support.plugins import capability, dependency, plugin

SHA = "a" * 40
BASE = "b" * 40


def market(tmp_path, registrations):
    root = tmp_path / "marketplace"
    for location in CATALOGS.values():
        write_json(root / location, {"name": "test-market", "plugins": []})
    write_json(root / REGISTRY, {"schema_version": "0.1", "plugins": registrations})
    return root


def candidate(tmp_path, identity="demo", dependencies=()):
    root = plugin(tmp_path, identity, dependencies, [capability("lookup")])
    exported = export_plugin(root, tmp_path / (identity + "-export"))
    report = tmp_path / (identity + "-report.json")
    write_json(report, exported)
    record = package_release(
        root.parent, report, tag="v0.1.0", sha=SHA, repository="lab/" + identity
    )
    entry = {
        "repository": "lab/" + identity,
        "owner": "individual",
        "plugin_path": "plugin",
    }
    verified = tmp_path / (identity + "-verified")
    verify_release(
        root.parent / "dist/release/release.json",
        root.parent / "dist/release",
        root,
        identity=identity,
        entry=entry,
        source_sha=SHA,
        output=verified,
    )
    return root, entry, verified, record


def apply_overlay(proposal, root):
    shutil.copytree(proposal / "overlay", root, dirs_exist_ok=True)


def test_published_050_candidates_remain_importable_by_new_tooling(
    tmp_path, monkeypatch
):
    from openstudio_si_harness import export, project_tools

    with monkeypatch.context() as old:
        old.setattr(export, "__version__", "0.5.0")
        old.setattr(project_tools, "__version__", "0.5.0")
        source, entry, _, record = candidate(tmp_path)
    monkeypatch.setattr(export, "__version__", "0.5.1")
    assert record["tool_version"] == "0.5.0"
    record["destination"]["status"] = "importer-pending"
    write_json(source.parent / "dist/release/release.json", record)
    verify_release(
        source.parent / "dist/release/release.json",
        source.parent / "dist/release",
        source,
        identity="demo",
        entry=entry,
        source_sha=SHA,
        output=tmp_path / "new-importer",
    )


def test_verify_stage_and_idempotence_preserve_other_plugins(tmp_path):
    _, first, verified, _ = candidate(tmp_path, "first")
    _, second, verified_second, _ = candidate(tmp_path, "second")
    root = market(tmp_path, {"first": first, "second": second})
    proposal = tmp_path / "first-proposal"
    result = stage_release(root, verified, proposal, base_sha=BASE)
    assert result["status"] == "candidate"
    apply_overlay(proposal, root)
    first_bytes = (root / "first" / DESCRIPTOR).read_bytes()
    proposal = tmp_path / "second-proposal"
    stage_release(root, verified_second, proposal, base_sha=BASE)
    apply_overlay(proposal, root)
    assert validate_marketplace(root)["plugins"] == 2
    assert (root / "first" / DESCRIPTOR).read_bytes() == first_bytes
    assert (
        stage_release(root, verified_second, tmp_path / "again", base_sha=BASE)[
            "status"
        ]
        == "unchanged"
    )


@pytest.mark.parametrize("change", ["archive", "owner", "source", "package-content"])
def test_release_is_checked_against_source_not_only_uploaded_hashes(tmp_path, change):
    source, entry, _, record = candidate(tmp_path)
    assets = source.parent / "dist/release"
    if change == "archive":
        (assets / record["packages"]["claude"]["archive"]).write_bytes(b"bad")
    elif change == "owner":
        entry["owner"] = "another-owner"
    elif change == "source":
        record["source_sha"] = "c" * 40
    else:
        (source / "skills/demo/SKILL.md").write_text(
            "---\nname: demo\ndescription: Changed.\n---\nDifferent source.\n"
        )
    write_json(assets / "release.json", record)
    with pytest.raises(ValueError):
        verify_release(
            assets / "release.json",
            assets,
            source,
            identity="demo",
            entry=entry,
            source_sha=SHA,
            output=tmp_path / "reverify",
        )


@pytest.mark.parametrize(
    "name,mode",
    [
        ("../escape", 0o100644),
        ("demo/../../escape", 0o100644),
        ("demo/link", 0o120777),
        ("other/file", 0o100644),
    ],
)
def test_zip_traversal_links_and_wrong_roots_are_rejected(tmp_path, name, mode):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        item = zipfile.ZipInfo(name)
        item.external_attr = mode << 16
        bundle.writestr(item, "untrusted")
    with pytest.raises(ValueError):
        unpack_package(archive, tmp_path / "out", "demo")
    assert not (tmp_path / "escape").exists()


def test_required_dependency_must_be_present_for_both_hosts(tmp_path):
    _, entry, verified, _ = candidate(tmp_path, dependencies=[dependency("provider")])
    root = market(tmp_path, {"demo": entry})
    with pytest.raises(ValueError, match="Unsatisfied"):
        stage_release(root, verified, tmp_path / "proposal", base_sha=BASE)
    assert read_json(root / CATALOGS["claude"])["plugins"] == []


def test_registry_rejects_reserved_identity_and_owner_shape(tmp_path):
    root = market(
        tmp_path,
        {
            "openstudio-si": {
                "repository": "lab/repo",
                "owner": "lab",
                "plugin_path": "plugin",
            }
        },
    )
    with pytest.raises(ValueError, match="reserved"):
        load_registry(root)


def test_version_reuse_with_different_record_is_rejected(tmp_path):
    _, entry, verified, _ = candidate(tmp_path)
    root = market(tmp_path, {"demo": entry})
    proposal = tmp_path / "proposal"
    stage_release(root, verified, proposal, base_sha=BASE)
    apply_overlay(proposal, root)
    receipt = read_json(verified / "receipt.json")
    receipt["record"]["source_sha"] = "c" * 40
    receipt["record_sha256"] = digest(receipt["record"])
    write_json(verified / "receipt.json", receipt)
    with pytest.raises(ValueError, match="immutable"):
        stage_release(root, verified, tmp_path / "changed", base_sha=BASE)


def test_existing_catalog_entries_keep_their_data_and_order():
    original = {
        "name": "catalog",
        "plugins": [
            {"name": "neighbor", "custom": "preserve"},
            {"name": "demo", "version": "old"},
        ],
    }
    merged = merge_entries(original, [{"name": "demo", "version": "new"}])
    assert merged["plugins"][0] == original["plugins"][0]
    assert original["plugins"][1]["version"] == "old"


def legacy(root, core_version="0.5.0"):
    from openstudio_si_harness.marketplace import catalog_entry, package_path

    for host, location in CATALOGS.items():
        entries = []
        for identity, version in [
            ("openstudio-si", core_version),
            ("openstudio-si-hvac", "0.2.0"),
        ]:
            package = package_path(root, host, identity)
            write_json(
                package / f".{host}-plugin/plugin.json",
                {"name": identity, "version": version},
            )
            if identity.endswith("-hvac"):
                write_json(
                    package / "openstudio-si-plugin.json",
                    {"core_min": core_version, "core_before": "1.0.0"},
                )
            entries.append(
                catalog_entry(
                    {"id": identity, "version": version, "description": identity}, host
                )
            )
        write_json(root / location, {"name": "legacy-market", "plugins": entries})


def test_core_sync_preserves_registered_plugins_and_registry(tmp_path):
    from openstudio_si_harness.marketplace import sync_core

    _, entry, verified, _ = candidate(tmp_path)
    root = market(tmp_path, {"demo": entry})
    legacy(root)
    stage_release(root, verified, tmp_path / "proposal", base_sha=BASE)
    apply_overlay(tmp_path / "proposal", root)
    before = (root / "demo" / DESCRIPTOR).read_bytes()
    registry = (root / REGISTRY).read_bytes()
    generated = tmp_path / "core-export"
    legacy(generated, "0.5.1")
    for name in ["README.md", "INSTALL.claude.md", "INSTALL.codex.md"]:
        (root / name).write_text("Maintainer-owned lab listing\n")
        (generated / name).write_text("Generated core installation instructions\n")
    result = sync_core(generated, root)
    assert result["validation"]["plugins"] == 3
    assert (root / "demo" / DESCRIPTOR).read_bytes() == before
    assert (root / REGISTRY).read_bytes() == registry
    for name in ["README.md", "INSTALL.claude.md", "INSTALL.codex.md"]:
        assert (root / name).read_text() == "Maintainer-owned lab listing\n"
        assert (
            root / "docs/core" / name
        ).read_text() == "Generated core installation instructions\n"
    assert {
        entry["name"] for entry in read_json(root / CATALOGS["claude"])["plugins"]
    } == {"demo", "openstudio-si", "openstudio-si-hvac"}


def test_core_update_cannot_break_existing_required_dependency(tmp_path):
    from openstudio_si_harness.marketplace import sync_core

    _, entry, verified, _ = candidate(
        tmp_path,
        dependencies=[
            dependency("core", target="openstudio-si", version_spec=">=0.5.0 <0.6.0")
        ],
    )
    root = market(tmp_path, {"demo": entry})
    legacy(root)
    stage_release(root, verified, tmp_path / "proposal", base_sha=BASE)
    apply_overlay(tmp_path / "proposal", root)
    before = (root / CATALOGS["claude"]).read_bytes()
    generated = tmp_path / "core-export"
    legacy(generated, "0.6.0")
    with pytest.raises(ValueError, match="Unsatisfied"):
        sync_core(generated, root)
    assert (root / CATALOGS["claude"]).read_bytes() == before
    registration = read_json(root / REGISTRY)
    registration["plugins"]["demo"]["enabled"] = False
    write_json(root / REGISTRY, registration)
    with pytest.raises(ValueError, match="Unsatisfied"):
        sync_core(generated, root)
    # Reviewed withdrawal removes both catalog entries and the current payloads;
    # disabling polling alone must never hide a catalog incompatibility.
    for location in CATALOGS.values():
        catalog = read_json(root / location)
        catalog["plugins"] = [p for p in catalog["plugins"] if p["name"] != "demo"]
        write_json(root / location, catalog)
    for name in ["demo", "plugins/demo"]:
        shutil.rmtree(root / name)
    (root / ".openstudio-si/releases/demo.json").unlink()
    assert sync_core(generated, root)["validation"]["plugins"] == 2


def test_required_cycles_are_rejected_across_the_complete_catalog(tmp_path):
    from openstudio_si_harness.marketplace import catalog_entry, package_path

    root = market(tmp_path, {})
    for identity, provider in [("first", "second"), ("second", "first")]:
        source = plugin(tmp_path, identity, [dependency(provider)])
        exports = export_plugin(source, tmp_path / (identity + "-export"))
        for host, path in exports["packages"].items():
            destination = package_path(root, host, identity)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(path, destination)
            manifest = read_json(destination / DESCRIPTOR)["manifest"]
            write_json(
                root / CATALOGS[host],
                merge_entries(
                    read_json(root / CATALOGS[host]), [catalog_entry(manifest, host)]
                ),
            )
    with pytest.raises(ValueError, match="cycle"):
        validate_marketplace(root, check_receipts=False)


def test_malformed_release_identity_returns_validation_error(tmp_path):
    source, entry, _, record = candidate(tmp_path)
    record["repository"] = {"invalid": True}
    write_json(source.parent / "dist/release/release.json", record)
    with pytest.raises(ValueError, match="strings"):
        verify_release(
            source.parent / "dist/release/release.json",
            source.parent / "dist/release",
            source,
            identity="demo",
            entry=entry,
            source_sha=SHA,
            output=tmp_path / "invalid",
        )


def test_cli_exposes_all_marketplace_commands(tmp_path, capsys):
    from openstudio_si_harness.cli import main

    root = market(tmp_path, {})
    assert main(["plugin", "validate-marketplace", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["plugins"] == 0
    for action in ["marketplace-poll", "validate-marketplace", "sync-core-marketplace"]:
        with pytest.raises(SystemExit) as code:
            main(["plugin", action, "--help"])
        assert code.value.code == 0
