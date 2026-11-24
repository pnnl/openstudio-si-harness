"""Select package files before reading their contents or following any links."""

from pathlib import Path, PurePosixPath

DEFAULTS = (
    "skills",
    "scripts",
    "references",
    "assets",
    "agents",
    "contracts",
    "bin",
    "sdk",
    "hooks",
    "commands",
    "README.md",
    "LICENSE",
    "LICENSE.md",
    "NOTICE",
    "settings.json",
    "AGENTS.md",
    "CLAUDE.md",
)
ROOT_IGNORED = {"venv", "build", "dist"}
CREDENTIAL_NAMES = {
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    ".npmrc",
    ".netrc",
    "credentials.json",
}

IGNORED = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".DS_Store",
    ".env",
    ".envrc",
    ".secrets",
}


def excluded(name: str) -> bool:
    return (
        name in IGNORED
        or name.lower() in CREDENTIAL_NAMES
        or name.startswith(".env.")
        or name.lower().endswith((".pyc", ".pyo", ".pem", ".key", ".p12", ".pfx"))
    )


def safe_relative(name: str) -> Path:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in name
        or path.as_posix() != name
        or name == "."
    ):
        raise ValueError(f"Expected a relative package path: {name}")
    if path.parts[0] in ROOT_IGNORED or any(excluded(part) for part in path.parts):
        raise ValueError(f"Excluded package path: {name}")
    return Path(name)


def selected_files(root: Path, manifest: dict) -> list[Path]:
    includes = manifest.get("package", {}).get("include", [])
    paths = list(DEFAULTS) + [manifest["evaluation"]["cases"]] + includes
    covered = {Path(name).parts[0] for name in paths}
    development = {
        "tests",
        ".github",
        ".agents",
        ".codex",
        ".claude-plugin",
        ".codex-plugin",
        "openstudio-si-plugin.yaml",
        ".mcp.json",
        "pyproject.toml",
        "uv.lock",
        "poetry.lock",
        "package.json",
        "package-lock.json",
        ".gitignore",
        ".gitattributes",
        ".editorconfig",
        ".python-version",
    }
    for path in root.iterdir():
        if path.name not in covered | development | ROOT_IGNORED and not excluded(
            path.name
        ):
            raise ValueError(f"Declare resource path in package.include: {path.name}")
    result = set()

    def visit(path):
        if excluded(path.name):
            return
        if path.is_symlink():
            raise ValueError(f"Package contains a symlink: {path.relative_to(root)}")
        if path.is_dir():
            for child in sorted(path.iterdir()):
                visit(child)
        elif path.is_file():
            result.add(path.relative_to(root))

    for name in paths:
        relative = safe_relative(name)
        for part in (relative, *relative.parents):
            if (root / part).is_symlink():
                raise ValueError(f"Package contains a symlink: {part}")
        if name in includes and not (root / relative).exists():
            raise ValueError(f"Included resource does not exist: {name}")
        visit(root / relative)
    # These inputs are read too, so apply the same link policy.
    for name in ("openstudio-si-plugin.yaml", ".mcp.json"):
        if (root / name).is_symlink():
            raise ValueError(f"Package contains a symlink: {name}")
    return sorted(result)
