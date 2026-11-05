#!/usr/bin/env python3
"""Regressions for the error-tolerant lexer and parser, rename names and formatting."""
import argparse
import tempfile
import unittest
from pathlib import Path

import integration
from integration import LspClient, position


class SyntaxRegressions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="glolsp-syntax-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.client = LspClient()
        self.addCleanup(self.client.stop)
        self.client.request("initialize", {
            "processId": None, "rootUri": self.root.as_uri(),
            "capabilities": {
                "general": {"positionEncodings": ["utf-16"]},
                "textDocument": {"documentSymbol": {"hierarchicalDocumentSymbolSupport": True}},
            },
            "initializationOptions": {"scanWorkspace": False},
        })
        self.client.notify("initialized", {})
        self.version = {}

    def open(self, name, text):
        uri = (self.root / name).as_uri()
        self.version[uri] = 1
        self.client.open(uri, text)
        return uri

    def change(self, uri, text):
        self.version[uri] += 1
        self.client.change(uri, [{"text": text}], self.version[uri])

    def request(self, method, uri, **params):
        return self.client.request(method, {"textDocument": {"uri": uri}, **params})

    def symbols(self, uri):
        # Hierarchical outlines nest locals and parameters under their declaration.
        found = {}
        pending = list(self.request("textDocument/documentSymbol", uri))
        while pending:
            symbol = pending.pop(0)
            found.setdefault(symbol["name"], symbol)
            pending.extend(symbol.get("children", []))
        return found

    def format(self, uri):
        edits = self.request("textDocument/formatting", uri,
                             options={"tabSize": 4, "insertSpaces": True})
        return edits[0]["newText"] if edits else None

    def assert_alive(self):
        self.assertIsNone(self.client.process.poll(), "".join(self.client.errors))

    def test_trailing_dollar_in_parameters_keeps_server_alive(self):
        uri = self.open("dollar.glo", "x := |$")
        for text in ["S :: struct($", "main :: () {\n    f := |a, $", "f :: ($) { inner := 1; }\n"]:
            self.request("textDocument/documentSymbol", uri)
            self.change(uri, text)
        names = self.symbols(uri)
        self.assert_alive()
        self.assertIn("inner", names)
        self.assertNotIn(")", names)

    def test_lexical_error_keeps_later_tokens(self):
        body = "main :: () {\n    later := 1;\n}\n"
        uri = self.open("lexical.glo", "#\n" + body)
        for prefix in ["#\n", "'\n", "z :: 0x;\n", 'p :: "C:\\q";\n']:
            self.change(uri, prefix + body)
            self.assertIn("later", self.symbols(uri), prefix)
            data = self.request("textDocument/semanticTokens/full", uri)["data"]
            lines = set()
            line = 0
            for at in range(0, len(data), 5):
                line += data[at]
                lines.add(line)
            self.assertIn(2, lines, prefix)

    def test_incomplete_member_access_and_call_end_at_the_body(self):
        for incomplete in ["p.", "foo("]:
            source = (
                "Point :: struct { px: s32; }\n"
                "a :: () {\n    secret_local := 1;\n    p: Point;\n    " + incomplete + "\n}\n"
                "b :: () {\n    y := sec\n}\n"
            )
            uri = self.open(f"incomplete{len(incomplete)}.glo", source)
            completion = self.request("textDocument/completion", uri,
                                      position=position(source, "y := sec", offset=8))
            items = completion["items"] if isinstance(completion, dict) else completion
            self.assertNotIn("secret_local", {item["label"] for item in items}, incomplete)
            folds = self.request("textDocument/foldingRange", uri)
            self.assertIn((1, 5), {(fold["startLine"], fold["endLine"]) for fold in folds})
        member = self.open("member.glo", "Point :: struct { px: s32; }\nmain :: () {\n    p: Point;\n    p.\n}\n")
        completion = self.request("textDocument/completion", member, position={"line": 3, "character": 6})
        items = completion["items"] if isinstance(completion, dict) else completion
        self.assertIn("px", {item["label"] for item in items})

    def test_string_escape_modifier_is_part_of_the_literal(self):
        source = "S :: #string,escape\n\\\\ tab\\there\n;\nafter :: 1;\n"
        uri = self.open("escape.glo", source)
        names = self.symbols(uri)
        self.assertNotIn("escape", names)
        self.assertGreaterEqual(names["S"]["range"]["end"]["line"], 2)

    def test_rename_rejects_discard_and_literal_names(self):
        source = "value :: 1;\nmain :: () { x := value; }\n"
        uri = self.open("rename.glo", source)
        self.client.diagnostics(uri, 1)
        for name in ["_", "true", "false", "null"]:
            message = self.client.request_message("textDocument/rename", {
                "textDocument": {"uri": uri}, "position": position(source, "value"), "newName": name,
            })
            self.assertIn("error", message, name)
        renamed = self.request("textDocument/rename", uri, position=position(source, "value"),
                               newName="other")
        self.assertEqual(len(renamed["changes"][uri]), 2)

    def test_formatting_keeps_question_mark_spacing_and_compiles(self):
        source = (
            "pick :: (c: bool) -> ?ssize {\nreturn ifx c ?1 else null;\n}\n"
            "main :: () {_:=pick(true);}\n"
        )
        uri = self.open("question.glo", source)
        self.client.diagnostics(uri, 1)
        formatted = self.format(uri)
        self.assertIn("return ifx c ?1 else null;", formatted)
        self.change(uri, formatted)
        self.assertEqual(self.client.diagnostics(uri, 2), [])

    def test_formatting_preserves_token_whitespace_line_endings_and_bom(self):
        uri = self.open("comment.glo", "main :: () {\n  x := 1; // note \n}\n")
        self.assertEqual(self.format(uri), "main :: () {\n    x := 1; // note \n}\n")
        crlf = self.open("crlf.glo", "main :: () {\r\n\tx := 1; // c\r\n    /* a\r\n b */\r\n}\r\n")
        self.assertEqual(self.format(crlf), "main :: () {\r\n    x := 1; // c\r\n    /* a\r\n b */\r\n}\r\n")
        bom = self.open("bom.glo", "\ufeffmain :: () {\nx := 1;\n}\n")
        self.assertEqual(self.format(bom), "\ufeffmain :: () {\n    x := 1;\n}\n")

    def test_formatting_keeps_blank_lines_trailing_comments_and_operator_spacing(self):
        source = (
            "a :: 1;\n\n\n\nb :: 2; // two\n"
            "f :: (p: *T, t: [..]*u8) -> s32 {\n"
            "    x := *p + -1; y := !x && &z != null;\n"
            "    v := a[i] * 0..n;\n"
            "    if x == .Red return .{ .x = 1 };\n"
            "    k: Error!Value;\n"
            "}\n"
        )
        uri = self.open("spacing.glo", source)
        self.assertEqual(self.format(uri), (
            "a :: 1;\n\nb :: 2; // two\n"
            "f :: (p: *T, t: [..]*u8) -> s32 {\n"
            "    x := *p + -1;\n    y := !x && &z != null;\n"
            "    v := a[i] * 0..n;\n"
            "    if x == .Red return .{ .x = 1 };\n"
            "    k: Error!Value;\n"
            "}\n"
        ))

    def test_formatting_keeps_one_line_constructs_and_compiles(self):
        source = (
            "P::struct{x:s32;y:s32;}\n"
            "Color :: enum u8 {Red;Green;}\n"
            "one :: () -> s32 {return 1;}\n"
            "pick :: (c: Color) -> s32 {\n"
            "    p: P = .{ .x = 1, .y = 2 };\n"
            "    if p.x > 0 { p.y = 3; } else { p.y = 4; }\n"
            "    if c == {\n"
            "        case .Red; return p.x;\n"
            "        case .Green;\n"
            "        return one();\n"
            "    }\n"
            "    return p.y;\n"
            "}\n"
            "main :: () {_ := pick(.Red);}\n"
        )
        uri = self.open("one_line.glo", source)
        self.assertEqual(self.client.diagnostics(uri, 1), [])
        formatted = self.format(uri)
        self.assertEqual(formatted, (
            "P :: struct { x: s32; y: s32; }\n"
            "Color :: enum u8 { Red; Green; }\n"
            "one :: () -> s32 { return 1; }\n"
            "pick :: (c: Color) -> s32 {\n"
            "    p: P = .{ .x = 1, .y = 2 };\n"
            "    if p.x > 0 { p.y = 3; } else { p.y = 4; }\n"
            "    if c == {\n"
            "        case .Red; return p.x;\n"
            "        case .Green;\n"
            "        return one();\n"
            "    }\n"
            "    return p.y;\n"
            "}\n"
            "main :: () { _ := pick(.Red); }\n"
        ))
        self.change(uri, formatted)
        self.assertEqual(self.client.diagnostics(uri, 2), [])
        self.assertIsNone(self.format(uri))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path, nargs="?", help="server executable")
    parser.add_argument("--server", type=Path, help="server executable")
    args, rest = parser.parse_known_args()
    integration.SERVER = (args.server or args.executable or integration.SERVER).resolve()
    if not integration.SERVER.is_file():
        parser.error(f"server executable does not exist: {integration.SERVER}")
    unittest.main(argv=[__file__, *rest], verbosity=2)
