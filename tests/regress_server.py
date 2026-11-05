#!/usr/bin/env python3
"""Regression tests for LSP framing, positions, workers and request lifecycle.

Each test reproduces a reported bug through the real stdio server.
Run with ``python3 tests/regress_server.py --server build/glolsp``.
"""
import argparse
import json
import queue
import re
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

import integration
from integration import LspClient


def lsp_position(text, offset):
    """UTF-16 position of a Python string index; \\r\\n, \\r and \\n end lines."""
    lines = re.split(r"\r\n|\r|\n", text[:offset])
    return {"line": len(lines) - 1, "character": len(lines[-1].encode("utf-16-le")) // 2}


class ArgumentClient(LspClient):
    """An LspClient whose server receives extra command-line arguments."""

    def __init__(self, arguments):
        self.process = subprocess.Popen(
            [str(integration.SERVER), *arguments],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.messages = queue.Queue()
        self.pending = []
        self.errors = []
        self.next_id = 1
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.error_reader = threading.Thread(target=self._read_errors, daemon=True)
        self.error_reader.start()


class RegressServer(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="glolsp-regress-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.client = self.start()

    def start(self, step_limit=None):
        client = LspClient()
        self.addCleanup(client.stop)
        options = {"scanWorkspace": False}
        if step_limit is not None:
            options["comptimeStepLimit"] = step_limit
        client.capabilities = client.request("initialize", {
            "processId": None, "rootUri": self.root.as_uri(), "capabilities": {},
            "initializationOptions": options,
        })["capabilities"]
        client.notify("initialized", {})
        return client

    def uri(self, name):
        return (self.root / name).as_uri()

    def exit_code(self, client):
        client.request("shutdown")
        client.notify("exit")
        return client.process.wait(timeout=integration.TIMEOUT)

    def wait_errors(self, client, needle, timeout=integration.TIMEOUT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in "".join(client.errors):
                return
            time.sleep(0.05)
        self.fail(f"{needle!r} not on stderr: {''.join(client.errors)!r}")

    def symbols(self, uri):
        """Declared names in document order, including nested children."""
        result = self.client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        names = []

        def walk(symbols):
            for symbol in symbols:
                names.append(symbol["name"])
                walk(symbol.get("children", []))

        walk(result)
        return names

    # D1: a range ending past the last line replaces through the end.
    def test_edit_ending_past_last_line_is_applied(self):
        uri = self.uri("eof.glo")
        self.client.open(uri, "alpha :: 1;\nbeta :: 2;")
        self.client.change(uri, [{"range": {
            "start": {"line": 0, "character": 11}, "end": {"line": 2, "character": 0},
        }, "text": ""}], 2)
        self.client.change(uri, [{"range": {
            "start": {"line": 0, "character": 11}, "end": {"line": 0, "character": 11},
        }, "text": "\ngamma :: 3;"}], 3)
        self.assertEqual(self.symbols(uri), ["alpha", "gamma"])
        # Whole-buffer requests end at the line count, past a missing final break.
        whole = {"start": {"line": 0, "character": 0}, "end": {"line": 2, "character": 0}}
        hints = self.client.request_message("textDocument/inlayHint", {
            "textDocument": {"uri": uri}, "range": whole,
        })
        self.assertIn("result", hints)
        actions = self.client.request_message("textDocument/codeAction", {
            "textDocument": {"uri": uri}, "range": whole, "context": {"diagnostics": []},
        })
        self.assertIn("result", actions)

    # D1: any line past the last one is the end of the document.
    def test_edit_on_a_line_far_past_the_end_appends(self):
        uri = self.uri("far-eof.glo")
        self.client.open(uri, "alpha :: 1;\n")
        self.client.change(uri, [{"range": {
            "start": {"line": 40, "character": 3}, "end": {"line": 99, "character": 0},
        }, "text": "omega :: 2;\n"}], 2)
        self.assertEqual(self.symbols(uri), ["alpha", "omega"])
        hover = self.client.request_message("textDocument/hover", {
            "textDocument": {"uri": uri}, "position": {"line": 999, "character": 0},
        })
        self.assertIn("result", hover)

    # C3: an oversized or unsupported body is skipped; the stream stays usable.
    def test_oversized_and_unsupported_messages_are_skipped(self):
        size = 64 * 1024 * 1024 + 1
        self.client.process.stdin.write(f"Content-Length: {size}\r\n\r\n".encode() + b"x" * size)
        latin1 = json.dumps({"jsonrpc": "2.0", "id": 900, "method": "workspace/symbol"}).encode()
        self.client.process.stdin.write(
            b"Content-Type: application/json; charset=latin1\r\n"
            + f"Content-Length: {len(latin1)}\r\n\r\n".encode() + latin1)
        self.client.process.stdin.flush()
        self.assertEqual(self.client.request("workspace/symbol", {"query": "zz"}), [])
        self.wait_errors(self.client, "64 MiB limit")
        self.wait_errors(self.client, "unsupported Content-Type charset")
        self.assertEqual(self.exit_code(self.client), 0)

    def test_large_document_below_source_limit_opens(self):
        # 11 MiB of source; escaping its quotes and backslashes makes the
        # frame larger than 16 MiB.
        line = 'x :: "\\\\\\"";\n'
        text = line * (11 * 1024 * 1024 // len(line))
        uri = self.uri("large.glo")
        self.client.open(uri, text)
        # The open itself parses 11 MiB on the main thread; this test is about
        # accepting the frame, so wait for that work instead of racing it.
        request_id = self.client.next_id
        self.client.next_id += 1
        self.client.send({"jsonrpc": "2.0", "id": request_id, "method": "textDocument/foldingRange",
                          "params": {"textDocument": {"uri": uri}}})
        reply = self.client.wait_for(lambda message: message.get("id") == request_id, timeout=120)
        self.assertEqual(reply["result"], [])
        self.assertNotIn("skipped", "".join(self.client.errors))

    def test_malformed_header_reports_reason_and_exits(self):
        self.client.process.stdin.write(b"Content-Length: twelve\r\n\r\n")
        self.client.process.stdin.flush()
        self.assertEqual(self.client.process.wait(timeout=integration.TIMEOUT), 1)
        self.wait_errors(self.client, "invalid LSP frame: invalid Content-Length")

    # C4: compile-time code cannot read the LSP input stream.
    def test_compile_time_stdin_read_leaves_frames_intact(self):
        uri = self.uri("stdin.glo")
        self.client.open(uri, '#import "File_System/File";\n'
                              '#comptime { input := File.stdin(); _ = input.read_byte(); }\n'
                              'main :: () {}\n')
        self.client.diagnostics(uri, 1)
        for _ in range(3):
            self.assertEqual(self.client.request("workspace/symbol", {"query": "zz"}), [])
        self.assertEqual(self.exit_code(self.client), 0)

    # C2: libglosso's recursion on the compiler worker needs a large stack.
    def test_long_operator_chain_does_not_crash_compiler_worker(self):
        uri = self.uri("chain.glo")
        self.client.open(uri, "a :: 1;\nmain :: () { x := " + " + ".join(["a"] * 6000) + "; }\n")
        self.assertEqual(self.client.diagnostics(uri, 1), [])
        self.assertEqual(self.exit_code(self.client), 0)

    # L2: a waiting request survives closing its document.
    def test_pending_diagnostic_is_answered_after_close(self):
        client = self.start(step_limit=0)
        slow = self.uri("slow.glo")
        plain = self.root / "plain.glo"
        plain.write_text("b_value :: 1;\n")
        client.open(slow, "#comptime { i := 0; while i < 400000 { i += 1; } }\nmain :: () {}\n")
        client.open(plain.as_uri(), "b_value :: 1;\n")
        request_id = client.next_id
        client.next_id += 1
        client.send({"jsonrpc": "2.0", "id": request_id, "method": "textDocument/diagnostic",
                     "params": {"textDocument": {"uri": plain.as_uri()}}})
        client.notify("textDocument/didClose", {"textDocument": {"uri": plain.as_uri()}})
        reply = client.wait_for(lambda message: message.get("id") == request_id, timeout=60)
        self.assertEqual(reply["result"]["items"], [])

    # F1: string literals use the standard semantic token type.
    def test_semantic_legend_uses_standard_string_type(self):
        legend = self.client.capabilities["semanticTokensProvider"]["legend"]["tokenTypes"]
        self.assertIn("string", legend)
        self.assertNotIn("str", legend)
        uri = self.uri("strings.glo")
        self.client.open(uri, 'text :: "quoted";\n')
        data = self.client.request("textDocument/semanticTokens/full",
                                   {"textDocument": {"uri": uri}})["data"]
        kinds = {legend[data[index + 3]] for index in range(0, len(data), 5)}
        self.assertIn("string", kinds)

    # P1: converting offsets far into a file no longer rescans from byte 0.
    def test_positions_far_into_a_file_are_fast_and_exact(self):
        padding = "".join(f"// padding line {index:06d} ..................................\n"
                          for index in range(6000))
        body = "counter :: 0;\nmain :: () {\n" + "".join(
            f"    x{index} := counter; // 😀\r\n" for index in range(2000)) + "}\n"
        text = padding + body
        uri = self.uri("far.glo")
        self.client.open(uri, text)
        self.client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        declaration = text.index("counter ::")
        started = time.monotonic()
        highlights = self.client.request("textDocument/documentHighlight", {
            "textDocument": {"uri": uri}, "position": lsp_position(text, declaration),
        })
        elapsed = time.monotonic() - started
        offsets = [match.start() for match in re.finditer(r"\bcounter\b", text)]
        expected = [{"start": lsp_position(text, offset),
                     "end": lsp_position(text, offset + len("counter"))} for offset in offsets]
        self.assertEqual(sorted(item["range"]["start"]["line"] for item in highlights),
                         [item["start"]["line"] for item in expected])
        self.assertEqual(sorted((item["range"] for item in highlights),
                                key=lambda r: r["start"]["line"]), expected)
        # The quadratic version took tens of seconds on this input.
        self.assertLess(elapsed, 5, elapsed)
        # Same length, different lines: positions follow the edited buffer.
        edited = text.replace("counter :: 0;\nmain", "counter :: 0; main", 1)
        self.client.change(uri, [{"text": edited}], 2)
        highlights = self.client.request("textDocument/documentHighlight", {
            "textDocument": {"uri": uri}, "position": lsp_position(edited, declaration),
        })
        lines = sorted(item["range"]["start"]["line"] for item in highlights)
        expected_lines = [lsp_position(edited, match.start())["line"]
                          for match in re.finditer(r"\bcounter\b", edited)]
        self.assertEqual(lines, expected_lines)

    # F16: a waiting request keeps its id even if its JSON cannot be re-encoded.
    def test_pending_request_with_non_finite_number_keeps_its_id(self):
        uri = self.uri("finite.glo")
        self.client.open(uri, "#comptime { i := 0; while i < 50000 { i += 1; } }\nmain :: () {}\n")
        self.client.send_raw(
            ('{"jsonrpc":"2.0","id":77,"method":"textDocument/diagnostic","params":'
             '{"textDocument":{"uri":"%s"},"workDoneToken":1e999}}' % uri).encode())
        reply = self.client.wait_for(lambda message: message.get("id") == 77, timeout=30)
        self.assertEqual(reply["result"]["kind"], "full")

    # F17: lone surrogates become U+FFFD instead of rejecting the message.
    def test_lone_surrogate_escape_does_not_drop_messages(self):
        self.client.send_raw(b'{"jsonrpc":"2.0","id":31,"method":"workspace/symbol",'
                             b'"params":{"query":"\\ud800"}}')
        self.assertEqual(self.client.wait_for(lambda message: message.get("id") == 31)["result"], [])
        uri = self.uri("surrogate.glo")
        self.client.send_raw(('{"jsonrpc":"2.0","method":"textDocument/didOpen","params":'
                              '{"textDocument":{"uri":"%s","languageId":"glosso","version":1,'
                              '"text":"// \\udc00\\nvalue :: 1;\\n"}}}' % uri).encode())
        self.assertEqual(self.symbols(uri), ["value"])

    # F18: unknown flags that clients add are ignored with a warning.
    def test_unknown_flags_are_ignored(self):
        client = ArgumentClient(["--clientProcessId=1234", "--stdio"])
        self.addCleanup(client.stop)
        client.request("initialize", {"processId": None, "rootUri": None, "capabilities": {}})
        self.assertEqual(self.exit_code(client), 0)
        self.assertIn("ignoring unsupported argument --clientProcessId=1234", "".join(client.errors))
        positional = subprocess.run([str(integration.SERVER), "positional"], capture_output=True,
                                    timeout=integration.TIMEOUT)
        self.assertEqual(positional.returncode, 1)
        self.assertIn(b"unsupported argument", positional.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path, default=integration.SERVER)
    args, rest = parser.parse_known_args()
    integration.SERVER = args.server.resolve()
    unittest.main(argv=[__file__, *rest], verbosity=2)
