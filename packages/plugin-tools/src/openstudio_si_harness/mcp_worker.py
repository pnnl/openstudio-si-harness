"""Bounded MCP SDK session in the caller's supervised process group.

Only initialize/initialized and, when requested, tools/list are issued. Server
instructions, tool descriptions, schemas, errors and raw streams are not reported.
"""

import json
import logging
import os
import subprocess
import sys


class OutputLimit(Exception):
    pass


class PaginationLimit(Exception):
    pass


def server_environment(command, base):
    """Match the launcher's ordered exports, including chained references."""
    environment = dict(base)
    for name, reference in command.get("env", {}).items():
        source = reference[2:-1]
        if not environment.get(source):
            raise KeyError(source)
        environment[name] = environment[source]
    return environment


async def inspect_server(request):
    import anyio
    from mcp import ClientSession, types
    from mcp.shared.message import SessionMessage

    command = request["server"]
    environment = server_environment(command, os.environ)
    # Unlike the SDK's stdio_client, keep the server in our live supervisor's
    # group. Outer timeout/output cleanup must reach the server and descendants.
    async with await anyio.open_process(
        [command["command"], *command.get("args", [])],
        env=environment,
        cwd=os.getcwd(),
        stderr=subprocess.DEVNULL,
        start_new_session=False,
    ) as process:
        incoming, reader = anyio.create_memory_object_stream(0)
        writer, outgoing = anyio.create_memory_object_stream(0)
        writer_done = anyio.Event()

        async def read_messages():
            total = 0
            buffer = b""
            async with incoming:
                while True:
                    try:
                        chunk = await process.stdout.receive(16384)
                    except anyio.EndOfStream:
                        break
                    total += len(chunk)
                    if total > request["max_bytes"]:
                        raise OutputLimit()
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if line.strip():
                            message = types.jsonrpc_message_adapter.validate_json(
                                line, by_name=False
                            )
                            await incoming.send(SessionMessage(message))
                if buffer.strip():
                    raise ValueError("Incomplete MCP message")

        async def write_messages():
            try:
                async with outgoing:
                    async for message in outgoing:
                        encoded = message.message.model_dump_json(
                            by_alias=True, exclude_none=True
                        )
                        await process.stdin.send((encoded + "\n").encode())
            finally:
                writer_done.set()

        try:
            with anyio.fail_after(request["timeout_seconds"]):
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(read_messages)
                    tasks.start_soon(write_messages)
                    try:
                        async with ClientSession(reader, writer) as session:
                            initialized = await session.initialize()
                            tools_supported = initialized.capabilities.tools is not None
                            names = None
                            if request["probe"] == "mcp-tools":
                                names = []
                                if tools_supported:
                                    cursor, seen = None, set()
                                    for _ in range(20):
                                        page = await session.list_tools(
                                            params=(
                                                types.PaginatedRequestParams(
                                                    cursor=cursor
                                                )
                                                if cursor
                                                else None
                                            )
                                        )
                                        names.extend(tool.name for tool in page.tools)
                                        if len(names) > 1000:
                                            raise PaginationLimit()
                                        cursor = page.next_cursor
                                        if cursor is None:
                                            break
                                        if cursor in seen:
                                            raise PaginationLimit()
                                        seen.add(cursor)
                                    else:
                                        raise PaginationLimit()
                            result = {
                                "ok": True,
                                "protocol_version": initialized.protocol_version,
                                "server_name": initialized.server_info.name,
                                "server_version": initialized.server_info.version,
                                "tools_supported": tools_supported,
                                "tools": names,
                            }
                        # Flush initialized/queued messages before ending the
                        # probe; then give cooperative servers bounded EOF cleanup.
                        await writer.aclose()
                        await writer_done.wait()
                        await process.stdin.aclose()
                        with anyio.move_on_after(0.2):
                            await process.wait()
                    finally:
                        tasks.cancel_scope.cancel()
            return result
        finally:
            with anyio.CancelScope(shield=True):
                if process.returncode is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                    with anyio.move_on_after(0.1):
                        await process.wait()
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    with anyio.move_on_after(0.2):
                        await process.wait()


def leaves(exc):
    children = getattr(exc, "exceptions", ())
    if children:
        for child in children:
            yield from leaves(child)
    else:
        yield exc


def main():
    logging.disable(logging.CRITICAL)
    try:
        import anyio
        import mcp  # noqa: F401
    except ImportError:
        print(json.dumps({"ok": False, "reason": "mcp_sdk_unavailable"}))
        return
    try:
        request = json.load(sys.stdin)
        result = anyio.run(inspect_server, request)
    except Exception as exc:  # noqa: BLE001
        errors = list(leaves(exc))
        reason = (
            "mcp_output_limit"
            if any(isinstance(e, OutputLimit) for e in errors)
            else (
                "mcp_pagination_limit"
                if any(isinstance(e, PaginationLimit) for e in errors)
                else (
                    "mcp_timeout"
                    if any(isinstance(e, TimeoutError) for e in errors)
                    else "mcp_protocol_error"
                )
            )
        )
        result = {"ok": False, "reason": reason}
    print(json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    main()
