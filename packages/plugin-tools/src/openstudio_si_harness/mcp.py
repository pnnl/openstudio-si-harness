"""Portable installed-command stdio declarations and Unix desktop launchers.

Source configuration has no host-specific root substitution. Bundled executable
MCP servers require a future host binding and are rejected rather than guessed.
"""

import re
import shlex

ENV_REF = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
COMMAND = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


def override_variable(name: str) -> str:
    return "OPENSTUDIO_SI_" + re.sub(r"[^A-Z0-9]", "_", name.upper()) + "_COMMAND"


def validate_server(name: str, entry: dict) -> None:
    if not COMMAND.fullmatch(name):
        raise ValueError(
            "MCP server names must use letters, digits, underscores or hyphens."
        )
    if set(entry) - {"command", "args", "env", "type"}:
        raise ValueError(f"MCP server {name} has unsupported fields.")
    if not COMMAND.fullmatch(entry["command"]):
        raise ValueError(
            f"MCP server {name} requires an installed command name, without absolute paths or host tokens."
        )
    for value in entry.get("args", []):
        if (
            "$" in value
            or "\\" in value
            or value.startswith(("/", "~"))
            or re.fullmatch(
                r"(?i)--?(?:access[-_]?token|auth[-_]?token|token|password|secret|api[-_]?key)(?:=.*)?",
                value,
            )
            or "=/" in value
            or "=~" in value
        ):
            raise ValueError(
                f"MCP server {name} has a nonportable or credential argument. Use environment references for credentials."
            )
    for key, value in entry.get("env", {}).items():
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key) or not ENV_REF.fullmatch(value):
            raise ValueError(
                f"MCP server {name} environment values must be explicit ${{VARIABLE}} references, never literals."
            )


def host_servers(servers: dict) -> dict:
    result = {}
    for name, entry in servers.items():
        override = override_variable(name)
        # No eval, shell interpolation of source strings, or host expansion needed.
        # A user can set an executable override in the desktop process environment.
        lines = [
            "set -eu",
            f'executable="${{{override}:-}}"',
            'if [ -n "$executable" ]; then',
            '  case "$executable" in /*) ;; *) echo "MCP command override must be absolute" >&2; exit 127;; esac',
            "else",
            f"  executable=$(command -v {shlex.quote(entry['command'])} || true)",
            '  if [ -z "$executable" ]; then',
            '    for directory in "${HOME:-}/.local/bin" "${HOME:-}/.cargo/bin" /opt/homebrew/bin /usr/local/bin; do',
            f'      if [ -x "$directory"/{shlex.quote(entry["command"])} ]; then executable="$directory"/{shlex.quote(entry["command"])}; break; fi',
            "    done",
            "  fi",
            "fi",
            f'[ -n "$executable" ] && [ -x "$executable" ] || {{ echo "Install {entry["command"]} or set {override} to its executable path" >&2; exit 127; }}',
        ]
        for key, value in entry.get("env", {}).items():
            variable = ENV_REF.fullmatch(value)[1]
            lines.append(
                f'export {key}="${{{variable}:?Required MCP environment variable {variable} is missing}}"'
            )
        lines.append('exec "$executable" "$@"')
        result[name] = {
            "command": "/bin/sh",
            "args": ["-c", "\n".join(lines), name, *entry.get("args", [])],
        }
    return result
