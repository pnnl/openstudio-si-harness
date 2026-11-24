"""Real headless host drivers with per-session state and bounded process groups.

CLI contracts: code.claude.com/docs/en/headless and
learn.chatgpt.com/docs/non-interactive-mode. No permission-bypass flags are used.
"""

import json
import os
import re
import shutil
import time

from ..mcp import override_variable
from ..process import process
from .environment import NETWORK_ENV_NAMES


def events(raw):
    parsed = []
    gaps = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:

            def invalid_constant(value):
                raise ValueError("Non-finite host JSON: " + value)

            event = json.loads(line, parse_constant=invalid_constant)
            if not isinstance(event, dict):
                raise TypeError("event is not an object")
            parsed.append(event)
        except (ValueError, TypeError):
            gaps.append("unparseable_host_event")
    return parsed, sorted(set(gaps))


def normalize(host, raw):
    """Retain full skill identities and stable event references; never infer success from prose."""
    rows, gaps = events(raw)
    trace = []
    answer = ""
    usage = {}
    finished = False
    failed = False
    plugin_names = None
    tools = None
    skills = None
    slash_commands = None
    pending = {}
    for event in rows:
        if host == "claude":
            if event.get("type") == "system" and event.get("subtype") == "init":
                tools = event.get("tools")
                skills = event.get("skills")
                slash_commands = event.get("slash_commands")
                if "plugins" in event:
                    plugin_names = [
                        p.get("name") if isinstance(p, dict) else p
                        for p in event["plugins"]
                        if not (
                            isinstance(p, dict)
                            and (
                                p.get("path") == "builtin"
                                or str(p.get("source", "")).endswith("@builtin")
                            )
                        )
                    ]
                for server in event.get("mcp_servers", []):
                    if server.get("status") != "connected":
                        gaps.append(
                            "mcp_not_connected:" + server.get("name", "unknown")
                        )
            for block in (
                (event.get("message") or {}).get("content", [])
                if isinstance((event.get("message") or {}).get("content"), list)
                else []
            ):
                if event.get("type") == "assistant" and block.get("type") not in {
                    "text",
                    "thinking",
                    "redacted_thinking",
                    "tool_use",
                }:
                    gaps.append(
                        "unrecognized_claude_content_type:" + str(block.get("type"))
                    )
                if block.get("type") == "tool_use":
                    name = block.get("name", "")
                    provider = "agent"
                    if name.startswith("mcp__"):
                        provider, _, name = name[5:].partition("__")
                    row = {
                        "id": f"trace:{len(trace)}",
                        "name": name,
                        "provider": provider,
                        "arguments": block.get("input"),
                        "result": None,
                        "is_error": None,
                    }
                    trace.append(row)
                    pending[block.get("id")] = row
                elif (
                    block.get("type") == "tool_result"
                    and block.get("tool_use_id") in pending
                ):
                    pending[block["tool_use_id"]].update(
                        result=block.get("content"),
                        is_error=bool(block.get("is_error")),
                    )
            if event.get("type") == "result":
                finished = True
                failed = bool(event.get("is_error", False)) or str(
                    event.get("subtype", "")
                ).startswith("error")
                answer = str(event.get("result") or "")
                usage = {
                    **event.get("usage", {}),
                    "cost_usd": event.get("total_cost_usd"),
                    "turns": event.get("num_turns"),
                }
        else:
            if event.get("type") == "turn.completed":
                finished = True
                usage = event.get("usage", {})
            if event.get("type") in {"turn.failed", "error"}:
                failed = True
            item = event.get("item", {})
            if event.get("type") == "item.completed":
                kind = item.get("type")
                if kind not in {
                    "agent_message",
                    "mcp_tool_call",
                    "command_execution",
                    "file_change",
                    "reasoning",
                    "todo_list",
                    "plan",
                    "web_search",
                }:
                    gaps.append("unrecognized_codex_item_type:" + str(kind))
                if kind == "mcp_tool_call" and (
                    not item.get("tool") or not item.get("server")
                ):
                    gaps.append("unidentified_mcp_tool_call")
                if kind == "agent_message":
                    answer = item.get("text", "")
                elif kind in {
                    "mcp_tool_call",
                    "command_execution",
                    "file_change",
                    "web_search",
                }:
                    trace.append(
                        {
                            "id": f"trace:{len(trace)}",
                            "name": item.get("tool", kind),
                            "provider": item.get("server", "agent"),
                            "arguments": item.get(
                                "arguments",
                                item.get(
                                    "command", item.get("changes", item.get("action"))
                                ),
                            ),
                            "result": item.get("result", item.get("aggregated_output")),
                            "is_error": item.get("status") == "failed"
                            or item.get("exit_code", 0) not in (0, None),
                        }
                    )
    if not finished:
        gaps.append("missing_completion_event")
    if not answer.strip():
        gaps.append("missing_final_answer")
    if any(row["is_error"] is None for row in trace):
        gaps.append("missing_tool_result")
    return {
        "trace": trace,
        "answer": answer,
        "usage": usage,
        "gaps": sorted(set(gaps)),
        "finished": finished,
        "failed": failed,
        "plugins": plugin_names,
        "tools": tools,
        "skills": skills,
        "slash_commands": slash_commands,
        "skill_observation": (
            "Skill tool calls"
            if host == "claude"
            else "not exposed by codex exec JSONL"
        ),
    }


