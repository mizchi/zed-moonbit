#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SAMPLE = ROOT / "tests" / "lsp-sample"


class LspSession:
    def __init__(self, command: list[str], cwd: Path, timeout: float) -> None:
        self.command = command
        self.cwd = cwd
        self.timeout = timeout
        self.next_id = 1
        self.messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.notifications: list[dict[str, Any]] = []
        self.responses: dict[int, dict[str, Any]] = {}
        self.stderr_lines: list[str] = []
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self.stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self.stdout_thread.start()
        self.stderr_thread.start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        while True:
            try:
                headers: dict[str, str] = {}
                while True:
                    line = self.process.stdout.readline()
                    if line == b"":
                        self.messages.put(None)
                        return
                    if line in (b"\r\n", b"\n"):
                        break
                    key, _, value = line.decode("ascii", errors="replace").partition(":")
                    headers[key.lower()] = value.strip()

                length_text = headers.get("content-length")
                if length_text is None:
                    continue
                body = self.process.stdout.read(int(length_text))
                if not body:
                    self.messages.put(None)
                    return
                self.messages.put(json.loads(body.decode("utf-8")))
            except Exception as exc:
                self.stderr_lines.append(f"stdout reader failed: {exc}")
                self.messages.put(None)
                return

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self.stderr_lines.append(line.decode("utf-8", errors="replace").rstrip())

    def send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
        header = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
        self.process.stdin.write(header + payload)
        self.process.stdin.flush()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        request_id = self.next_id
        self.next_id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        response = self._wait_for_response(request_id)
        if "error" in response:
            raise AssertionError(f"{method} failed: {response['error']}")
        return response.get("result")

    def _wait_for_response(self, request_id: int) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if request_id in self.responses:
                return self.responses.pop(request_id)
            self._drain_one(deadline)
        raise AssertionError(f"timed out waiting for response {request_id}")

    def wait_for(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        description: str,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        for message in self.notifications:
            if predicate(message):
                return message

        while time.monotonic() < deadline:
            message = self._drain_one(deadline)
            if message is not None and predicate(message):
                return message
        diagnostics = []
        for message in self.notifications:
            if message.get("method") != "textDocument/publishDiagnostics":
                continue
            params = message.get("params", {})
            diagnostics.append(
                {
                    "uri": params.get("uri"),
                    "messages": [
                        item.get("message")
                        for item in params.get("diagnostics", [])
                    ],
                }
            )
        detail = json.dumps(diagnostics[-5:], ensure_ascii=False, indent=2)
        raise AssertionError(f"timed out waiting for {description}\nlast diagnostics: {detail}")

    def _drain_one(self, deadline: float) -> dict[str, Any] | None:
        remaining = max(0.0, min(0.25, deadline - time.monotonic()))
        try:
            message = self.messages.get(timeout=remaining)
        except queue.Empty:
            return None

        if message is None:
            code = self.process.poll()
            stderr = "\n".join(self.stderr_lines[-20:])
            raise AssertionError(f"LSP server exited unexpectedly with {code}\n{stderr}")

        if "id" in message and ("result" in message or "error" in message):
            self.responses[int(message["id"])] = message
        elif "method" in message:
            self.notifications.append(message)
        return message

    def close(self) -> None:
        try:
            if self.process.poll() is None:
                self.request("shutdown", {})
                self.notify("exit")
                self.process.wait(timeout=5)
        except Exception:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()


def find_lsp_command() -> list[str]:
    override = os.environ.get("MOONBIT_LSP_CMD")
    if override:
        return shlex.split(override)

    moonbit_lsp = shutil.which("moonbit-lsp")
    if moonbit_lsp:
        return [moonbit_lsp]

    moon_lsp = shutil.which("moon-lsp")
    if moon_lsp:
        return [moon_lsp, "--stdio"]

    raise AssertionError("could not find moonbit-lsp or moon-lsp in PATH")


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise AssertionError(f"could not find {name} in PATH")
    return path


def run(cmd: list[str], cwd: Path) -> None:
    print("+", " ".join(cmd))
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        raise AssertionError(f"command failed: {' '.join(cmd)}")


def position_after(text: str, needle: str) -> dict[str, int]:
    index = text.index(needle) + len(needle)
    before = text[:index]
    return {
        "line": before.count("\n"),
        "character": len(before.rsplit("\n", 1)[-1]),
    }


def position_inside(text: str, needle: str) -> dict[str, int]:
    index = text.index(needle) + 1
    before = text[:index]
    return {
        "line": before.count("\n"),
        "character": len(before.rsplit("\n", 1)[-1]),
    }


def completion_items(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return result
    if isinstance(result, dict) and isinstance(result.get("items"), list):
        return result["items"]
    return []


def content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(content_text(item) for item in value)
    if isinstance(value, dict):
        return content_text(value.get("value", ""))
    return ""


def assert_contains(text: str, expected: str, label: str) -> None:
    if expected not in text:
        raise AssertionError(f"{label} did not contain {expected!r}: {text!r}")


def initialize_session(session: LspSession, sample_dir: Path) -> None:
    root_uri = sample_dir.as_uri()
    session.request(
        "initialize",
        {
            "processId": os.getpid(),
            "rootPath": str(sample_dir),
            "rootUri": root_uri,
            "workspaceFolders": [{"uri": root_uri, "name": sample_dir.name}],
            "capabilities": {
                "workspace": {"workspaceFolders": True},
                "textDocument": {
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "completion": {
                        "completionItem": {
                            "documentationFormat": ["markdown", "plaintext"],
                            "snippetSupport": False,
                        }
                    },
                },
            },
        },
    )
    session.notify("initialized", {})


def assert_diagnostics(sample_dir: Path, command: list[str], timeout: float) -> None:
    with tempfile.TemporaryDirectory(prefix="zed-moonbit-lsp-") as temp_dir:
        temp_sample = Path(temp_dir) / "lsp-sample"
        shutil.copytree(
            sample_dir,
            temp_sample,
            ignore=shutil.ignore_patterns("_build"),
        )
        run(["moon", "check"], temp_sample)

        main_file = temp_sample / "cmd" / "main" / "main.mbt"
        diagnostic_text = """///|
fn main {
  let x = @sample.greeting()
  let message = @sample.greeting("Zed")
  println(message)
}
"""
        main_file.write_text(diagnostic_text, encoding="utf-8")

        uri = main_file.resolve().as_uri()
        print("+", " ".join(command))
        session = LspSession(command, temp_sample.resolve(), timeout)
        try:
            initialize_session(session, temp_sample.resolve())
            session.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": "moonbit",
                        "version": 1,
                        "text": diagnostic_text,
                    }
                },
            )
            diagnostics_message = session.wait_for(
                lambda message: (
                    message.get("method") == "textDocument/publishDiagnostics"
                    and message.get("params", {}).get("uri") == uri
                    and any(
                        "requires 1 arguments" in str(diagnostic.get("message", ""))
                        for diagnostic in message.get("params", {}).get("diagnostics", [])
                    )
                ),
                "MoonBit diagnostics for invalid greeting call",
            )
            diagnostics = diagnostics_message["params"]["diagnostics"]
            diagnostic_messages = "\n".join(str(item.get("message", "")) for item in diagnostics)
            assert_contains(diagnostic_messages, "requires 1 arguments", "diagnostics")
            print("[OK] diagnostics reported invalid @sample.greeting() call")
        finally:
            session.close()


