"""Central polling of registered public GitHub releases; never publish remotely."""

import contextlib
import hashlib
import http.client
import json
import os
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

from .content import ROOT_IGNORED, excluded
from .marketplace import (
    DESCRIPTOR,
    HOSTS,
    MAX_ARCHIVE,
    MAX_EXPANDED,
    MAX_FILES,
    RECEIPTS,
    SHA,
    ReleaseError,
    digest,
    load_registry,
    read_json,
    stage_release,
    validate_export,
    validate_marketplace,
    validate_record,
    verify_release,
    write_json,
)


class GitHub:
    """Only registered GitHub metadata and constructed public asset/source URLs."""

    def __init__(self, token=None):
        self.token = os.environ.get("GITHUB_TOKEN", "") if token is None else token

    def metadata(self, repository, suffix):
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "openstudio-si-marketplace",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(
            "https://api.github.com/repos/" + repository + suffix, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read(4 * 1024 * 1024 + 1)
            if len(payload) > 4 * 1024 * 1024:
                raise ValueError("GitHub metadata exceeds the response limit.")
            data = json.loads(payload)
            if not isinstance(data, dict):
                raise ReleaseError(
                    "invalid_metadata", "GitHub metadata must be an object."
                )
            return data
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise ReleaseError(
                "metadata_failed", "GitHub metadata request failed."
            ) from None
        except (
            urllib.error.URLError,
            TimeoutError,
            http.client.HTTPException,
            json.JSONDecodeError,
        ):
            raise ReleaseError(
                "metadata_failed", "GitHub metadata request failed."
            ) from None

    @staticmethod
    def download(url, target, limit):
        # Binary URLs are constructed here, never taken from release.json.
        # No API credential is forwarded to GitHub/CDN redirects.
        try:
            with (
                urllib.request.urlopen(
                    urllib.request.Request(
                        url, headers={"User-Agent": "openstudio-si-marketplace"}
                    ),
                    timeout=60,
                ) as response,
                Path(target).open("xb") as stream,
            ):
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > limit:
                        raise ReleaseError(
                            "download_limit", "Download exceeds the configured limit."
                        )
                    stream.write(chunk)
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException):
            raise ReleaseError(
                "download_failed", "Public GitHub asset/source download failed."
            ) from None

    def asset(self, repository, tag, name, target, limit):
        url = (
            "https://github.com/"
            + repository
            + "/releases/download/"
            + urllib.parse.quote(tag, safe="")
            + "/"
            + urllib.parse.quote(name, safe="")
        )
        self.download(url, target, limit)

    def source(self, repository, sha, target):
        self.download(
            "https://codeload.github.com/" + repository + "/tar.gz/" + sha,
            target,
            64 * 1024 * 1024,
        )


