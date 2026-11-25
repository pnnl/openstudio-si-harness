"""A release is read from its registered source, not from supplied download URLs."""

import http.client
import io
import os
import shutil
import tarfile
from pathlib import Path

import pytest
import yaml
from openstudio_si_harness.github_releases import GitHub, extract_source, poll
from openstudio_si_harness.marketplace import ReleaseError, read_json, write_json

from .test_marketplace import BASE, SHA, candidate, market


class GitHubFixture:
    def __init__(self, source, record):
        self.source_plugin = source
        self.record = record
        self.assets = source.parent / "dist/release"
        self.resolved_sha = SHA

    def metadata(self, repository, suffix):
        if repository != self.record["repository"]:
            raise ValueError("Fixture source is unavailable.")
        if suffix == "/releases/latest":
            return {
                "tag_name": self.record["tag"],
                "draft": False,
                "prerelease": False,
                "assets": [
                    {"name": name}
                    for name in [
                        "release.json",
                        *[item["archive"] for item in self.record["packages"].values()],
                    ]
                ],
            }
        return {"sha": self.resolved_sha}

    def asset(self, repository, tag, name, target, limit):
        assert repository == self.record["repository"] and tag == self.record["tag"]
        shutil.copyfile(self.assets / name, target)

    def source(self, repository, sha, target):
        assert sha == self.resolved_sha
        with tarfile.open(target, "w:gz") as archive:
            archive.add(self.source_plugin, arcname="repository-commit/plugin")


def test_poll_verifies_source_and_native_hosts_without_credentials(
    tmp_path, monkeypatch
):
    source, entry, _, record = candidate(tmp_path)
    root = market(tmp_path, {"demo": entry})
    monkeypatch.setenv("GITHUB_TOKEN", "fake-central-read-token")
    monkeypatch.setenv("FAKE_MODEL_API_KEY", "not-for-host-reader")
    seen = []

    def native(report):
        assert (
            "GITHUB_TOKEN" not in os.environ and "FAKE_MODEL_API_KEY" not in os.environ
        )
        assert Path(os.environ["HOME"]).name == "host-home"
        seen.append(read_json(report)["id"])

    result = poll(
        root,
        tmp_path / "proposals",
        base_sha=BASE,
        native_hosts=True,
        client=GitHubFixture(source, record),
        host_checker=native,
    )
    assert result["results"][0]["status"] == "candidate" and seen == ["demo"]
    assert read_json(tmp_path / "proposals/demo/plan.json")["native_hosts_verified"]
    assert os.environ["GITHUB_TOKEN"] == "fake-central-read-token"


def test_tag_source_mismatch_never_emits_a_plan(tmp_path):
    source, entry, _, record = candidate(tmp_path)
    root = market(tmp_path, {"demo": entry})
    client = GitHubFixture(source, record)
    client.resolved_sha = "c" * 40
    result = poll(root, tmp_path / "proposals", base_sha=BASE, client=client)
    assert result["has_errors"] and not (tmp_path / "proposals/demo/plan.json").exists()


def test_one_failed_source_does_not_hide_another_release(tmp_path):
    source, entry, _, record = candidate(tmp_path)
    root = market(
        tmp_path, {"bad": {**entry, "repository": "missing/repo"}, "demo": entry}
    )
    result = poll(
        root,
        tmp_path / "proposals",
        base_sha=BASE,
        client=GitHubFixture(source, record),
    )
    assert result["results"][0]["status"] == "error"
    assert result["results"][1]["status"] == "candidate"


@pytest.mark.parametrize(
    "failure", ["release", "commit", "asset", "source", "unexpected"]
)
def test_flaky_source_still_writes_summary_and_checks_later_plugins(tmp_path, failure):
    fixtures, entries = {}, {}
    for identity in ("bad", "demo"):
        source, entry, _, record = candidate(tmp_path, identity)
        entries[identity] = entry
        fixtures[entry["repository"]] = GitHubFixture(source, record)
    root = market(tmp_path, entries)

    class Mixed:
        def metadata(self, repository, suffix):
            if repository == "lab/bad" and (
                (failure == "release" and suffix == "/releases/latest")
                or (failure == "commit" and suffix != "/releases/latest")
            ):
                return ["untrusted response body"]
            return fixtures[repository].metadata(repository, suffix)

        def asset(self, repository, tag, name, target, limit):
            if repository == "lab/bad" and failure == "asset":
                raise http.client.IncompleteRead(b"untrusted response body", 100)
            fixtures[repository].asset(repository, tag, name, target, limit)

        def source(self, repository, sha, target):
            if repository == "lab/bad":
                if failure == "source":
                    raise http.client.IncompleteRead(b"untrusted response body", 100)
                if failure == "unexpected":
                    raise RuntimeError("untrusted response body")
            fixtures[repository].source(repository, sha, target)

    output = tmp_path / "proposals"
    result = poll(root, output, base_sha=BASE, client=Mixed())
    assert result == read_json(output / "summary.json")
    assert [item["status"] for item in result["results"]] == ["error", "candidate"]
    assert result["results"][0]["reason"] == (
        "invalid_metadata" if failure in {"release", "commit"} else "download_failed"
    )
    assert "untrusted response body" not in (output / "summary.json").read_text()
    assert not (output / "bad/plan.json").exists()