def test_lsp(sample_dir: Path, timeout: float) -> None:
    sample_dir = sample_dir.resolve()
    main_file = sample_dir / "cmd" / "main" / "main.mbt"
    if not main_file.exists():
        raise AssertionError(f"missing sample entry point: {main_file}")

    require_tool("moon")
    run(["moon", "check"], sample_dir)

    command = find_lsp_command()
    print("+", " ".join(command))

    uri = main_file.resolve().as_uri()
    root_uri = sample_dir.as_uri()
    version = 1
    text = main_file.read_text(encoding="utf-8")
    session = LspSession(command, sample_dir, timeout)

    try:
        initialize_session(session, sample_dir)
        session.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": "moonbit",
                    "version": version,
                    "text": text,
                }
            },
        )

        hover = session.request(
            "textDocument/hover",
            {
                "textDocument": {"uri": uri},
                "position": position_inside(text, "println"),
            },
        )
        hover_text = content_text(hover.get("contents") if isinstance(hover, dict) else hover)
        assert_contains(hover_text, "println", "hover")
        print("[OK] hover returned documentation for println")

        version += 1
        completion_text = """///|
fn main {
  let value = @sample.
}
"""
        session.notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": completion_text}],
            },
        )
        completion = session.request(
            "textDocument/completion",
            {
                "textDocument": {"uri": uri},
                "position": position_after(completion_text, "@sample."),
                "context": {"triggerKind": 2, "triggerCharacter": "."},
            },
        )
        labels = {str(item.get("label")) for item in completion_items(completion)}
        if "greeting" not in labels:
            raise AssertionError(f"completion labels did not include 'greeting': {sorted(labels)[:20]}")
        print("[OK] completion included @sample.greeting")
    finally:
        session.close()

    assert_diagnostics(sample_dir, command, timeout)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run MoonBit LSP smoke tests.")
    parser.add_argument(
        "--sample-dir",
        type=Path,
        default=DEFAULT_SAMPLE,
        help="MoonBit sample project used for LSP checks.",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    try:
        test_lsp(args.sample_dir, args.timeout)
    except AssertionError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