def capture(config, workspace, packages, session, timeout):
    deadline = time.monotonic() + timeout
    host = config["host"]
    agent = config["agent"]
    limits = config.get("limits", {})
    command = shutil.which(agent.get("command", host))
    if not command:
        raise ValueError(f'Host command not found: {agent.get("command",host)}')
    key_name = agent.get(
        "api_key_env", "ANTHROPIC_API_KEY" if host == "claude" else "OPENAI_API_KEY"
    )
    if not os.environ.get(key_name):
        raise ValueError(
            f"Agent credential environment variable is missing: {key_name}"
        )
    inherited = {
        "PATH",
        "LANG",
        "TERM",
        "TMPDIR",
        "SYSTEMROOT",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
    inherited.update(NETWORK_ENV_NAMES)
    mcp_names = set()
    overrides = set()
    inherited.update(name for name in os.environ if name.startswith("LC_"))
    inherited.update(agent.get("env", []))
    inherited.add(key_name)
    for package in packages:
        file = package / ".mcp.json"
        if file.exists():
            declared = json.loads(file.read_text())["mcpServers"]
            for name, settings in declared.items():
                overrides.add(override_variable(name))
                mcp_names.update(
                    re.findall(r"\$\{([A-Z_][A-Z0-9_]*)(?=[:}])", json.dumps(settings))
                )
    inherited.update(mcp_names | overrides)
    # MCP-declared variables are shared with the host; scopes record that fact.
    env = {name: os.environ[name] for name in inherited if name in os.environ}
    home = session / "host-home"
    home.mkdir(mode=0o700)
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / "config"),
        XDG_CACHE_HOME=str(home / "cache"),
        XDG_DATA_HOME=str(workspace / ".si-data"),
    )
    if host == "claude":
        env.update(
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
            DISABLE_AUTOUPDATER="1",
            DISABLE_TELEMETRY="1",
            DISABLE_ERROR_REPORTING="1",
        )
    # Version/setup commands use the same restricted environment as execution.
    version = process([command, "--version"], workspace, env, min(15, timeout), 10000)
    if "host_interrupted" in version["gaps"]:
        raise KeyboardInterrupt
    if version["returncode"] or version["gaps"]:
        raise ValueError("Unable to determine host version.")
    inventory = None
    if host == "claude":
        env["CLAUDE_CONFIG_DIR"] = str(home)
        env["ANTHROPIC_API_KEY"] = env[key_name]
        env["ANTHROPIC_BASE_URL"] = agent.get("base_url", "https://api.anthropic.com")
        for name in (
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
        ):
            env.pop(name, None)
        mcp = {}
        for package in packages:
            file = package / ".mcp.json"
            if file.exists():
                for name, value in json.loads(file.read_text())["mcpServers"].items():
                    if name in mcp:
                        raise ValueError(
                            "Duplicate MCP server name across selected plugins: " + name
                        )
                    mcp[name] = value
        file = session / "mcp.json"
        file.write_text(json.dumps({"mcpServers": mcp}))
        args = [
            command,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--permission-mode",
            "dontAsk",
            "--setting-sources",
            "",
            "--settings",
            json.dumps(
                {
                    "disableAllHooks": True,
                    "autoMemoryEnabled": False,
                    "permissions": {"blockReadsOutsideWorkingDirectories": True},
                }
            ),
            "--strict-mcp-config",
            "--mcp-config",
            str(file),
            "--model",
            agent["model"],
        ]
        allowed = agent.get("allowed_tools", ["Read", "Glob", "Grep", "Skill"])
        args += [
            "--tools",
            ",".join(sorted({rule.split("(", 1)[0] for rule in allowed})),
            "--allowedTools",
            ",".join([*allowed, *["mcp__" + name + "__*" for name in mcp]]),
        ]
        # These are sanitized package copies, never the source/export/evaluation folders.
        for package in packages:
            args += ["--add-dir", str(package)]
        capabilities = {
            "host_tools": allowed,
            "sandbox": "claude-dontAsk-scoped-file-reads",
            "mcp_servers": sorted(mcp),
            "mcp_policy": "plugin-defined",
        }
        if agent.get("agent"):
            args += ["--agent", agent["agent"]]
        if limits.get("max_cost_usd"):
            args += ["--max-budget-usd", str(limits["max_cost_usd"])]
        for package in packages:
            args += ["--plugin-dir", str(package)]
    else:
        env["CODEX_HOME"] = str(home)
        marketplace = session / "marketplace"
        (marketplace / ".agents/plugins").mkdir(parents=True)
        entries = []
        for package in packages:
            target = marketplace / "plugins" / package.name
            shutil.copytree(package, target)
            entries.append(
                {
                    "name": package.name,
                    "source": {"source": "local", "path": "./plugins/" + package.name},
                    "policy": {
                        "installation": "AVAILABLE",
                        "authentication": "ON_INSTALL",
                    },
                    "category": "Developer Tools",
                }
            )
        (marketplace / ".agents/plugins/marketplace.json").write_text(
            json.dumps({"name": "si-eval", "plugins": entries})
        )
        for setup in [
            [command, "plugin", "marketplace", "add", str(marketplace), "--json"],
            *[
                [command, "plugin", "add", p.name + "@si-eval", "--json"]
                for p in packages
            ],
        ]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("case_time_budget_exhausted_during_plugin_setup")
            ready = process(setup, workspace, env, min(30, remaining), 500000)
            if "host_interrupted" in ready["gaps"]:
                raise KeyboardInterrupt
            if ready["returncode"] or ready["gaps"]:
                raise ValueError(
                    "Codex evaluation plugin setup failed: " + ready["stderr"][-1500:]
                )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("case_time_budget_exhausted_during_inventory")
        listed = process(
            [command, "plugin", "list", "--json"],
            workspace,
            env,
            min(15, remaining),
            500000,
        )
        if "host_interrupted" in listed["gaps"]:
            raise KeyboardInterrupt
        if listed["returncode"] or listed["gaps"]:
            raise ValueError("Codex plugin inventory unavailable.")
        inventory = json.loads(listed["stdout"])
        active = {
            item["name"]
            for item in inventory.get("installed", [])
            if item.get("enabled")
        }
        if active != {package.name for package in packages}:
            raise ValueError("Codex selected/active plugin inventory mismatch.")
        # Preserve only the fresh local plugin config; user auth/config/plugins are never copied.
        base = agent.get("base_url", "https://api.openai.com/v1")
        args = [
            command,
            "exec",
            "--json",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-rules",
            "-s",
            agent.get("sandbox", "read-only"),
            "-m",
            agent["model"],
            "-c",
            'approval_policy="never"',
            "-c",
            'model_provider="si-eval"',
            "-c",
            "model_providers.si-eval="
            + '{name="Evaluation gateway",base_url='
            + json.dumps(base)
            + ",env_key="
            + json.dumps(key_name)
            + ',wire_api="responses"}',
            "-c",
            'web_search="disabled"',
            "-c",
            "project_doc_max_bytes=0",
            "-",
        ]
        capabilities = {
            "host_tools": ["codex-exec-native"],
            "sandbox": agent.get("sandbox", "read-only"),
            "mcp_policy": "plugin-defined",
        }
    capabilities["environment_scopes"] = {
        "agent_credential_names": sorted(
            {key_name, *(["ANTHROPIC_API_KEY"] if host == "claude" else [])}
        ),
        "agent_explicit_names": sorted(agent.get("env", [])),
        "mcp_declared_shared_with_agent": sorted(mcp_names & env.keys()),
        "mcp_launcher_override_names": sorted(overrides & env.keys()),
        "network_names": sorted(set(NETWORK_ENV_NAMES) & env.keys()),
    }
    return {
        "capabilities": capabilities,
        "environment_names": sorted(env),
        "command": args,
        "env": env,
        "version": version["stdout"].strip() or version["stderr"].strip(),
        "inventory": inventory,
    }
