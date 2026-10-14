"""Restricted Qoder subprocess and fail-closed, version-pinned stream handling."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

EXECUTABLE = Path("/home/fec/.local/bin/qodercli")
SANDBOX = Path("/usr/bin/bwrap")
SANDBOX_POLICY = "ram-profile-auth-bind-v1"
PROFILE = Path("/home/fec/.qoder-news")
IDENTITY_FILES = ("installation_id", "umid-cache.json")
# Runtime isolation/authentication attestation must precede any portfolio prompt.
VERIFICATION = Path(__file__).resolve().parents[1] / ".run" / "news-compatibility.json"
CLI_VERSION = "1.1.66"
PROTOCOL_VERSION = "1.5.0"
DEADLINE = 30 * 60
PROGRESS_INTERVAL = 30
OUTPUT_LIMIT = 2_000_000
ERROR_LIMIT = 64_000
TOOLS = {"WebSearch", "WebFetch"}
WEB_ACCESS_POLICY = "public-web-callback-v1"
# The built-in catalog remains in initialization even when Agent is unavailable.
BUILTIN_AGENTS = ["general-purpose", "statusline-setup", "Explore", "Plan", "qoder-guide"]


class QoderError(Exception):
    pass


def decode_json(value):
    def pairs(entries):
        result = {}
        for key, item in entries:
            if key in result:
                raise QoderError("Duplicate JSON field in Qoder output or cached state.")
            result[key] = item
        return result

    def invalid(_):
        raise QoderError("Non-finite JSON value in Qoder output or cached state.")

    try:
        return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, TypeError, RecursionError):
        raise QoderError("Malformed JSON in Qoder output or cached state.") from None


def environment():
    return {"HOME": "/home/fec", "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8", "TZ": "Asia/Shanghai", "TMPDIR": "/tmp"}


def preflight():
    if not EXECUTABLE.is_file() or not os.access(EXECUTABLE, os.X_OK):
        raise QoderError("Qoder executable is missing or not executable.")
    if not PROFILE.exists():
        raise QoderError("News-only Qoder login is missing. Run qodercli --config-dir /home/fec/.qoder-news login interactively.")
    meta = PROFILE.lstat()
    if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.getuid() or stat.S_IMODE(meta.st_mode) != 0o700:
        raise QoderError("News-only Qoder profile must be an owned private directory (0700).")
    if not SANDBOX.is_file() or not os.access(SANDBOX, os.X_OK):
        raise QoderError("News requires bubblewrap; no unsandboxed fallback is permitted.")
    try:
        meta = (PROFILE / ".auth").lstat()
        if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.getuid() or stat.S_IMODE(meta.st_mode) != 0o700:
            raise QoderError("News authentication storage must be an owned private directory (0700).")
        for name in IDENTITY_FILES:
            meta = (PROFILE / name).lstat()
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o022:
                raise QoderError("News login identity files must be owned regular files without shared write access.")
    except OSError:
        raise QoderError("News login authentication or identity storage is missing; no research was requested.") from None
    try:
        fd = os.open(VERIFICATION, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as src:
            meta = os.fstat(src.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or stat.S_IMODE(meta.st_mode) != 0o600 or meta.st_size > 4096:
                raise QoderError("Invalid news compatibility receipt.")
            verified = decode_json(src.read())
    except OSError:
        raise QoderError("News compatibility gate is closed: authenticated public-only Sonus smoke, extension/source isolation, content-free diagnostics and web-tool model policy have not been verified. No portfolio request was sent.") from None
    required = {"cli_version", "protocol_version", "executable_sha256", "profile", "web_only",
                "source_isolation", "cron_auth", "webfetch_model_policy", "sandbox_policy", "web_access_policy"}
    if not isinstance(verified, dict) or set(verified) != required:
        raise QoderError("Invalid news compatibility receipt.")
    digest = hashlib.sha256()
    with EXECUTABLE.open("rb") as executable:
        for chunk in iter(lambda: executable.read(1024 * 1024), b""):
            digest.update(chunk)
    digest = digest.hexdigest()
    if (verified["cli_version"] != CLI_VERSION or verified["protocol_version"] != PROTOCOL_VERSION
            or verified["executable_sha256"] != digest or verified["profile"] != str(PROFILE)
            or any(verified[key] is not True for key in ("web_only", "source_isolation", "cron_auth"))
            or verified["sandbox_policy"] != SANDBOX_POLICY
            or verified["web_access_policy"] != WEB_ACCESS_POLICY
            or verified["webfetch_model_policy"] != "Sonus primary; WebFetch secondary lite"):
        raise QoderError("News compatibility receipt does not match the verified executable/profile/model policy.")


def arguments(cwd, system_prompt):
    sandbox = [str(SANDBOX), "--unshare-user", "--unshare-pid", "--die-with-parent",
               "--ro-bind", "/", "/", "--tmpfs", "/home", "--tmpfs", "/tmp", "--proc", "/proc",
               "--dev", "/dev", "--dir", str(PROFILE), "--bind", str(PROFILE / ".auth"), str(PROFILE / ".auth"),
               "--ro-bind", str(EXECUTABLE), str(EXECUTABLE), "--dir", str(cwd), "--chdir", str(cwd)]
    for name in IDENTITY_FILES:
        sandbox.extend(["--ro-bind", str(PROFILE / name), str(PROFILE / name)])
    return sandbox + ["--", str(EXECUTABLE), "--config-dir", str(PROFILE), "--cwd", str(cwd),
            "--model", "Sonus", "--tools", "WebSearch,WebFetch", "--allowed-tools", "WebSearch",
            "--permission-mode", "default", "--permission-prompt-tool", "stdio",
            "--setting-sources", "", "--disable-builtin-skills",
            "--settings", '{"skills":{"enabled":false},"disableAllHooks":true,"enabledPlugins":{"qoder-sites@builtin":false},"telemetry":{"enabled":false,"logPrompts":false}}',
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--system-prompt", system_prompt, "--no-session-persistence", "--max-output-tokens", "6000",
            "--max-model-request-retries", "1", "--input-format", "stream-json", "--output-format", "stream-json", "--print"]


def validate_init(event, cwd):
    if (event.get("qodercli_version") != CLI_VERSION
            or event.get("protocol_version") != PROTOCOL_VERSION
            or event.get("model") != "Sonus" or event.get("cwd") != str(cwd)
            or event.get("permissionMode") != "default"
            or not isinstance(event.get("tools"), list) or set(event["tools"]) != TOOLS
            or event.get("agents") != BUILTIN_AGENTS
            or any(event.get(key) != [] for key in ("mcp_servers", "skills", "plugins"))):
        raise QoderError("Qoder initialization does not match the verified web-only Sonus profile.")


def permission_response(event, remaining):
    from .news import NewsError, public_url

    request = event.get("request")
    request_id = event.get("request_id")
    if (not isinstance(request_id, str) or not request_id or not isinstance(request, dict)
            or request.get("subtype") != "can_use_tool"
            or not isinstance(request.get("tool_use_id"), str)):
        raise QoderError("Unsupported Qoder permission request.")
    approved = False
    value = request.get("input")
    if request.get("tool_name") == "WebFetch" and isinstance(value, dict):
        try:
            host = urlsplit(public_url(value.get("url"))).hostname
            # Resolve out of process so DNS cannot outlive the research deadline.
            check = subprocess.run(
                ["/usr/bin/python3", "-I", "-c",
                 "import ipaddress,socket,sys; "
                 "rows=socket.getaddrinfo(sys.argv[1],443,type=socket.SOCK_STREAM); "
                 "sys.exit(0 if rows and all(ipaddress.ip_address(r[4][0]).is_global for r in rows) else 1)",
                 host], env=environment(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=max(0.01, min(10, remaining)))
            approved = check.returncode == 0
        except (NewsError, OSError, subprocess.TimeoutExpired):
            pass
    response = {"behavior": "allow" if approved else "deny", "toolUseID": request["tool_use_id"]}
    if approved:
        response["updatedInput"] = value
    else:
        response["message"] = "Only verified public-web fetches are approved."
    return {"type": "control_response", "response": {
        "subtype": "success", "request_id": request_id, "response": response}}


def execute(argv, prompt, cwd):
    if len(prompt.encode("utf-8")) > 64_000:
        raise QoderError("Sanitized research prompt exceeds its size limit.")
    data = bytearray((json.dumps({"type": "user", "session_id": "", "parent_tool_use_id": None,
                      "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}})
                      + "\n").encode("utf-8"))
    started = time.monotonic()
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=environment(), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                                start_new_session=True)
    except OSError:
        raise QoderError("Cannot launch the isolated Qoder executable.") from None
    output = bytearray()
    pending = bytearray()
    received = 0
    errors = 0
    initialized = False
    finished = False
    approvals = set()
    try:
        with selectors.DefaultSelector() as selector:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                os.set_blocking(stream.fileno(), False)
            selector.register(proc.stdin, selectors.EVENT_WRITE)
            selector.register(proc.stdout, selectors.EVENT_READ)
            selector.register(proc.stderr, selectors.EVENT_READ)
            next_progress = started + PROGRESS_INTERVAL
            while selector.get_map() or proc.poll() is None:
                current = time.monotonic()
                elapsed = current - started
                remaining = DEADLINE - elapsed
                if remaining <= 0:
                    raise QoderError(f"Qoder exceeded the {DEADLINE / 60:g}-minute deadline; its process group was stopped.")
                if current >= next_progress:
                    print(f"News research: waiting for Qoder ({int(elapsed)}s elapsed; "
                          f"deadline {DEADLINE}s; {len(output)} output bytes received).",
                          file=sys.stderr, flush=True)
                    next_progress = current + PROGRESS_INTERVAL
                for key, _ in selector.select(min(remaining, 1, next_progress - current)):
                    stream = key.fileobj
                    if stream is proc.stdin:
                        try:
                            written = os.write(stream.fileno(), data[:4096])
                            del data[:written]
                        except BrokenPipeError:
                            data.clear()
                            selector.unregister(stream)
                            stream.close()
                            continue
                        if not data:
                            selector.unregister(stream)
                        continue
                    chunk = os.read(stream.fileno(), 16384)
                    if not chunk:
                        selector.unregister(stream)
                        stream.close()
                    elif stream is proc.stdout:
                        received += len(chunk)
                        if received > OUTPUT_LIMIT:
                            raise QoderError("Qoder stdout exceeded its limit; no report accepted.")
                        pending.extend(chunk)
                        while b"\n" in pending:
                            line, _, rest = pending.partition(b"\n")
                            pending = bytearray(rest)
                            if not line.strip():
                                continue
                            event = decode_json(line)
                            if not isinstance(event, dict) or finished:
                                raise QoderError("Invalid Qoder event ordering.")
                            kind = event.get("type")
                            if kind == "system" and event.get("subtype") == "init":
                                if initialized:
                                    raise QoderError("Duplicate Qoder initialization.")
                                validate_init(event, cwd)
                                initialized = True
                            if kind == "control_request":
                                identifier = event.get("request_id")
                                if not initialized or not isinstance(identifier, str) or identifier in approvals or proc.stdin.closed:
                                    raise QoderError("Uncorrelated Qoder permission request.")
                                reply = permission_response(event, DEADLINE - (time.monotonic() - started))
                                approvals.add(identifier)
                                data.extend((json.dumps(reply) + "\n").encode("utf-8"))
                                if proc.stdin not in [entry.fileobj for entry in selector.get_map().values()]:
                                    selector.register(proc.stdin, selectors.EVENT_WRITE)
                                continue
                            if kind == "control_cancel_request":
                                if event.get("request_id") not in approvals:
                                    raise QoderError("Uncorrelated Qoder permission cancellation.")
                                continue
                            output.extend(line + b"\n")
                            if kind == "result":
                                finished = True
                                if not proc.stdin.closed:
                                    if proc.stdin in [entry.fileobj for entry in selector.get_map().values()]:
                                        selector.unregister(proc.stdin)
                                    proc.stdin.close()
                    else:
                        errors += len(chunk)
                        if errors > ERROR_LIMIT:
                            raise QoderError("Qoder stderr exceeded its limit; no report accepted.")
            remaining = DEADLINE - (time.monotonic() - started)
            if remaining <= 0:
                raise QoderError("Qoder exceeded its deadline; no report accepted.")
            try:
                code = proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise QoderError("Qoder exceeded its deadline; no report accepted.") from None
            if code:
                raise QoderError("Qoder failed in its sandbox; check bubblewrap, news-only login, Sonus availability and network access. No fallback was attempted.")
        output.extend(pending)
        try:
            return output.decode("utf-8", errors="strict")
        except UnicodeError:
            raise QoderError("Qoder emitted invalid UTF-8.") from None
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()


def parse_stream(output, cwd):
    from .news import public_url

    initialized = False
    calls = {}
    completed = set()
    urls = set()
    searched = False
    fetched = False
    final = None
    for line in output.splitlines():
        if not line.strip():
            continue
        event = decode_json(line)
        if not isinstance(event, dict) or final is not None:
            raise QoderError("Invalid Qoder event ordering.")
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            if initialized:
                raise QoderError("Duplicate Qoder initialization.")
            validate_init(event, cwd)
            initialized = True
            continue
        if not initialized:
            raise QoderError("Qoder stream lacks a verified initialization event.")
        if kind == "system" and event.get("subtype") == "artifacts_update":
            if event.get("artifacts") != []:
                raise QoderError("Qoder produced unexpected artifacts; no briefing accepted.")
            continue
        if kind == "system" and event.get("subtype") == "permission_denied":
            raise QoderError("Qoder denied a required web tool; check the news-only allowlist. No briefing accepted.")
        if kind in ("assistant", "user"):
            message = event.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("content"), list):
                raise QoderError("Unsupported Qoder message envelope.")
            results = []
            for block in message["content"]:
                if not isinstance(block, dict):
                    raise QoderError("Unsupported Qoder content block.")
                if block.get("type") == "tool_use":
                    identifier = block.get("id")
                    if (kind != "assistant" or not isinstance(identifier, str) or identifier in calls
                            or block.get("name") not in TOOLS or not isinstance(block.get("input"), dict)):
                        raise QoderError("Unapproved or malformed Qoder tool invocation.")
                    calls[identifier] = block["name"]
                elif block.get("type") == "tool_result":
                    results.append(block)
                elif block.get("type") not in ("text", "thinking", "redacted_thinking"):
                    raise QoderError("Unsupported Qoder content block type.")
            if results:
                if kind != "user" or len(results) != 1:
                    raise QoderError("Unsupported Qoder tool-result envelope.")
                block = results[0]
                identifier = block.get("tool_use_id")
                if not isinstance(identifier, str) or identifier not in calls or identifier in completed:
                    raise QoderError("Uncorrelated Qoder tool evidence.")
                completed.add(identifier)
                if block.get("is_error", False) is not False:
                    continue
                evidence = event.get("tool_use_result")
                if not isinstance(evidence, dict):
                    continue
                if calls[identifier] == "WebSearch":
                    rows = evidence.get("results")
                    if isinstance(rows, list):
                        for row in rows:
                            if isinstance(row, dict) and isinstance(row.get("link"), str):
                                urls.add(public_url(row["link"]))
                                searched = True
                elif (type(evidence.get("code")) is int and 200 <= evidence["code"] < 300
                      and isinstance(evidence.get("result"), str) and evidence["result"].strip()):
                    urls.add(public_url(evidence.get("url")))
                    fetched = True
        elif kind == "result":
            if (event.get("subtype") != "success" or event.get("is_error") is not False
                    or event.get("permission_denials") != []
                    or event.get("stop_reason") not in (None, "end_turn", "stop")
                    or not isinstance(event.get("result"), str)):
                raise QoderError("Qoder returned an error, denial or truncated result.")
            final = decode_json(event["result"])
        else:
            raise QoderError("Unsupported Qoder stream event; compatibility verification required.")
    if not isinstance(final, dict) or not searched or not fetched or set(calls) != completed:
        raise QoderError("Incomplete Qoder result or missing successful search/extraction evidence.")
    return final, sorted(urls)


def research(system_prompt, prompt):
    preflight()
    with tempfile.TemporaryDirectory(prefix="schwab-news-", dir="/tmp") as cwd:
        output = execute(arguments(cwd, system_prompt), prompt, cwd)
        return parse_stream(output, cwd)
