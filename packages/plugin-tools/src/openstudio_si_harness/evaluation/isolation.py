"""Agent-visible packages and holdout-access checks; no model calls."""

import json
import re
import shutil
import tempfile
import unicodedata
from pathlib import Path

from ..export import DESCRIPTOR, export_plugin
from .contracts import digest, inside


def verify_source_export(source, source_info, packages, host):
    selected = packages.get(source_info["manifest"]["id"])
    if not selected:
        return  # A configured baseline may intentionally omit the plugin under test.
    with tempfile.TemporaryDirectory(prefix="si-eval-source-check-") as directory:
        exported = export_plugin(source, Path(directory) / "export", hosts=(host,))
        fresh = json.loads((Path(exported["packages"][host]) / DESCRIPTOR).read_text())
    if selected["manifest"] != fresh["manifest"] or selected["files"] != fresh["files"]:
        raise ValueError(
            "Selected export is stale or does not match the evaluated source; re-export after any source or case edit."
        )


def evaluation_paths(package):
    """All declared cases/references, including those belonging to sibling plugins."""
    root = package["path"]
    cases = Path(package["manifest"]["evaluation"]["cases"])
    directories = {"evals", cases.as_posix()}
    if cases.name == "cases" and cases.parent != Path("."):
        directories.add(cases.parent.as_posix())
    references = set()
    for file in (root / cases).glob("*.json"):
        value = json.loads(file.read_text())
        for case in value if isinstance(value, list) else [value]:
            ref = case.get("expected", {}).get("reference")
            if ref:
                inside(root, ref)
                references.add(ref)
    return sorted(directories), sorted(references)


def agent_view(package, target, *, withheld_files=()):
    """Copy a verified subset without cases, reviewed answers or export descriptors."""
    directories, references = evaluation_paths(package)
    references = set(references) | set(withheld_files)
    excluded = []
    hashes = {}
    for relative, expected in package["files"].items():
        path = Path(relative)
        if relative in references or any(
            path.is_relative_to(Path(folder)) for folder in directories
        ):
            excluded.append(relative)
            continue
        data = inside(package["path"], relative).read_bytes()
        if digest(data) != expected:
            raise ValueError("Selected export changed while staging: " + relative)
        destination = target / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        shutil.copymode(package["path"] / relative, destination)
        hashes[relative] = digest(destination.read_bytes())
    return {
        "files": hashes,
        "withheld": excluded,
    }


def holdout_access(trace, packages, source, cases, output, additional_references=()):
    """Fail explicit holdout path access. This is an audit, not an OS sandbox."""
    forbidden_roots = [source.resolve(), cases.resolve(), output.resolve()]
    forbidden_relative = {"evals"}
    references = set(additional_references)
    for package in packages.values():
        directories, paths = evaluation_paths(package)
        forbidden_relative.update(directories)
        references.update(paths)
    reasons = []

    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for child in value.values():
                yield from strings(child)
        elif isinstance(value, list):
            for child in value:
                yield from strings(child)

    for row in trace:
        for text in strings(row.get("arguments")):
            normalized = text.replace("\\", "/")
            if any(str(root) in normalized for root in forbidden_roots) or any(
                re.search(
                    r"(?<![A-Za-z0-9_-])" + re.escape(path) + r"(?:/|\b)", normalized
                )
                for path in forbidden_relative | references
            ):
                reasons.append(row["id"])
                break
    return sorted(set(reasons))


def external_model_paths(trace, workspace, home):
    roots = (workspace.resolve(), home.resolve())
    outside = []
    for row in trace:
        if row.get("provider") == "agent":
            continue
        value = json.dumps(row.get("result"), ensure_ascii=False)
        for match in re.findall(r'(/[^\n\r"<>]+?\.(?:osm|sql|idf))\b', value):
            path = Path(match).resolve()
            if not any(path.is_relative_to(root) for root in roots):
                outside.append(str(path))
    return sorted(set(outside))


def holdout_content(trace, answer, references, hashes=(), *, public_texts=()):
    """Conservative contamination indicators, not proof of where text was obtained.

    Tool results containing a whole private reference or a strict majority of its
    substantial lines block judging. Other overlaps remain advisory. Public text
    is excluded; answer-only matches never block.
    """

    def normalize(value, *, tool_output=False):
        lines = []
        for line in unicodedata.normalize("NFKC", value).splitlines():
            # Grep's path:N: / path-N- and Read/cat's N-tab / N-arrow columns.
            if tool_output:
                line = re.sub(r"^\s*[^\r\n]+?[:\-]\d+[:\-][ \t]*", "", line)
                line = re.sub(r"^\s*\d+(?:\t+|[ \t]*[→│|][ \t]*)", "", line)
            lines.append(line)
        return " ".join(" ".join(lines).casefold().split())

    public = [normalize(value) for value in public_texts]
    private = []
    for value in references:
        whole = normalize(value)
        if len(whole) < 32 or any(whole in text for text in public):
            continue
        private_lines = {
            line
            for raw in value.splitlines()
            if (line := normalize(raw)) and not any(line in text for text in public)
        }
        if not private_lines:
            continue
        lines = {line for line in private_lines if len(line) >= 32}
        private.append((whole, lines))
    needles = {text for whole, lines in private for text in (whole, *lines)}
    hashes = [value for value in hashes if not any(value in text for text in public)]

    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for child in value.values():
                yield from strings(child)
        elif isinstance(value, list):
            for child in value:
                yield from strings(child)

    tools = {
        row["id"]: normalize("\n".join(strings(row.get("result"))), tool_output=True)
        for row in trace
    }
    blocking = set()
    for whole, lines in private:
        blocking.update(identity for identity, text in tools.items() if whole in text)
        matched = {
            line for line in lines if any(line in text for text in tools.values())
        }
        if len(matched) >= 2 and len(matched) * 2 > len(lines):
            blocking.update(
                identity
                for identity, text in tools.items()
                if any(line in text for line in matched)
            )
    return [
        {"location": identity + ":holdout_content", "blocking": identity in blocking}
        for identity, text in [("answer", normalize(answer)), *tools.items()]
        if any(needle in text for needle in needles)
        or any(value in text for value in hashes)
    ]
