#!/usr/bin/env python3
"""Exercise the built server through its public LSP stdio interface.

Run ``python3 tests/integration.py`` after building, or pass ``--server PATH``.
The tests require only Python's standard library.
"""

import argparse
import json
import queue
import subprocess
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "build" / "glolsp"
TIMEOUT = 5


def position(text, needle, occurrence=0, offset=0):
    """Find a source location, counting LSP characters as UTF-16 code units."""
    start = -1
    for _ in range(occurrence + 1):
        start = text.index(needle, start + 1)
    start += offset
    line_start = text.rfind("\n", 0, start) + 1
    return {
        "line": text.count("\n", 0, start),
        "character": len(text[line_start:start].encode("utf-16-le")) // 2,
    }


def source_range(text, needle, occurrence=0):
    return {
        "start": position(text, needle, occurrence),
        "end": position(text, needle, occurrence, len(needle)),
    }


def names(symbols):
    result = set()
    for symbol in symbols:
        result.add(symbol["name"])
        result.update(names(symbol.get("children", [])))
    return result


class LspClient:
    def __init__(self):
        self.process = subprocess.Popen(
            [str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.messages = queue.Queue()
        self.pending = []
        self.errors = []
        self.next_id = 1
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.error_reader = threading.Thread(target=self._read_errors, daemon=True)
        self.error_reader.start()

    def _read(self):
        try:
            while True:
                headers = {}
                while True:
                    line = self.process.stdout.readline()
                    if not line:
                        self.messages.put(EOFError("Server closed stdout"))
                        return
                    if line in (b"\r\n", b"\n"):
                        break
                    key, value = line.decode("ascii").split(":", 1)
                    headers[key.lower()] = value.strip()
                length = int(headers["content-length"])
                body = self.process.stdout.read(length)
                if len(body) != length:
                    raise EOFError("Server closed stdout inside an LSP message")
                self.messages.put(json.loads(body))
        except Exception as error:
            self.messages.put(error)

    def _read_errors(self):
        for line in self.process.stderr:
            self.errors.append(line.decode("utf-8", errors="replace"))

    def send_raw(self, body):
        self.process.stdin.write(
            f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        )
        self.process.stdin.flush()

    def send(self, message):
        self.send_raw(json.dumps(message, ensure_ascii=False).encode("utf-8"))

    def notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)

    def wait_for(self, predicate, timeout=TIMEOUT):
        for index, message in enumerate(self.pending):
            if predicate(message):
                return self.pending.pop(index)
        deadline = time.monotonic() + timeout
        while True:
            try:
                message = self.messages.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise AssertionError(
                    f"Timed out waiting for LSP response; pending={self.pending!r}; "
                    f"stderr={''.join(self.errors)!r}"
                ) from None
            if isinstance(message, Exception):
                raise AssertionError(f"{message}; stderr={''.join(self.errors)!r}")
            if predicate(message):
                return message
            self.pending.append(message)

    def request_message(self, method, params=None):
        request_id = self.next_id
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        return self.wait_for(lambda reply: reply.get("id") == request_id)

    def request(self, method, params=None):
        message = self.request_message(method, params)
        if "error" in message:
            raise AssertionError(f"{method} failed: {message['error']!r}")
        if "result" not in message:
            raise AssertionError(f"Missing result in response: {message!r}")
        return message["result"]

    def initialize(self, capabilities=None):
        if capabilities is None:
            capabilities = {
                "general": {"positionEncodings": ["utf-16"]},
                "textDocument": {
                    "publishDiagnostics": {"versionSupport": True},
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                    "foldingRange": {"lineFoldingOnly": False},
                },
            }
        result = self.request("initialize", {
            "processId": None,
            "rootUri": ROOT.as_uri(),
            "initializationOptions": {"scanWorkspace": False},
            "capabilities": capabilities,
        })
        self.notify("initialized", {})
        return result["capabilities"]

    def open(self, uri, text, version=1):
        self.notify("textDocument/didOpen", {"textDocument": {
            "uri": uri, "languageId": "glosso", "version": version, "text": text,
        }})

    def change(self, uri, changes, version=2):
        self.notify("textDocument/didChange", {
            "textDocument": {"uri": uri, "version": version},
            "contentChanges": changes,
        })

    def diagnostics(self, uri, version=None):
        return self.wait_for(lambda message:
            message.get("method") == "textDocument/publishDiagnostics"
            and message.get("params", {}).get("uri") == uri
            and (version is None or message["params"].get("version", version) == version)
        )["params"]["diagnostics"]

    def stop(self):
        if self.process.poll() is None:
            try:
                self.request("shutdown")
                self.notify("exit")
                self.process.wait(timeout=TIMEOUT)
            except (AssertionError, BrokenPipeError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait(timeout=TIMEOUT)
        self.reader.join(timeout=1)
        self.error_reader.join(timeout=1)
        try:
            self.process.stdin.close()
        except BrokenPipeError:
            pass
        self.process.stdout.close()
        self.process.stderr.close()


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.client = LspClient()
        self.addCleanup(self.client.stop)
        self.capabilities = self.client.initialize()

    def params(self, uri, text, needle, occurrence=0):
        return {"textDocument": {"uri": uri}, "position": position(text, needle, occurrence)}

    def test_capabilities_and_request_errors(self):
        self.assertEqual(self.capabilities.get("positionEncoding", "utf-16"), "utf-16")
        sync = self.capabilities["textDocumentSync"]
        self.assertEqual(sync["change"] if isinstance(sync, dict) else sync, 2)
        self.assertTrue(self.capabilities.get("hoverProvider"))
        self.assertTrue(self.capabilities.get("definitionProvider"))
        self.assertTrue(self.capabilities.get("completionProvider"))
        message = self.client.request_message("glosso/unknownMethod", {})
        self.assertEqual(message["error"]["code"], -32601)
        self.client.send_raw(b'{"jsonrpc": "2.0", broken')
        message = self.client.wait_for(
            lambda reply: reply.get("id", 1) is None and "error" in reply
        )
        self.assertEqual(message["error"]["code"], -32700)
        # A malformed payload must not corrupt framing for subsequent messages.
        self.assertEqual(self.client.request("workspace/symbol", {"query": "missing"}), [])
        self.client.send({"jsonrpc": "2.0", "id": "unicode-🙂", "method": "workspace/symbol",
                          "params": {"query": "missing"}})
        message = self.client.wait_for(lambda reply: reply.get("id") == "unicode-🙂")
        self.assertEqual(message["result"], [])

    def test_fragmented_and_pipelined_frames(self):
        def frame(request_id):
            body = json.dumps({
                "jsonrpc": "2.0", "id": request_id,
                "method": "workspace/symbol", "params": {"query": "🙂"},
            }, ensure_ascii=False).encode("utf-8")
            return (
                f"cOnTeNt-LeNgTh: {len(body)}\r\n"
                "Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n\r\n"
            ).encode("ascii") + body

        first, second = frame(100), frame(101)
        self.client.process.stdin.write(first[:7])
        self.client.process.stdin.flush()
        with self.assertRaises(queue.Empty):
            self.client.messages.get(timeout=0.02)
        self.client.process.stdin.write(first[7:-2])
        self.client.process.stdin.flush()
        with self.assertRaises(queue.Empty):
            self.client.messages.get(timeout=0.02)
        self.client.process.stdin.write(first[-2:] + second)
        self.client.process.stdin.flush()
        for request_id in (100, 101):
            reply = self.client.wait_for(lambda message: message.get("id") == request_id)
            self.assertEqual(reply["result"], [])

    def test_invalid_request_params_are_rejected(self):
        uri = "file:///integration/validation.glo"
        self.client.open(uri, "value :: 1;\n")
        self.client.diagnostics(uri)
        for method, params in (
            ("workspace/symbol", []),
            ("workspace/symbol", {}),
            ("textDocument/hover", {"textDocument": {"uri": "file:///not-open.glo"},
                                    "position": {"line": 0, "character": 0}}),
            ("textDocument/codeAction", {"textDocument": {"uri": uri}}),
            ("textDocument/codeAction", {"textDocument": {"uri": uri}, "range": {
                "start": {"line": 0, "character": 5},
                "end": {"line": 0, "character": 2},
            }}),
            ("textDocument/codeAction", {"textDocument": {"uri": uri}, "range": {
                "start": {"line": 0, "character": 0},
                "end": {"line": 999, "character": 0},
            }}),
            ("textDocument/selectionRange", {"textDocument": {"uri": uri}}),
        ):
            with self.subTest(method=method, params=params):
                reply = self.client.request_message(method, params)
                self.assertEqual(reply["error"]["code"], -32602)
        reply = self.client.request_message("initialize", {"capabilities": {}})
        self.assertEqual(reply["error"]["code"], -32600)

    def test_invalid_payloads_do_not_corrupt_following_requests(self):
        cases = [
            (b"", -32700),
            (b"\xff", -32700),
            (b'{"jsonrpc":"2.0","method":"workspace/symbol","params":{"query":"\xff"}}', -32700),
            (b'{"broken":]', -32700),
            (b"{} {}", -32700),
            (b"[" * 140 + b"0" + b"]" * 140, -32700),
            (b"null", -32600),
            (b"[]", -32600),
            (b"1", -32600),
            (b'"text"', -32600),
            (b"{}", -32600),
            (b'{"jsonrpc":"1.0","id":1,"method":"workspace/symbol"}', -32600),
            (b'{"jsonrpc":"2.0","id":[],"method":"workspace/symbol"}', -32600),
            (b'{"jsonrpc":"2.0","id":true,"method":"workspace/symbol"}', -32600),
            (b'{"jsonrpc":"2.0","id":1.5,"method":"workspace/symbol"}', -32600),
        ]
        for body, expected_code in cases:
            with self.subTest(body=body[:100]):
                self.client.send_raw(body)
                reply = self.client.wait_for(
                    lambda message: message.get("id", 1) is None and "error" in message
                )
                self.assertEqual(reply["error"]["code"], expected_code)
                self.assertEqual(self.client.request("workspace/symbol", {"query": ""}), [])

    def test_navigation_completion_and_symbols(self):
        uri = "file:///integration/navigation.glo"
        text = (
            "Point :: struct {\n"
            "    x: s64;\n"
            "}\n"
            "double :: (value: s64) -> s64 {\n"
            "    return value * 2;\n"
            "}\n"
            "main :: () {\n"
            "    count := double(21);\n"
            "    result := double(count);\n"
            "}\n"
        )
        self.client.open(uri, text)
        self.assertEqual(self.client.diagnostics(uri), [])
        reference = self.params(uri, text, "double", 1)
        hover = self.client.request("textDocument/hover", reference)
        self.assertIsNotNone(hover)
        self.assertIn("double", json.dumps(hover))
        definition = self.client.request("textDocument/definition", reference)
        if isinstance(definition, list):
            self.assertEqual(len(definition), 1)
            definition = definition[0]
        self.assertEqual(definition["uri"], uri)
        self.assertEqual(definition["range"]["start"], position(text, "double"))
        references = self.client.request("textDocument/references", {
            **reference, "context": {"includeDeclaration": True},
        })
        self.assertEqual({item["range"]["start"]["line"] for item in references}, {3, 7, 8})
        completion = self.client.request("textDocument/completion", {
            "textDocument": {"uri": uri}, "position": {"line": 7, "character": 4},
        })
        items = completion["items"] if isinstance(completion, dict) else completion
        labels = {item["label"] for item in items}
        self.assertIn("double", labels)
        self.assertIn("return", labels)
        symbols = self.client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        self.assertTrue({"Point", "double", "main"}.issubset(names(symbols)))
        self.assertTrue(all("range" in symbol and "selectionRange" in symbol for symbol in symbols))
        workspace = self.client.request("workspace/symbol", {"query": "dou"})
        self.assertIn("double", names(workspace))
        if self.capabilities.get("documentHighlightProvider"):
            highlights = self.client.request("textDocument/documentHighlight", reference)
            self.assertEqual({item["range"]["start"]["line"] for item in highlights}, {3, 7, 8})
        if self.capabilities.get("foldingRangeProvider"):
            folding = self.client.request(
                "textDocument/foldingRange", {"textDocument": {"uri": uri}}
            )
            ranges = {(item["startLine"], item["endLine"]) for item in folding}
            self.assertTrue({(0, 2), (3, 5), (6, 9)}.issubset(ranges))

    def test_completion_observes_declaration_order(self):
        uri = "file:///integration/completion-scope.glo"
        text = (
            "value :: 1;\n"
            "main :: () {\n"
            "    before := value;\n"
            "    value := 2;\n"
            "    future := value;\n"
            "}\n"
        )
        self.client.open(uri, text)
        self.assertEqual(self.client.diagnostics(uri), [])
        completion = self.client.request("textDocument/completion", {
            "textDocument": {"uri": uri}, "position": {"line": 2, "character": 4},
        })
        items = completion["items"] if isinstance(completion, dict) else completion
        self.assertNotIn("future", {item["label"] for item in items})
        completion = self.client.request("textDocument/completion", {
            "textDocument": {"uri": uri}, "position": position(text, "value", 1, 1),
        })
        items = completion["items"] if isinstance(completion, dict) else completion
        self.assertIn("value", {item["label"] for item in items})

    def test_completion_resumes_after_closed_comment(self):
        uri = "file:///integration/comment-completion.glo"
        text = "main :: () { /* completed */\n}\n"
        self.client.open(uri, text)
        self.assertEqual(self.client.diagnostics(uri), [])
        completion = self.client.request("textDocument/completion", {
            "textDocument": {"uri": uri}, "position": position(text, "*/", offset=2),
        })
        items = completion["items"] if isinstance(completion, dict) else completion
        self.assertIn("return", {item["label"] for item in items})

    def test_completion_at_eof_observes_comment_and_string_boundaries(self):
        cases = (
            ("// comment", False),
            ("/* unclosed", False),
            ("/* outer /* inner */", False),
            ('"unclosed', False),
            (r"\\ text", False),
            ('"closed"', True),
            ("/* closed */", True),
        )
        for index, (text, expected_items) in enumerate(cases):
            with self.subTest(text=text):
                uri = f"file:///integration/eof-completion-{index}.glo"
                self.client.open(uri, text)
                self.client.diagnostics(uri)
                completion = self.client.request("textDocument/completion", {
                    "textDocument": {"uri": uri},
                    "position": {"line": 0, "character": len(text.encode("utf-16-le")) // 2},
                })
                items = completion["items"] if isinstance(completion, dict) else completion
                self.assertEqual(bool(items), expected_items)

    def test_directive_completion_replaces_the_hash_prefix(self):
        uri = "file:///integration/directive-completion.glo"
        text = "#im\n"
        self.client.open(uri, text)
        self.assertEqual(self.client.diagnostics(uri), [])
        completion = self.client.request("textDocument/completion", {
            "textDocument": {"uri": uri}, "position": {"line": 0, "character": 3},
        })
        items = completion["items"] if isinstance(completion, dict) else completion
        item = next(item for item in items if item["label"] == "#import")
        self.assertEqual(item["textEdit"]["range"], source_range(text, "#im"))
        self.assertEqual(item["textEdit"]["newText"], "#import")
        edit = item["textEdit"]
        completed = (
            text[:edit["range"]["start"]["character"]]
            + edit["newText"]
            + text[edit["range"]["end"]["character"]:]
        )
        self.assertEqual(completed, "#import\n")

    def test_unicode_identifiers_documentation_and_rename(self):
        uri = "file:///integration/한글.glo"
        text = (
            "/// Double a value safely.\n"
            "배가 :: (값: s64) -> s64 {\n"
            "    return 값 * 2;\n"
            "}\n"
            "main :: () {\n"
            "    banner := \"🙂\"; result := 배가(21);\n"
            "}\n"
        )
        self.client.open(uri, text)
        self.assertEqual(self.client.diagnostics(uri), [])
        reference = self.params(uri, text, "배가", 1)
        hover = self.client.request("textDocument/hover", reference)
        self.assertIsNotNone(hover)
        self.assertIn("Double a value safely.", str(hover))
        self.assertIn("배가", str(hover))
        definition = self.client.request("textDocument/definition", reference)
        if isinstance(definition, list):
            self.assertEqual(len(definition), 1)
            definition = definition[0]
        self.assertEqual(definition["uri"], uri)
        self.assertEqual(definition["range"], source_range(text, "배가"))
        prepared = self.client.request("textDocument/prepareRename", reference)
        self.assertEqual(prepared["range"], source_range(text, "배가", 1))
        renamed = self.client.request("textDocument/rename", {**reference, "newName": "두배"})
        changes = renamed["changes"][uri]
        self.assertEqual(len(changes), 2)
        self.assertTrue(all(item["newText"] == "두배" for item in changes))
        self.assertEqual(
            [item["range"] for item in changes],
            [source_range(text, "배가"), source_range(text, "배가", 1)],
        )

    def test_semantic_tokens_are_ordered_utf16_spans(self):
        provider = self.capabilities.get("semanticTokensProvider")
        if not provider or not provider.get("full"):
            self.skipTest("Full semantic tokens are not advertised")
        legend = provider["legend"]
        token_types = legend["tokenTypes"]
        modifiers = legend["tokenModifiers"]
        self.assertTrue(token_types)
        uri = "file:///integration/semantic.glo"
        text = (
            "main :: () {\r\n"
            "    /* block\r\n"
            "       comment 🙂 */\r\n"
            "    text := \"🙂\"; count := 2;\r\n"
            "}\r\n"
        )
        self.client.open(uri, text)
        self.assertEqual(self.client.diagnostics(uri), [])
        tokens = self.client.request(
            "textDocument/semanticTokens/full", {"textDocument": {"uri": uri}}
        )
        data = tokens["data"]
        self.assertTrue(data)
        self.assertEqual(len(data) % 5, 0)
        lines = text.splitlines()
        line, character, previous_end = 0, 0, 0
        seen = set()
        for index in range(0, len(data), 5):
            delta_line, delta_start, length, kind, modifier_mask = data[index:index + 5]
            self.assertGreaterEqual(delta_line, 0)
            self.assertGreaterEqual(delta_start, 0)
            self.assertGreater(length, 0)
            if delta_line:
                line += delta_line
                character = delta_start
                previous_end = 0
            else:
                character += delta_start
            self.assertGreaterEqual(character, previous_end)
            self.assertLess(line, len(lines))
            raw_line = lines[line].encode("utf-16-le")
            self.assertLessEqual((character + length) * 2, len(raw_line))
            # Decoding fails if a token boundary divides a surrogate pair.
            spelling = raw_line[character * 2:(character + length) * 2].decode("utf-16-le")
            self.assertGreaterEqual(kind, 0)
            self.assertLess(kind, len(token_types))
            self.assertGreaterEqual(modifier_mask, 0)
            self.assertLess(modifier_mask, 1 << len(modifiers))
            seen.add((spelling, token_types[kind]))
            previous_end = character + length
        self.assertIn(("main", "function"), seen)
        self.assertTrue(any("🙂" in spelling for spelling, _ in seen))
        self.client.change(uri, [{"text": ""}])
        self.assertEqual(self.client.diagnostics(uri, 2), [])
        self.assertEqual(self.client.request("textDocument/semanticTokens/full", {
            "textDocument": {"uri": uri},
        })["data"], [])

    def test_repeated_document_updates_have_bounded_memory(self):
        status_path = Path(f"/proc/{self.client.process.pid}/status")
        if not status_path.exists():
            self.skipTest("RSS inspection requires procfs")

        def rss_kib():
            for line in status_path.read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
            self.skipTest("VmRSS is unavailable")

        text = "main :: () {}\n/*" + "x" * (128 * 1024) + "*/\n"

        def cycle(index):
            uri = f"file:///integration/repeated-{index}.glo"
            self.client.open(uri, text)
            self.assertEqual(self.client.diagnostics(uri), [])
            self.client.change(uri, [{"text": text.replace("main", "changed", 1)}])
            self.assertEqual(self.client.diagnostics(uri, 2), [])
            self.client.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
            self.assertEqual(self.client.diagnostics(uri), [])

        for index in range(8):
            cycle(index)
        before = rss_kib()
        for index in range(8, 104):
            cycle(index)
        self.assertEqual(self.client.request("workspace/symbol", {"query": ""}), [])
        growth = rss_kib() - before
        self.assertLess(growth, 8 * 1024, f"RSS grew by {growth} KiB after closing 96 documents")

    def test_rename_respects_local_shadowing_and_noncode_text(self):
        uri = "file:///integration/shadowing.glo"
        text = (
            "main :: () {\n"
            "    value := 1;\n"
            "    first := value;\n"
            "    {\n"
            "        value := 2;\n"
            "        second := value;\n"
            "    }\n"
            "    last := value;\n"
            "    text := \"value\";\n"
            "    // value\n"
            "}\n"
        )
        self.client.open(uri, text)
        self.assertEqual(self.client.diagnostics(uri), [])
        outer = self.params(uri, text, "value", 1)
        references = self.client.request("textDocument/references", {
            **outer, "context": {"includeDeclaration": True},
        })
        self.assertEqual({item["range"]["start"]["line"] for item in references}, {1, 2, 7})
        inner = self.client.request("textDocument/definition", self.params(uri, text, "value", 3))
        if isinstance(inner, list):
            inner = inner[0]
        self.assertEqual(inner["range"]["start"]["line"], 4)
        rename_provider = self.capabilities.get("renameProvider")
        if isinstance(rename_provider, dict) and rename_provider.get("prepareProvider"):
            prepared = self.client.request("textDocument/prepareRename", outer)
            self.assertIsNotNone(prepared)
        edit = self.client.request("textDocument/rename", {**outer, "newName": "renamed"})
        if "changes" in edit:
            self.assertEqual(set(edit["changes"]), {uri})
            changes = edit["changes"][uri]
        else:
            documents = edit["documentChanges"]
            self.assertEqual(len(documents), 1)
            self.assertEqual(documents[0]["textDocument"]["uri"], uri)
            changes = documents[0]["edits"]
        self.assertEqual(len(changes), 3)
        self.assertEqual({item["range"]["start"]["line"] for item in changes}, {1, 2, 7})
        self.assertTrue(all(item["newText"] == "renamed" for item in changes))
        for invalid_name in ("return", "not-an-identifier", "first"):
            with self.subTest(new_name=invalid_name):
                reply = self.client.request_message("textDocument/rename", {
                    **outer, "newName": invalid_name,
                })
                self.assertEqual(reply["error"]["code"], -32602)

    def test_incremental_utf16_crlf_and_full_document_changes(self):
        uri = "file:///integration/unicode.glo"
        text = (
            "main :: () {\r\n"
            "    text := \"🙂\"; value := 1;\r\n"
            "    copy := value;\r\n"
            "}\r\n"
        )
        self.client.open(uri, text)
        self.assertEqual(self.client.diagnostics(uri), [])
        # The first edit starts after a non-BMP character: its UTF-16 offset
        # differs from both its Python character index and its UTF-8 byte index.
        changes = [
            {"range": source_range(text, "value", 0), "text": "number"},
            {"range": source_range(text, "value", 1), "text": "number"},
        ]
        self.client.change(uri, changes)
        changed = text.replace("value", "number")
        self.assertEqual(self.client.diagnostics(uri, 2), [])
        definition = self.client.request(
            "textDocument/definition", self.params(uri, changed, "number", 1),
        )
        if isinstance(definition, list):
            self.assertEqual(len(definition), 1)
            definition = definition[0]
        self.assertEqual(definition["range"], source_range(changed, "number"))
        self.client.change(uri, [{"text": "replacement :: () {}\n"}], version=3)
        self.assertEqual(self.client.diagnostics(uri, 3), [])
        symbols = self.client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        self.assertEqual(names(symbols), {"replacement"})

    def test_stale_versions_and_invalid_edits_leave_document_intact(self):
        uri = "file:///integration/versions.glo"
        self.client.open(uri, "original :: () {}\n")
        self.assertEqual(self.client.diagnostics(uri), [])
        self.client.change(uri, [{"text": "retained :: () {}\n"}], version=3)
        self.assertEqual(self.client.diagnostics(uri, 3), [])
        self.client.change(uri, [{"text": "stale :: () {}\n"}], version=2)
        # A request is a processing barrier for preceding notifications.
        symbols = self.client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        self.assertEqual(names(symbols), {"retained"})
        self.client.change(uri, [
            {"text": "partial :: () {}\n"},
            {"range": {"start": {"line": 99, "character": 0},
                       "end": {"line": 99, "character": 1}}, "text": "invalid"},
        ], version=4)
        symbols = self.client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        self.assertEqual(names(symbols), {"retained"})
        self.client.change(uri, [{"text": "final :: () {}\n"}], version=4)
        self.assertEqual(self.client.diagnostics(uri, 4), [])
        symbols = self.client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        self.assertEqual(names(symbols), {"final"})

    def test_diagnostics_update_and_close(self):
        uri = "file:///integration/diagnostics.glo"
        self.client.open(uri, "main :: () {\n")
        diagnostics = self.client.diagnostics(uri)
        self.assertTrue(diagnostics)
        for diagnostic in diagnostics:
            self.assertIn("range", diagnostic)
            self.assertTrue(diagnostic["message"])
        self.client.change(uri, [{"text": "main :: () {}\n"}])
        self.assertEqual(self.client.diagnostics(uri, 2), [])
        self.client.change(uri, [{"text": "/* unterminated comment"}], version=3)
        self.assertTrue(self.client.diagnostics(uri, 3))
        self.client.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
        self.assertEqual(self.client.diagnostics(uri), [])
        self.assertEqual(self.client.request("workspace/symbol", {"query": "main"}), [])
        self.client.open(uri, "reopened :: () {}\n")
        self.assertEqual(self.client.diagnostics(uri), [])
        reopened = self.client.request("workspace/symbol", {"query": ""})
        self.assertEqual(names(reopened), {"reopened"})

    def test_shutdown_and_exit(self):
        self.assertIsNone(self.client.request("shutdown"))
        reply = self.client.request_message("workspace/symbol", {"query": ""})
        self.assertEqual(reply["error"]["code"], -32600)
        self.client.notify("exit")
        self.assertEqual(self.client.process.wait(timeout=TIMEOUT), 0)


class LifecycleTests(unittest.TestCase):
    def test_clients_without_hierarchical_symbols_and_with_line_only_folding(self):
        client = LspClient()
        self.addCleanup(client.stop)
        capabilities = client.initialize({
            "textDocument": {"foldingRange": {"lineFoldingOnly": True}},
        })
        uri = "file:///integration/compatibility.glo"
        client.open(uri, "main :: () {\n    value := 1;\n}\n")
        self.assertEqual(client.diagnostics(uri), [])
        symbols = client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        self.assertIn("main", names(symbols))
        self.assertTrue(all("location" in symbol and "range" not in symbol for symbol in symbols))
        self.assertTrue(all(symbol["location"]["uri"] == uri for symbol in symbols))
        if capabilities.get("foldingRangeProvider"):
            folding = client.request("textDocument/foldingRange", {"textDocument": {"uri": uri}})
            self.assertTrue(folding)
            self.assertTrue(all(
                "startCharacter" not in item and "endCharacter" not in item for item in folding
            ))

    def test_requests_before_initialization_are_rejected(self):
        client = LspClient()
        self.addCleanup(client.stop)
        message = client.request_message("workspace/symbol", {"query": "main"})
        self.assertEqual(message["error"]["code"], -32002)
        client.initialize()

    def test_exit_without_shutdown_is_unsuccessful(self):
        client = LspClient()
        self.addCleanup(client.stop)
        client.notify("exit")
        self.assertEqual(client.process.wait(timeout=TIMEOUT), 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path, nargs="?", help="server executable")
    parser.add_argument("--server", type=Path, help="server executable")
    args, unittest_args = parser.parse_known_args()
    SERVER = (args.server or args.executable or SERVER).resolve()
    if not SERVER.is_file():
        parser.error(f"server executable does not exist: {SERVER}; build it with first.glo first")
    unittest.main(argv=[__file__, *unittest_args], verbosity=2)