@pytest.mark.parametrize("operation", ["metadata", "download"])
def test_http_incomplete_read_has_a_fixed_diagnostic(tmp_path, monkeypatch, operation):
    import urllib.request

    class Truncated:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            raise http.client.IncompleteRead(b"private response", 50)

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: Truncated())
    with pytest.raises(ReleaseError) as error:
        if operation == "metadata":
            GitHub(token="").metadata("lab/demo", "/releases/latest")
        else:
            GitHub.download("https://example.invalid/asset", tmp_path / "asset", 100)
    assert error.value.code == (
        "metadata_failed" if operation == "metadata" else "download_failed"
    )
    assert "private response" not in str(error.value)


@pytest.mark.parametrize("version", ["0.1.0-rc.1", "0.1.0+build.1"])
def test_github_stable_flag_does_not_override_semver(tmp_path, version):
    from openstudio_si_harness.export import export_plugin
    from openstudio_si_harness.project_tools import package_release

    from ..support.plugins import capability, plugin
    from .test_marketplace import SHA

    source = plugin(tmp_path, "demo", (), [capability("lookup")])
    manifest = source / "openstudio-si-plugin.yaml"
    data = yaml.safe_load(manifest.read_text())
    data["version"] = version
    manifest.write_text(yaml.safe_dump(data))
    report = tmp_path / "export.json"
    write_json(report, export_plugin(source, tmp_path / "export"))
    record = package_release(
        source.parent, report, tag="v" + version, sha=SHA, repository="lab/demo"
    )
    entry = {"repository": "lab/demo", "owner": "individual", "plugin_path": "plugin"}
    root = market(tmp_path, {"demo": entry})
    result = poll(
        root, tmp_path / "out", base_sha=BASE, client=GitHubFixture(source, record)
    )
    item = result["results"][0]
    if "-rc" in version:
        assert item["status"] == "error" and item["reason"] == "prerelease_unsupported"
        assert not (tmp_path / "out/demo/plan.json").exists()
    else:
        assert item["status"] == "candidate"


def test_source_mismatch_has_a_fixed_reason(tmp_path):
    source, entry, _, record = candidate(tmp_path)
    skill = next(source.glob("skills/*/SKILL.md"))
    skill.write_text(skill.read_text() + "\nChanged after export.\n")
    root = market(tmp_path, {"demo": entry})
    result = poll(
        root, tmp_path / "out", base_sha=BASE, client=GitHubFixture(source, record)
    )
    assert result["results"][0]["reason"] == "source_mismatch"


@pytest.mark.parametrize(
    "name,kind", [("root/plugin/../../escape", "file"), ("root/plugin/link", "symlink")]
)
def test_source_archive_cannot_escape_or_supply_links(tmp_path, name, kind):
    path = tmp_path / "source.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        item = tarfile.TarInfo(name)
        if kind == "symlink":
            item.type = tarfile.SYMTYPE
            item.linkname = "../../escape"
            archive.addfile(item)
        else:
            item.size = 4
            archive.addfile(item, io.BytesIO(b"data"))
    with pytest.raises(ValueError):
        extract_source(path, "plugin", tmp_path / "extracted")
    assert not (tmp_path / "escape").exists()


def test_native_reader_cannot_replace_the_verified_receipt(tmp_path):
    source, entry, _, record = candidate(tmp_path)
    root = market(tmp_path, {"demo": entry})

    def mutate(report):
        receipt = Path(report).parent / "receipt.json"
        data = read_json(receipt)
        data["record"]["source_sha"] = "c" * 40
        write_json(receipt, data)

    result = poll(
        root,
        tmp_path / "proposals",
        base_sha=BASE,
        native_hosts=True,
        client=GitHubFixture(source, record),
        host_checker=mutate,
    )
    assert result["results"][0]["status"] == "error"
    assert not (tmp_path / "proposals/demo/plan.json").exists()


def test_disabled_registration_does_not_contact_github(tmp_path):
    source, entry, _, record = candidate(tmp_path)
    root = market(tmp_path, {"demo": {**entry, "enabled": False}})
    result = poll(
        root,
        tmp_path / "proposals",
        base_sha=BASE,
        client=GitHubFixture(source, record),
    )
    assert result["results"] == [{"id": "demo", "status": "disabled"}]


def test_approved_identical_release_does_not_redownload_packages(tmp_path):
    from .test_marketplace import apply_overlay

    source, entry, _, record = candidate(tmp_path)
    root = market(tmp_path, {"demo": entry})
    first = tmp_path / "first"
    client = GitHubFixture(source, record)
    poll(root, first, base_sha=BASE, client=client)
    apply_overlay(first / "demo", root)
    original = client.asset

    def only_record(repository, tag, name, target, limit):
        assert name == "release.json"
        original(repository, tag, name, target, limit)

    client.asset = only_record
    result = poll(root, tmp_path / "again", base_sha=BASE, client=client)
    assert result["results"][0]["status"] == "unchanged"