def extract_source(archive, plugin_path, destination):
    """Extract only the registered plugin subtree; never extract credentials/links."""
    destination = Path(destination)
    parts = PurePosixPath(plugin_path).parts
    prefix, total, count, seen = None, 0, 0, set()
    with tarfile.open(archive, "r:gz") as source:
        for index, member in enumerate(source):
            if index > 100000:
                raise ValueError("Source archive has too many entries.")
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or "\\" in member.name:
                raise ValueError("Unsafe source archive path.")
            if not path.parts:
                continue
            if prefix is None:
                prefix = path.parts[0]
            if path.parts[0] != prefix:
                raise ValueError("Source archive needs one top-level directory.")
            inner = path.parts[1:]
            if inner[: len(parts)] != parts:
                continue
            relative = inner[len(parts) :]
            if (
                not relative
                or relative[0] in ROOT_IGNORED
                or any(excluded(part) for part in relative)
            ):
                continue
            name = Path(*relative)
            if name in seen or (not member.isfile() and not member.isdir()):
                raise ValueError(
                    "Duplicate source paths, links or special files are forbidden."
                )
            seen.add(name)
            target = destination / name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            count += 1
            total += member.size
            if count > MAX_FILES or total > MAX_EXPANDED:
                raise ValueError("Source plugin exceeds the size/count limit.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as stream, target.open("xb") as output:
                while chunk := stream.read(1024 * 1024):
                    output.write(chunk)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    if not (destination / "openstudio-si-plugin.yaml").is_file():
        raise ValueError("Registered source plugin path has no manifest.")
    return destination


@contextlib.contextmanager
def native_environment(home):
    """Host readers see a clean home and no GitHub/model credentials."""
    original = dict(os.environ)
    allowed = {
        "PATH",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    }
    try:
        os.environ.clear()
        os.environ.update(
            {name: value for name, value in original.items() if name in allowed}
        )
        os.environ["HOME"] = str(home)
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def poll(
    marketplace,
    output,
    *,
    base_sha,
    plugin_id=None,
    native_hosts=False,
    client=None,
    host_checker=None,
):
    """Prepare one isolated PR proposal per new release; contain per-source errors."""
    from .project_tools import check_hosts

    marketplace, output = Path(marketplace), Path(output)
    registry = load_registry(marketplace)["plugins"]
    validate_marketplace(marketplace)
    if not SHA.fullmatch(base_sha) or (plugin_id and plugin_id not in registry):
        raise ValueError(
            "A full marketplace commit and registered plugin selection are required."
        )
    output.mkdir(parents=True, exist_ok=False)
    client = client or GitHub()
    host_checker = host_checker or check_hosts
    results = []
    for identity, entry in registry.items():
        if plugin_id and identity != plugin_id:
            continue
        if not entry.get("enabled", True):
            results.append({"id": identity, "status": "disabled"})
            continue
        stage = "release_metadata"
        try:
            release = client.metadata(entry["repository"], "/releases/latest")
            if release is not None and not isinstance(release, dict):
                raise ReleaseError(
                    "invalid_metadata", "Release metadata must be an object."
                )
            if release is None or release.get("draft") or release.get("prerelease"):
                results.append({"id": identity, "status": "no_stable_release"})
                continue
            tag = release["tag_name"]
            if not isinstance(tag, str) or not isinstance(
                release.get("assets", []), list
            ):
                raise ReleaseError("invalid_metadata", "Invalid release tag/assets.")
            if any(
                not isinstance(asset, dict) or not isinstance(asset.get("name"), str)
                for asset in release.get("assets", [])
            ):
                raise ReleaseError(
                    "invalid_metadata", "Invalid release asset metadata."
                )
            names = {asset["name"] for asset in release.get("assets", [])}
            if "release.json" not in names:
                results.append({"id": identity, "status": "awaiting_assets"})
                continue
            with tempfile.TemporaryDirectory(prefix="si-github-release-") as temporary:
                work = Path(temporary)
                stage = "release_record"
                client.asset(
                    entry["repository"],
                    tag,
                    "release.json",
                    work / "release.json",
                    1024 * 1024,
                )
                record = read_json(work / "release.json")
                stage = "tag_resolution"
                commit = client.metadata(
                    entry["repository"], "/commits/" + urllib.parse.quote(tag, safe="")
                )
                if commit is not None and not isinstance(commit, dict):
                    raise ReleaseError(
                        "invalid_metadata", "Commit metadata must be an object."
                    )
                if not commit or not isinstance(commit.get("sha"), str):
                    raise ReleaseError(
                        "tag_commit",
                        "Release tag cannot be resolved to an immutable commit.",
                    )
                stage = "release_record"
                validate_record(record, identity, entry, commit["sha"])
                if record["tag"] != tag:
                    raise ReleaseError(
                        "tag_commit", "Published release tag does not match its record."
                    )
                approved = marketplace / RECEIPTS / (identity + ".json")
                if approved.exists():
                    previous = read_json(approved)
                    if previous["record"]["version"] == record["version"]:
                        if previous["record_sha256"] != digest(record):
                            raise ReleaseError(
                                "immutable_version",
                                "Released versions are immutable; bump the plugin version.",
                            )
                        results.append(
                            {
                                "id": identity,
                                "status": "unchanged",
                                "version": record["version"],
                                "native_hosts_verified": False,
                                "previously_reviewed": True,
                            }
                        )
                        continue
                if any(
                    item["archive"] not in names for item in record["packages"].values()
                ):
                    results.append({"id": identity, "status": "awaiting_assets"})
                    continue
                stage = "asset_download"
                for item in record["packages"].values():
                    client.asset(
                        entry["repository"],
                        tag,
                        item["archive"],
                        work / item["archive"],
                        MAX_ARCHIVE,
                    )
                stage = "source_download"
                client.source(
                    entry["repository"], commit["sha"], work / "source.tar.gz"
                )
                stage = "immutable_source_verification"
                source = extract_source(
                    work / "source.tar.gz", entry["plugin_path"], work / "source"
                )
                verified = work / "verified"
                verify_release(
                    work / "release.json",
                    work,
                    source,
                    identity=identity,
                    entry=entry,
                    source_sha=commit["sha"],
                    output=verified,
                )
                receipt = read_json(verified / "receipt.json")
                modes = {
                    path.relative_to(verified).as_posix(): bool(
                        path.stat().st_mode & 0o111
                    )
                    for host in HOSTS
                    for path in (verified / host / identity).rglob("*")
                    if path.is_file()
                }
                if native_hosts:
                    stage = "native_host_conformance"
                    home = work / "host-home"
                    home.mkdir()
                    with native_environment(home):
                        host_checker(verified / "export-report.json")
                # Native readers have no write credentials. Still bind their
                # output back to the pre-check source/receipt held in memory.
                if read_json(verified / "receipt.json") != receipt:
                    raise ValueError("Native conformance changed the verified receipt.")
                for host in HOSTS:
                    package = verified / host / identity
                    validate_export(package)
                    if (
                        hashlib.sha256((package / DESCRIPTOR).read_bytes()).hexdigest()
                        != record["packages"][host]["descriptor_sha256"]
                    ):
                        raise ValueError(
                            "Native conformance changed the verified package."
                        )
                if modes != {
                    path.relative_to(verified).as_posix(): bool(
                        path.stat().st_mode & 0o111
                    )
                    for host in HOSTS
                    for path in (verified / host / identity).rglob("*")
                    if path.is_file()
                }:
                    raise ValueError(
                        "Native conformance changed executable permissions."
                    )
                stage = "complete_catalog_validation"
                result = stage_release(
                    marketplace, verified, output / identity, base_sha=base_sha
                )
                if result["status"] == "candidate":
                    plan = read_json(output / identity / "plan.json")
                    plan["native_hosts_verified"] = native_hosts
                    write_json(output / identity / "plan.json", plan)
                results.append(
                    {
                        "id": identity,
                        "status": result["status"],
                        "version": record["version"],
                        "native_hosts_verified": native_hosts,
                    }
                )
        # Deliberate source boundary: keep later plugins running for any ordinary
        # failure, while allowing KeyboardInterrupt/SystemExit to propagate.
        except Exception as exc:  # noqa: BLE001
            # Do not echo HTTP bodies, raw host stderr or untrusted record text.
            shutil.rmtree(output / identity, ignore_errors=True)
            defaults = {
                "release_metadata": "invalid_metadata",
                "release_record": "invalid_record",
                "tag_resolution": "tag_commit",
                "asset_download": "download_failed",
                "source_download": "download_failed",
                "immutable_source_verification": "source_verification_failed",
                "native_host_conformance": "native_hosts_failed",
                "complete_catalog_validation": "catalog_validation_failed",
            }
            reason = defaults[stage]
            if isinstance(exc, ReleaseError):
                reason = exc.code
            elif isinstance(exc, http.client.HTTPException):
                reason = (
                    "metadata_failed"
                    if stage in {"release_metadata", "tag_resolution"}
                    else "download_failed"
                )
            results.append(
                {
                    "id": identity,
                    "status": "error",
                    "stage": stage,
                    "reason": reason,
                }
            )
    summary = {
        "format": "openstudio-si-release-poll/0.1",
        "base_sha": base_sha,
        "results": results,
        "has_errors": any(item["status"] == "error" for item in results),
    }
    write_json(output / "summary.json", summary)
    return summary
