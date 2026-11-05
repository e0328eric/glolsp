#!/usr/bin/env python3
"""Regressions for navigation, rename, completion and signature help.

Each test names the bug it guards (see bugs/README.md). Run with
``python3 tests/regress_analysis.py --server build/glolsp``.
"""
import argparse
import json
import tempfile
import time
import unittest
from pathlib import Path

import integration
from integration import LspClient, names, position, source_range


class AnalysisRegressions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="glolsp-regress-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.client = LspClient()
        self.addCleanup(self.client.stop)
        self.client.request("initialize", {
            "processId": None,
            "rootUri": self.root.as_uri(),
            "capabilities": {"textDocument": {"documentSymbol": {
                "hierarchicalDocumentSymbolSupport": True,
            }}},
            "initializationOptions": {"scanWorkspace": False},
        })
        self.client.notify("initialized", {})
        self.sources = {}

    def write(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path.as_uri()

    def open(self, name, text):
        uri = self.write(name, text)
        self.sources[uri] = text
        self.client.open(uri, text)
        self.client.diagnostics(uri, 1)
        return uri

    def message(self, method, uri, needle, occurrence=0, offset=0, **extra):
        return self.client.request_message("textDocument/" + method, {
            "textDocument": {"uri": uri},
            "position": position(self.sources[uri], needle, occurrence, offset),
            **extra,
        })

    def at(self, method, uri, needle, occurrence=0, offset=0, **extra):
        reply = self.message(method, uri, needle, occurrence, offset, **extra)
        self.assertNotIn("error", reply, reply)
        return reply["result"]

    def definition(self, uri, needle, occurrence=0, offset=0):
        result = self.at("definition", uri, needle, occurrence, offset)
        if isinstance(result, list):
            self.assertEqual(len(result), 1)
            result = result[0]
        return result

    def starts(self, locations):
        return sorted((item["range"]["start"]["line"], item["range"]["start"]["character"])
                      for item in locations)

    def labels(self, uri, needle, occurrence=0, offset=0):
        result = self.at("completion", uri, needle, occurrence, offset)
        return {item["label"] for item in result["items"]}

    def test_long_operator_chain_keeps_server_alive(self):
        # C2: typing recursed once per operator on the main thread's stack.
        # The trailing unterminated comment stops the compiler at lexing, so
        # only the editor index sees the chains.
        source = ("Node :: struct { next: *Node; }\n"
                  "main :: () {\n    a := 1; node: Node;\n"
                  "    sum := a" + " + a" * 6000 + ";\n"
                  "    last := node" + ".next" * 3000 + ";\n}\n/*")
        uri = self.open("chain.glo", source)
        symbols = self.client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        self.assertTrue({"sum", "last"} <= names(symbols))
        self.assertIn("sum: ssize", str(self.at("hover", uri, "sum :=")["contents"]))
        self.assertIn("last: *Node", str(self.at("hover", uri, "last :=")["contents"]))
        self.assertEqual(self.definition(uri, "a + a", 0, 4)["range"]["start"],
                         position(source, "a :="))

    def test_rename_rejects_standard_library_declarations(self):
        # D2: rename edited the installed standard library.
        uri = self.open("std.glo", '#import "Io";\nmain :: () { print("hi {}", 1); }\n')
        self.assertIsNone(self.at("prepareRename", uri, "print"))
        reply = self.message("rename", uri, "print", newName="print2")
        self.assertEqual(reply["error"]["code"], -32602)

    def test_rename_rejects_generated_references(self):
        # D3: rename edited a content-addressed cache file under ~/.cache/glosso.
        source = ('helper :: () -> s32 { return 1; }\n'
                  '#insert "gen :: () -> s32 { return helper(); }";\n'
                  'main :: () -> s32 { return helper() + gen(); }\n')
        uri = self.open("generated.glo", source)
        references = self.at("references", uri, "helper() +", context={"includeDeclaration": True})
        self.assertTrue(any(item["uri"] != uri for item in references), references)
        self.assertIsNone(self.at("prepareRename", uri, "helper() +"))
        reply = self.message("rename", uri, "helper() +", newName="renamed")
        self.assertEqual(reply["error"]["code"], -32602)
        self.assertIn("#insert", reply["error"]["message"])

    def test_local_procedures_do_not_see_enclosing_locals(self):
        # D4: a local procedure or lambda is not a closure.
        source = ('a :: "global";\n'
                  'f :: (pp: s32) -> s32 {\n'
                  '    a: s32 = 1;\n'
                  '    K :: 3;\n'
                  '    helper :: () -> s32 { return 4; }\n'
                  '    inner :: () -> str { return a; }\n'
                  '    other :: () -> s32 { return helper() + pp; }\n'
                  '    return a + other();\n'
                  '}\n')
        uri = self.open("nested.glo", source)
        self.assertEqual(self.definition(uri, "a; }")["range"], source_range(source, "a"))
        references = self.at("references", uri, "a: s32", context={"includeDeclaration": True})
        self.assertEqual(self.starts(references), [(2, 4), (7, 11)])
        renamed = self.at("rename", uri, "a: s32", newName="b")
        self.assertEqual(self.starts(renamed["changes"][uri]), [(2, 4), (7, 11)])
        labels = self.labels(uri, "pp; }")
        self.assertIn("helper", labels)
        self.assertNotIn("K", labels)
        self.assertNotIn("pp", labels)
        self.assertEqual(self.at("definition", uri, "pp; }"), None)

    def test_initializer_reads_the_outer_binding(self):
        # D5: `x := x.count` bound its right-hand `x` to itself.
        source = ('main :: () -> ssize {\n'
                  '    x: str = "abc";\n'
                  '    {\n'
                  '        x := x.count;\n'
                  '        return x;\n'
                  '    }\n'
                  '}\n')
        uri = self.open("initializer.glo", source)
        self.assertEqual(self.definition(uri, "x.count")["range"], source_range(source, "x"))
        renamed = self.at("rename", uri, "x: str", newName="text")
        self.assertEqual(self.starts(renamed["changes"][uri]), [(1, 4), (3, 13)])

    def test_rename_rejects_capturing_existing_uses(self):
        # D6: `g` below reads `p.g` through `using`; a local `g` would capture it.
        source = ('P :: struct { g: s32; h: s32; }\n'
                  'f :: (p: *P) -> s32 {\n'
                  '    a: s32 = 1;\n'
                  '    b := a;\n'
                  '    using p;\n'
                  '    return g + b;\n'
                  '}\n'
                  'top :: () -> s32 { return 1; }\n'
                  'use :: () -> s32 { local := 2; return top() + local; }\n'
                  'other :: () -> s32 { shadow := 3; return shadow; }\n')
        uri = self.open("capture.glo", source)
        reply = self.message("rename", uri, "a: s32", newName="g")
        self.assertEqual(reply["error"]["code"], -32602)
        reply = self.message("rename", uri, "top ::", newName="local")
        self.assertEqual(reply["error"]["code"], -32602)
        # An unrelated local of the same name elsewhere is no conflict.
        renamed = self.at("rename", uri, "top ::", newName="shadow")
        expected = [position(source, "top ::"), position(source, "top()")]
        self.assertEqual(self.starts(renamed["changes"][uri]),
                         [(item["line"], item["character"]) for item in expected])
        renamed = self.at("rename", uri, "a: s32", newName="count")
        self.assertEqual(len(renamed["changes"][uri]), 2)

    def test_alias_uses_stay_bound_to_the_alias(self):
        # D7: compiler facts rebound `alias(1)` to the alias's target.
        source = ('foo :: (x: s32) -> s32 { return x; }\n'
                  'alias :: foo;\n'
                  'main :: () -> s32 { return alias(1) + foo(2); }\n')
        uri = self.open("alias.glo", source)
        references = self.at("references", uri, "foo ::", context={"includeDeclaration": True})
        self.assertEqual(self.starts(references), [(0, 0), (1, 9), (2, 38)])
        references = self.at("references", uri, "alias ::", context={"includeDeclaration": True})
        self.assertEqual(self.starts(references), [(1, 0), (2, 27)])
        renamed = self.at("rename", uri, "foo ::", newName="bar")
        for edit in renamed["changes"][uri]:
            start = edit["range"]["start"]
            self.assertNotEqual((start["line"], start["character"]), (2, 27))
        self.assertEqual(self.at("prepareRename", uri, "alias(1)")["placeholder"], "alias")

    def test_document_symbols_are_not_capped(self):
        # F2: the workspace/symbol cap of 500 also truncated documentSymbol.
        source = "".join(f"g{i} :: {i};\n" for i in range(600)) + "last_function :: () {}\n"
        uri = self.open("many.glo", source)
        symbols = self.client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        self.assertEqual(len(symbols), 601)
        self.assertEqual(symbols[-1]["name"], "last_function")
        self.assertLessEqual(len(self.client.request("workspace/symbol", {"query": "g"})), 500)

    def test_hierarchical_outline_nests_declarations(self):
        # F2: locals and parameters were emitted flat beside their procedure.
        source = ('Point :: struct { x: s32; y: s32; }\n'
                  'Point ::: {\n'
                  '    norm :: (#self) -> s32 { total := #self.x + #self.y; return total; }\n'
                  '}\n'
                  'main :: (count: s32) -> s32 {\n'
                  '    local := count;\n'
                  '    helper :: (value: s32) -> s32 { doubled := value * 2; return doubled; }\n'
                  '    {\n'
                  '        inner := helper(local);\n'
                  '        return inner;\n'
                  '    }\n'
                  '}\n')
        uri = self.open("outline.glo", source)
        symbols = self.client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        tree = {symbol["name"]: symbol for symbol in symbols}
        self.assertEqual(set(tree), {"Point", "norm", "main"})

        def children(symbol):
            return {child["name"]: child for child in symbol.get("children", [])}

        self.assertEqual(set(children(tree["Point"])), {"x", "y"})
        self.assertEqual(set(children(tree["norm"])), {"#self", "total"})
        self.assertEqual(set(children(tree["main"])), {"count", "local", "helper", "inner"})
        self.assertEqual(set(children(children(tree["main"])["helper"])), {"value", "doubled"})

        def key(point):
            return point["line"], point["character"]

        def inside(inner, outer):
            return key(outer["start"]) <= key(inner["start"]) and key(inner["end"]) <= key(outer["end"])

        def check(nodes, parent=None):
            for node in nodes:
                self.assertTrue(inside(node["selectionRange"], node["range"]), node["name"])
                if parent is not None:
                    self.assertTrue(inside(node["range"], parent["range"]), node["name"])
                check(node.get("children", []), node)

        check(symbols)
        # Clients without hierarchy support keep the flat list of every symbol.
        flat_client = LspClient()
        self.addCleanup(flat_client.stop)
        flat_client.request("initialize", {
            "processId": None, "rootUri": self.root.as_uri(), "capabilities": {},
            "initializationOptions": {"scanWorkspace": False},
        })
        flat_client.notify("initialized", {})
        flat_client.open(uri, source)
        flat = flat_client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        self.assertTrue(all("location" in symbol and "children" not in symbol for symbol in flat))
        self.assertEqual(len(flat), 13)
        self.assertEqual({symbol["name"] for symbol in flat}, names(symbols))

    def test_large_documents_analyse_in_linear_time(self):
        # P2: each lookup scanned every symbol of the file per scope level.
        source = "".join(f"f{i} :: (a: s32, b: s32, c: s32) -> s32 {{\n"
                         "    x := a + b;\n    y := x + c;\n    return y + c;\n}\n"
                         for i in range(3000))
        uri = self.write("large.glo", source)
        self.sources[uri] = source
        self.client.open(uri, source)
        # The editor index answers before the compiler finishes; the old
        # quadratic analysis took about 20 s here, well past the 5 s timeout.
        started = time.monotonic()
        hover = self.at("hover", uri, "y + c", 2999)
        self.assertIn("y: ", str(hover["contents"]))
        self.client.change(uri, [{"range": {"start": {"line": 0, "character": 0},
                                            "end": {"line": 0, "character": 0}},
                                  "text": "g :: 1;\n"}])
        self.sources[uri] = "g :: 1;\n" + source
        hover = self.at("hover", uri, "x + c", 2999)
        self.assertIn("x: ", str(hover["contents"]))
        self.assertLess(time.monotonic() - started, 10)

    def test_navigation_with_cursor_after_identifier(self):
        # F6: a cursor just after a name returned nothing.
        source = ('helper :: () -> s32 { return 1; }\n'
                  'main :: () -> s32 {\n'
                  '    value := helper();\n'
                  '    return value;\n'
                  '}\n')
        uri = self.open("after.glo", source)
        end = len("value")
        self.assertEqual(self.definition(uri, "value;", 0, end)["range"],
                         source_range(source, "value"))
        self.assertIn("value", str(self.at("hover", uri, "value;", 0, end)["contents"]))
        references = self.at("references", uri, "value;", 0, end, context={"includeDeclaration": True})
        self.assertEqual(len(references), 2)
        self.assertEqual(self.at("prepareRename", uri, "value;", 0, end)["range"],
                         source_range(source, "value", 1))
        self.assertEqual(len(self.at("rename", uri, "helper()", 0, len("helper"),
                                     newName="other")["changes"][uri]), 2)

    def test_completion_after_range_operator(self):
        # F8: the second `.` of `..` was taken for member access.
        source = ('main :: () {\n'
                  '    count := 4;\n'
                  '    for i: 0..count { }\n'
                  '    values: [4]s32;\n'
                  '    slice := values[1..count];\n'
                  '}\n')
        uri = self.open("range.glo", source)
        self.assertIn("count", self.labels(uri, "count {", 0, 3))
        self.assertIn("count", self.labels(uri, "count]", 0, 3))

    def quick_fix_titles(self, uri):
        source = self.sources[uri]
        actions = self.client.request("textDocument/codeAction", {
            "textDocument": {"uri": uri},
            "range": {"start": {"line": 0, "character": 0},
                      "end": {"line": source.count("\n"), "character": 0}},
            "context": {"diagnostics": []},
        })
        return [action["title"] for action in actions]

    def open_importing_module(self):
        self.write("module.glo", '#insert "#public generated_fn :: () -> s32 { return 1; }";\n'
                                 '#public plain_fn :: () -> s32 { return 2; }\n')
        return self.open("first.glo", '#import "./module.glo";\n#import "Io";\n'
                         'main :: () -> s32 { print("x"); return generated_fn() + plain_fn(); }\n')

    def test_import_quick_fix_uses_module_names(self):
        # F9: the quick fix offered cache files and installed std paths.
        first = self.open_importing_module()
        source = 'main :: () -> s32 { print("y"); return generated_fn() + plain_fn(); }\n'
        uri = self.open("second.glo", source)
        # Opening a document invalidates every snapshot. Once first.glo is
        # checked again, its #insert output is known and relevant.
        self.client.diagnostics(first)
        titles = self.quick_fix_titles(uri)
        self.assertIn("Import 'print' from 'Io'", titles)
        self.assertIn("Import 'plain_fn' from 'module.glo'", titles)
        # A declaration generated by module.glo's #insert is imported from module.glo.
        self.assertIn("Import 'generated_fn' from 'module.glo'", titles)
        self.assertFalse(any(".cache" in title or "/std/" in title for title in titles), titles)

    def test_import_quick_fix_ignores_untitled_paths(self):
        # F9: an untitled buffer's overlay path lies in the temporary directory.
        self.open_importing_module()
        untitled = "untitled:Untitled-1"
        self.sources[untitled] = 'main :: () -> s32 { print("y"); return plain_fn(); }\n'
        self.client.open(untitled, self.sources[untitled])
        self.client.diagnostics(untitled, 1)
        titles = self.quick_fix_titles(untitled)
        self.assertIn("Import 'print' from 'Io'", titles)
        self.assertFalse(any("plain_fn" in title for title in titles), titles)
        declared = "untitled:Untitled-2"
        self.client.open(declared, "#public scratch_fn :: () -> s32 { return 3; }\n")
        self.client.diagnostics(declared, 1)
        uri = self.open("third.glo", "main :: () -> s32 { return scratch_fn(); }\n")
        titles = self.quick_fix_titles(uri)
        self.assertFalse(any("scratch_fn" in title for title in titles), titles)

    def test_signature_help_tracks_parentheses(self):
        # F14: a call that had just closed still counted, and EOF lost the call.
        source = ('f :: (a: s32, b: s32, c: s32) -> s32 { return a; }\n'
                  'g :: (x: s32, y: s32) -> s32 { return x; }\n'
                  'main :: () {\n'
                  '    f(g(1, 2) , 5, 6);\n'
                  '    f(1, 2);\n'
                  '    f(1, ')
        uri = self.open("signature.glo", source)
        help = self.at("signatureHelp", uri, " , 5")
        self.assertIn("f ::", help["signatures"][0]["label"])
        self.assertEqual(help["activeParameter"], 0)
        help = self.at("signatureHelp", uri, "2) , 5")
        self.assertIn("g ::", help["signatures"][0]["label"])
        self.assertEqual(help["activeParameter"], 1)
        self.assertIsNone(self.at("signatureHelp", uri, "f(1, 2);", 0, len("f(1, 2)")))
        help = self.at("signatureHelp", uri, "f(1, ", 1, len("f(1, "))
        self.assertIn("f ::", help["signatures"][0]["label"])
        self.assertEqual(help["activeParameter"], 1)

    def test_rename_neither_leaks_nor_rescans_per_reference(self):
        # L4 and P2: the conflict check computed and leaked every visible
        # symbol for each reference.
        status_path = Path(f"/proc/{self.client.process.pid}/status")
        if not status_path.exists():
            self.skipTest("RSS inspection requires procfs")

        def rss_kib():
            for line in status_path.read_text().splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
            self.skipTest("VmRSS is unavailable")

        source = ("helper :: (v: s32) -> s32 { return v; }\n"
                  + "".join(f"g{i} :: {i};\n" for i in range(300))
                  + "main :: () {\n"
                  + "".join(f"    x{i} := helper({i});\n" for i in range(300))
                  + "}\n")
        uri = self.open("references.glo", source)
        self.at("rename", uri, "helper(", newName="warmup")
        before = rss_kib()
        started = time.monotonic()
        for index in range(10):
            renamed = self.at("rename", uri, "helper(", newName=f"helper_{index}")
            self.assertEqual(len(renamed["changes"][uri]), 301)
        elapsed = time.monotonic() - started
        self.assertLess(rss_kib() - before, 24 * 1024)
        self.assertLess(elapsed, 30, f"10 renames took {elapsed:.1f} s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path, default=integration.SERVER)
    args, rest = parser.parse_known_args()
    integration.SERVER = args.server.resolve()
    unittest.main(argv=[__file__, *rest], verbosity=2)
