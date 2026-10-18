#!/usr/bin/env python3
"""Semantic LSP regressions using temporary, multi-file Glosso projects."""

import argparse
import tempfile
import unittest
from pathlib import Path

import integration
from integration import LspClient, names, position, source_range


class SemanticLspTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="glolsp-semantic-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.client = LspClient()
        self.addCleanup(self.client.stop)
        self.documents = {}

    def write(self, name, source):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        self.documents[path.as_uri()] = source
        return path.as_uri()

    def initialize(self, root_uri=None, watch_registration=False, **options):
        result = self.client.request("initialize", {
            "processId": None,
            "rootUri": root_uri or self.root.as_uri(),
            "capabilities": {
                "general": {"positionEncodings": ["utf-16"]},
                "textDocument": {"documentSymbol": {
                    "hierarchicalDocumentSymbolSupport": True,
                }},
                "workspace": {"didChangeWatchedFiles": {
                    "dynamicRegistration": watch_registration,
                }},
            },
            "initializationOptions": {"scanWorkspace": True, **options},
        })
        self.client.notify("initialized", {})
        return result["capabilities"]

    def open(self, uri, source=None, version=1):
        if source is None:
            source = self.documents[uri]
        self.documents[uri] = source
        self.client.open(uri, source, version)
        self.client.diagnostics(uri, version)

    def at(self, method, uri, needle, occurrence=0, offset=0, **extra):
        result = self.client.request(method, {
            "textDocument": {"uri": uri},
            "position": position(self.documents[uri], needle, occurrence, offset),
            **extra,
        })
        if method in {"textDocument/definition", "textDocument/typeDefinition"}:
            if result is None:
                return []
            if isinstance(result, dict):
                return [result]
        return result

    def completion_names(self, uri, needle, offset=None):
        if offset is None:
            offset = len(needle)
        result = self.at("textDocument/completion", uri, needle, offset=offset)
        items = result["items"] if isinstance(result, dict) else result
        return {item["label"] for item in items}

    def test_cross_file_exports_and_alias_member_resolution(self):
        dependency = self.write("types.glo", (
            "#public Point :: struct { x: ssize; y: ssize; }\n"
            "#public create :: () -> Point { return .{ .x = 1, .y = 2 }; }\n"
            "secret :: 1;\n"
        ))
        self.write("module.glo", '#export "types.glo";\n')
        caller = self.write("main.glo", (
            'Shapes :: #load "module.glo";\n'
            "main :: () { item := Shapes.create(); item.x; Shapes.secret; }\n"
        ))
        self.initialize()
        self.open(caller)
        definition = self.at("textDocument/definition", caller, "item.x", offset=5)
        self.assertEqual(definition[0]["uri"], dependency)
        self.assertEqual(definition[0]["range"], source_range(self.documents[dependency], "x"))
        hover = self.at("textDocument/hover", caller, "item.x", offset=5)
        self.assertIn("ssize", str(hover["contents"]))
        self.assertEqual(self.at("textDocument/definition", caller, "secret"), [])
        labels = self.completion_names(caller, "item.")
        self.assertTrue({"x", "y"}.issubset(labels))
        module_labels = self.completion_names(caller, "Shapes.")
        self.assertTrue({"Point", "create"}.issubset(module_labels))
        self.assertNotIn("secret", module_labels)

    def test_return_generic_alias_and_nested_member_inference(self):
        uri = self.write("main.glo", (
            "Point :: struct { x: ssize; }\n"
            "Child :: struct { point: Point; }\n"
            "Alias :: Point;\n"
            "Box :: struct { value: $T; }\n"
            "identity :: (value: $T) -> T { return value; }\n"
            "make :: () -> Point { result: Point; return result; }\n"
            "main :: () {\n"
            "    point: Alias; child: Child; boxed: Box(Point);\n"
            "    result := make(); generic := identity(point);\n"
            "    child.point.x; boxed.value.x; result.x; generic.x;\n"
            "}\n"
        ))
        self.initialize()
        self.open(uri)
        expected = source_range(self.documents[uri], "x")
        for access in ["child.point.x", "boxed.value.x", "result.x", "generic.x"]:
            with self.subTest(access=access):
                result = self.at("textDocument/definition", uri, access, offset=len(access) - 1)
                self.assertEqual(result, [{"uri": uri, "range": expected}])
        hover = self.at("textDocument/hover", uri, "generic :=")
        self.assertIn("Point", str(hover["contents"]))

    def test_string_slice_arguments_preserve_their_type(self):
        uri = self.write("main.glo", (
            "Token :: struct { start: ssize; end: ssize; }\n"
            "semantic_builtin_type :: (name: string) -> bool { return false; }\n"
            "main :: () {\n"
            '    source := "hello"; token: Token;\n'
            "    semantic_builtin_type(source[token.start..token.end]);\n"
            "    text := source[token.start..token.end];\n"
            "    text.count;\n"
            "    semantic_builtin_type(source[token.start]);\n"
            "}\n"
        ))
        self.initialize()
        self.open(uri)
        diagnostics = self.client.request("textDocument/diagnostic", {
            "textDocument": {"uri": uri},
        })["items"]
        self.assertEqual([(item["range"], item["message"]) for item in diagnostics], [(
            source_range(self.documents[uri], "source[token.start]"),
            "Argument type does not match the procedure parameter.",
        )])
        hover = self.at("textDocument/hover", uri, "text :=")
        self.assertIn("string", str(hover["contents"]))

    def test_pointer_fields_support_nested_access_addresses_and_indexing(self):
        uri = self.write("main.glo", (
            "Leaf :: struct { value: ssize; }\n"
            "Outer :: struct { leaf: Leaf; next: *Leaf; items: [2]Leaf; }\n"
            "Box :: struct { item: $T; }\n"
            "main :: () {\n"
            "    leaf: Leaf; pointer := &leaf;\n"
            "    outer: Outer = .{ .leaf = leaf, .next = pointer, .items = .[leaf, leaf] };\n"
            "    nested := &outer;\n"
            "    pointer.value = 7; pointer.value += 1; pointer.*.value;\n"
            "    nested.leaf.value; nested.next.value; nested.items[0].value;\n"
            "    address := &pointer.value; postfix := pointer.value.&;\n"
            "    box: Box(Leaf); generic := &box; generic.item.value;\n"
            "}\n"
        ))
        self.initialize()
        self.open(uri)
        diagnostics = self.client.request("textDocument/diagnostic", {
            "textDocument": {"uri": uri},
        })["items"]
        self.assertEqual(diagnostics, [])
        for access in ["pointer.value", "pointer.*.value", "nested.leaf.value",
                       "nested.next.value", "nested.items[0].value", "generic.item.value"]:
            with self.subTest(access=access):
                definition = self.at("textDocument/definition", uri, access,
                                     offset=len(access) - len("value"))
                self.assertEqual(definition, [{
                    "uri": uri, "range": source_range(self.documents[uri], "value"),
                }])
        for name in ["address :=", "postfix :="]:
            hover = self.at("textDocument/hover", uri, name)
            self.assertIn("*ssize", str(hover["contents"]))
        self.assertIn("value", self.completion_names(uri, "nested.next."))

    def test_pointer_receivers_bind_and_specialize_generic_inherent_methods(self):
        uri = self.write("main.glo", (
            "Leaf :: struct { value: ssize; }\n"
            "Box :: struct { item: $T; }\n"
            "Leaf ::: #public {\n"
            "    #memory { reads(#self), noescape(#self) }\n"
            "    get :: (*const #self) -> ssize { return #self.value; }\n"
            "    set :: (noalias *#self, value: ssize) { #self.value = value; }\n"
            "}\n"
            "Box($T) ::: #public {\n"
            "    read :: (*const #self) -> T { return #self.item; }\n"
            "}\n"
            "main :: () {\n"
            "    leaf: Leaf; pointer := &leaf; pointer.set(7);\n"
            "    Leaf.set(pointer, 9); number := pointer.get();\n"
            "    box: Box(Leaf); box_pointer := &box;\n"
            "    selected := box_pointer.read(); selected.value;\n"
            "}\n"
        ))
        self.initialize()
        self.open(uri)
        diagnostics = self.client.request("textDocument/diagnostic", {
            "textDocument": {"uri": uri},
        })["items"]
        self.assertEqual(diagnostics, [])
        for call in ["pointer.set(7", "Leaf.set(pointer, 9"]:
            signature = self.at("textDocument/signatureHelp", uri, call, offset=len(call))
            parameters = signature["signatures"][0]["parameters"]
            self.assertEqual(parameters[signature["activeParameter"]]["label"], "value: ssize")
        for name, expected in [("selected :=", "Leaf"), ("number :=", "ssize")]:
            hover = self.at("textDocument/hover", uri, name)
            self.assertIn(expected, str(hover["contents"]))
        definition = self.at("textDocument/definition", uri, "selected.value", offset=9)
        self.assertEqual(definition, [{
            "uri": uri, "range": source_range(self.documents[uri], "value"),
        }])
        receiver = self.at("textDocument/definition", uri, "#self.value")
        self.assertEqual(receiver, [{
            "uri": uri, "range": source_range(self.documents[uri], "#self", 2),
        }])

    def test_pattern_payload_types_and_capture_scopes(self):
        uri = self.write("main.glo", (
            "Point :: struct { x: ssize; }\n"
            "Option :: union($T: type) { Some(T); None; }\n"
            "Result :: union($E: type, $T: type) { Ok(T); Err(E); }\n"
            "main :: () {\n"
            "    optional: ?Point; outcome: string!Point;\n"
            "    if #pattern .Some(payload) = optional { payload.x; } else { payload; }\n"
            "    payload;\n"
            "    if #pattern .Ok(success) = outcome { success.x; } else { success; }\n"
            "    if #pattern .Err(failure) = outcome { failure; }\n"
            "}\n"
        ))
        self.initialize()
        self.open(uri)
        for name, access in [("payload", "payload.x"), ("success", "success.x")]:
            with self.subTest(capture=name):
                declaration = self.at("textDocument/definition", uri, access)
                self.assertEqual(declaration, [{
                    "uri": uri, "range": source_range(self.documents[uri], name),
                }])
                field = self.at("textDocument/definition", uri, access, offset=len(access) - 1)
                self.assertEqual(field, [{
                    "uri": uri, "range": source_range(self.documents[uri], "x"),
                }])
                inferred = self.at("textDocument/typeDefinition", uri, access)
                self.assertEqual(inferred, [{
                    "uri": uri, "range": source_range(self.documents[uri], "Point"),
                }])
        self.assertEqual(self.at("textDocument/definition", uri, "payload", occurrence=2), [])
        self.assertEqual(self.at("textDocument/definition", uri, "payload", occurrence=3), [])
        self.assertEqual(self.at("textDocument/definition", uri, "success", occurrence=2), [])
        failure = self.at("textDocument/hover", uri, "failure", occurrence=1)
        self.assertIn("string", str(failure["contents"]))

    def test_tuple_destructuring_preserves_each_element_type(self):
        uri = self.write("main.glo", (
            "Point :: struct { x: ssize; }\n"
            "Child :: struct { point: Point; }\n"
            "pair :: () -> (Point, Child) { a: Point; b: Child; return a, b; }\n"
            "main :: () { first, second := pair(); first.x; second.point.x; }\n"
        ))
        self.initialize()
        self.open(uri)
        for name, expected_type, access in [
            ("first", "Point", "first.x"), ("second", "Child", "second.point.x"),
        ]:
            with self.subTest(binding=name):
                declaration = self.at("textDocument/definition", uri, access)
                self.assertEqual(declaration, [{
                    "uri": uri, "range": source_range(self.documents[uri], name),
                }])
                inferred = self.at("textDocument/typeDefinition", uri, access)
                self.assertEqual(inferred, [{
                    "uri": uri, "range": source_range(self.documents[uri], expected_type),
                }])
                field = self.at("textDocument/definition", uri, access, offset=len(access) - 1)
                self.assertEqual(field, [{
                    "uri": uri, "range": source_range(self.documents[uri], "x"),
                }])

    def test_references_and_rename_include_unopened_workspace_files(self):
        declaration = self.write("types.glo", "#public Shared :: struct { value: ssize; }\n")
        first = self.write("first.glo", '#load "types.glo";\nfirst: Shared;\n')
        second = self.write("second.glo", '#load "types.glo";\nsecond: Shared;\n')
        self.initialize()
        self.open(first)
        references = self.at("textDocument/references", first, "Shared", context={
            "includeDeclaration": True,
        })
        self.assertEqual({item["uri"] for item in references}, {declaration, first, second})
        edits = self.at("textDocument/rename", first, "Shared", newName="Renamed")
        self.assertEqual(set(edits["changes"]), {declaration, first, second})
        for uri, changes in edits["changes"].items():
            self.assertEqual(changes, [{
                "range": source_range(self.documents[uri], "Shared"), "newText": "Renamed",
            }])

    def test_rename_respects_shadowed_symbol_identities(self):
        uri = self.write("main.glo", (
            "main :: () { value := 1; { value := 2; value; } value; }\n"
        ))
        self.initialize()
        self.open(uri)
        edits = self.at("textDocument/rename", uri, "value", newName="outer")
        self.assertEqual(edits["changes"][uri], [
            {"range": source_range(self.documents[uri], "value", 0), "newText": "outer"},
            {"range": source_range(self.documents[uri], "value", 3), "newText": "outer"},
        ])

    def test_configured_directory_module_and_import_document_links(self):
        module = self.write("modules/Shapes/module.glo", "#public Square :: struct {}\n")
        caller = self.write("main.glo", '#import "Shapes";\nshape: Square;\n')
        self.initialize(importPaths=[str(self.root / "modules")])
        self.open(caller)
        definition = self.at("textDocument/definition", caller, "Square")
        self.assertEqual(definition[0]["uri"], module)
        links = self.client.request("textDocument/documentLink", {"textDocument": {"uri": caller}})
        self.assertEqual([link["target"] for link in links], [module])

    def test_import_path_completion_discovers_modules_without_workspace_scanning(self):
        self.write("std/Standard.glo", "")
        self.write("modules/Single.glo", "")
        self.write("modules/Shapes/module.glo", "")
        self.write("modules/Nested/Child.glo", "")
        self.write("Workspace.glo", "")
        self.write("modules/notes.txt", "")
        self.write("modules/build/Generated.glo", "")
        caller = self.write("main.glo", '#import "";\n')
        capabilities = self.initialize(stdPath=str(self.root / "std"),
                                       importPaths=[str(self.root / "modules")],
                                       scanWorkspace=False)
        self.assertTrue({'"', '/'}.issubset(
            capabilities["completionProvider"]["triggerCharacters"]))
        self.open(caller)
        completion = self.at("textDocument/completion", caller, '"', offset=1)
        self.assertFalse(completion["isIncomplete"])
        items = {item["label"]: item for item in completion["items"]}
        for label in ("Standard", "Single", "Shapes", "Workspace"):
            self.assertIn(label, items)
            self.assertEqual(items[label]["kind"], 9)
            self.assertEqual(items[label]["textEdit"], {
                "range": {"start": {"line": 0, "character": 9},
                          "end": {"line": 0, "character": 9}},
                "newText": label,
            })
        self.assertEqual(items["Nested/"]["kind"], 19)
        self.assertIn("Shapes/", items)
        for label in ("Single.glo", "notes", "notes.txt", "build/", "return"):
            self.assertNotIn(label, items)

    def test_import_path_completion_replaces_partial_and_unterminated_paths(self):
        self.write("modules/Nested/Child.glo", "")
        self.write("modules/Nested/Choice/module.glo", "")
        self.write("modules/Single.glo", "")
        caller = (self.root / "main.glo").as_uri()
        self.initialize(stdPath=str(self.root / "std"),
                        importPaths=[str(self.root / "modules")], scanWorkspace=False)
        cases = (
            ('#import "Si";\n', "Si", "Single", 2),
            ('Alias :: #import "Single";\n', "Single", "Single", 2),
            ('#import,only (thing) /* path */ "Si";\n', "Si", "Single", 2),
            ('#import "Nested/Ch";\n', "Nested/Ch", "Nested/Child", 9),
            ('#import "Nested/";\n', "Nested/", "Nested/Child", 7),
            ('#import "Si', "Si", "Single", 2),
            ('#import "Si\nnext :: 1;\n', "Si", "Single", 2),
            ('/* 🙂 */ #import "Si";\r\n', "Si", "Single", 2),
            ('#import "', "", "Single", 0),
        )
        for version, (source, path, expected, offset) in enumerate(cases, start=1):
            with self.subTest(source=source):
                if version == 1:
                    self.open(caller, source)
                else:
                    self.documents[caller] = source
                    self.client.change(caller, [{"text": source}], version=version)
                    self.client.diagnostics(caller, version)
                completion = self.at("textDocument/completion", caller, '"' + path,
                                     offset=offset + 1)
                items = {item["label"]: item for item in completion["items"]}
                self.assertIn(expected, items)
                start = position(source, '"' + path, offset=1)
                end = position(source, '"' + path, offset=len(path) + 1)
                self.assertEqual(items[expected]["textEdit"], {
                    "range": {"start": start, "end": end}, "newText": expected,
                })
                self.assertTrue(all(label.startswith(path[:offset]) for label in items))

    def test_import_path_completion_respects_precedence_and_ambiguity(self):
        self.write("std/Shared.glo", "")
        self.write("modules/Shared/module.glo", "")
        self.write("Shared.glo", "")
        self.write("std/Ambiguous.glo", "")
        self.write("std/Ambiguous/module.glo", "")
        self.write("modules/Ambiguous.glo", "")
        self.write("modules/Conflict.glo", "")
        self.write("modules/Conflict/module.glo", "")
        caller = self.write("main.glo", '#import "";\n')
        self.initialize(stdPath=str(self.root / "std"),
                        importPaths=[str(self.root / "modules")], scanWorkspace=False)
        self.open(caller)
        completion = self.at("textDocument/completion", caller, '"', offset=1)
        labels = [item["label"] for item in completion["items"]]
        self.assertEqual(labels.count("Shared"), 1)
        self.assertEqual(len(labels), len(set(labels)))
        self.assertNotIn("Ambiguous", labels)
        self.assertNotIn("Conflict", labels)
        self.assertIn("Ambiguous/", labels)
        self.assertIn("Conflict/", labels)

    def test_import_path_completion_includes_unsaved_modules_and_disk_changes(self):
        caller = self.write("main.glo", '#import "Fresh";\n')
        self.initialize(stdPath=str(self.root / "std"), scanWorkspace=False)
        self.open(caller)
        self.assertEqual(self.completion_names(caller, '"Fresh'), set())
        module = (self.root / "Fresh" / "module.glo").as_uri()
        self.open(module, "#public value :: 1;\n")
        self.assertEqual(self.completion_names(caller, '"Fresh'), {"Fresh", "Fresh/"})
        self.client.notify("textDocument/didClose", {"textDocument": {"uri": module}})
        self.assertEqual(self.completion_names(caller, '"Fresh'), set())
        self.write("Fresh.glo", "")
        self.assertEqual(self.completion_names(caller, '"Fresh'), {"Fresh"})
        (self.root / "Fresh.glo").unlink()
        self.assertEqual(self.completion_names(caller, '"Fresh'), set())

    def test_relative_import_completion_preserves_extensions_and_parent_paths(self):
        self.write("shared/part.glo", "")
        self.write("src/local.glo", "")
        self.write("src/children/child.glo", "")
        self.write("src/notes.txt", "")
        caller = self.write("src/main.glo", (
            '#load "lo";\n'
            '#export "../shared/pa";\n'
            'Alias :: #load "children/";\n'
        ))
        self.initialize(stdPath=str(self.root / "std"), scanWorkspace=False)
        self.open(caller)
        for prefix, expected in (("lo", "local.glo"),
                                 ("../shared/pa", "../shared/part.glo"),
                                 ("children/", "children/child.glo")):
            with self.subTest(prefix=prefix):
                result = self.at("textDocument/completion", caller, '"' + prefix,
                                 offset=len(prefix) + 1)
                self.assertEqual([item["label"] for item in result["items"]], [expected])
                self.assertEqual(result["items"][0]["kind"], 17)
                self.assertEqual(result["items"][0]["textEdit"], {
                    "range": {
                        "start": position(self.documents[caller], '"' + prefix, offset=1),
                        "end": position(self.documents[caller], '"' + prefix,
                                        offset=len(prefix) + 1),
                    },
                    "newText": expected,
                })

    def test_import_path_completion_handles_escaped_and_unicode_names(self):
        self.write('modules/한글/Quote"Name.glo', "")
        caller = self.write("main.glo", r'#import "\u{D55C}글/Qu";' + "\n")
        self.initialize(stdPath=str(self.root / "std"),
                        importPaths=[str(self.root / "modules")], scanWorkspace=False)
        self.open(caller)
        result = self.at("textDocument/completion", caller, "Qu", offset=2)
        self.assertEqual([item["label"] for item in result["items"]], ['한글/Quote"Name'])
        self.assertEqual(result["items"][0]["textEdit"], {
            "range": source_range(self.documents[caller], r"\u{D55C}글/Qu"),
            "newText": r'한글/Quote\"Name',
        })

    def test_import_path_completion_does_not_leak_into_other_strings_or_comments(self):
        self.write("Single.glo", "")
        caller = (self.root / "main.glo").as_uri()
        self.initialize(stdPath=str(self.root / "std"), scanWorkspace=False)
        cases = (
            'text :: "Si";\n',
            '// #import "Si";\n',
            '/* #import "Si"; */\n',
            '#import,only ("Si") "Single";\n',
            '#embed "Si";\n',
            '#import "../Si";\n',
            '#import "/Si";\n',
        )
        for version, source in enumerate(cases, start=1):
            with self.subTest(source=source):
                if version == 1:
                    self.open(caller, source)
                else:
                    self.documents[caller] = source
                    self.client.change(caller, [{"text": source}], version=version)
                    self.client.diagnostics(caller, version)
                self.assertEqual(self.completion_names(caller, "Si"), set())

    def test_implicit_prelude_names_resolve_without_an_import(self):
        caller = self.write("main.glo", "main :: () { exit(0); }\n")
        self.initialize()
        self.open(caller)
        definition = self.at("textDocument/definition", caller, "exit")
        self.assertTrue(definition)
        self.assertTrue(definition[0]["uri"].endswith("/Prelude/Utilities.glo"))
        hover = self.at("textDocument/hover", caller, "exit")
        self.assertIn("exit", str(hover["contents"]))

    def test_imported_typeclass_operations_follow_exports_and_aliases(self):
        dependency = self.write("modules/Errors.glo", (
            "#public Error :: typeclass (E: type) {\n"
            "    report_error :: (error: E) -> void;\n"
            "}\n"
            "Hidden :: typeclass (T: type) { hidden_operation :: (value: T); }\n"
            "#public Record :: struct { field: ssize; }\n"
        ))
        self.write("modules/Facade/module.glo", '#export "../Errors.glo";\n')
        caller = self.write("main.glo", (
            '#import "Facade";\n'
            'Errors :: #import "Errors";\n'
            'main :: () { report_error("failure"); Errors.report_error("failure"); }\n'
        ))
        self.initialize(importPaths=[str(self.root / "modules")])
        self.open(caller)
        diagnostics = self.client.request("textDocument/diagnostic", {
            "textDocument": {"uri": caller},
        })["items"]
        self.assertEqual(diagnostics, [])
        expected = [{
            "uri": dependency, "range": source_range(self.documents[dependency], "report_error"),
        }]
        for occurrence in (0, 1):
            self.assertEqual(self.at("textDocument/definition", caller, "report_error",
                                     occurrence=occurrence), expected)
        hover = self.at("textDocument/hover", caller, "report_error")
        self.assertIn("error: E", str(hover["contents"]))
        for needle in ("report_error", "Errors."):
            offset = 0 if needle == "report_error" else len(needle)
            labels = self.completion_names(caller, needle, offset=offset)
            self.assertIn("report_error", labels)
            self.assertTrue({"hidden_operation", "E", "T", "error", "field"}.isdisjoint(labels))
        references = self.at("textDocument/references", caller, "report_error",
                             context={"includeDeclaration": False})
        self.assertEqual(len([item for item in references if item["uri"] == caller]), 2)

    def test_typeclass_imports_preserve_visibility_and_export_boundaries(self):
        self.write("modules/Dependency.glo", (
            "#public Public :: typeclass (T: type) { public_operation :: (value: T); }\n"
            "Private :: typeclass (T: type) { private_operation :: (value: T); }\n"
            "#public(unit) Unit :: typeclass (T: type) { unit_operation :: (value: T); }\n"
        ))
        self.write("modules/Wrapper.glo", '#import "Dependency";\n')
        caller = self.write("main.glo", (
            '#import "Wrapper";\n'
            'Dependency :: #import "Dependency";\n'
            "main :: () {\n"
            "    public_operation(1); private_operation(1); unit_operation(1);\n"
            "    Dependency.public_operation(1);\n"
            "    Dependency.private_operation(1); Dependency.unit_operation(1);\n"
            "}\n"
        ))
        self.initialize(importPaths=[str(self.root / "modules")])
        self.open(caller)
        diagnostics = self.client.request("textDocument/diagnostic", {
            "textDocument": {"uri": caller},
        })["items"]
        source = self.documents[caller]
        expected = [source_range(source, name) for name in (
            "public_operation", "private_operation", "unit_operation",
        )]
        expected += [source_range(source, name, 1) for name in (
            "private_operation", "unit_operation",
        )]
        self.assertEqual([item["range"] for item in diagnostics], expected)
        self.assertTrue(self.at("textDocument/definition", caller,
                                "Dependency.public_operation", offset=len("Dependency.")))
        self.assertNotIn("public_operation", self.completion_names(
            caller, "public_operation", offset=0))

    def test_local_typeclass_operations_respect_lexical_shadowing(self):
        uri = self.write("main.glo", (
            "Local :: typeclass (T: type) { local_operation :: (value: T); }\n"
            "main :: () {\n"
            "    local_operation(1);\n"
            "    { local_operation := 1; local_operation; }\n"
            "    local_operation(2);\n"
            "}\n"
        ))
        self.initialize()
        self.open(uri)
        diagnostics = self.client.request("textDocument/diagnostic", {
            "textDocument": {"uri": uri},
        })["items"]
        self.assertEqual(diagnostics, [])
        for occurrence in (1, 4):
            self.assertEqual(self.at("textDocument/definition", uri, "local_operation",
                                     occurrence=occurrence), [{
                "uri": uri, "range": source_range(self.documents[uri], "local_operation"),
            }])
        shadowed = self.at("textDocument/definition", uri, "local_operation", occurrence=3)
        self.assertEqual(shadowed, [{
            "uri": uri, "range": source_range(self.documents[uri], "local_operation", 2),
        }])
        self.assertIn("local_operation", self.completion_names(uri, "local_operation(1)", offset=0))

    def test_standard_error_typeclass_operation_is_visible(self):
        uri = self.write("main.glo", (
            '#import "Typeclass/Error";\n'
            'main :: () { report_error("failure"); }\n'
        ))
        self.initialize()
        self.open(uri)
        self.assertEqual(self.client.request("textDocument/diagnostic", {
            "textDocument": {"uri": uri},
        })["items"], [])
        definition = self.at("textDocument/definition", uri, "report_error")
        self.assertTrue(definition)
        self.assertTrue(definition[0]["uri"].endswith("/Typeclass/Error.glo"))
        self.assertIn("report_error", self.completion_names(uri, "report_error", offset=0))

    def test_compiler_primitive_types_are_recognized_and_completed(self):
        primitive_types = [
            "Code", "Pattern", "Namespace", "Library", "label", "c32", "c64", "c128",
            "c160", "c256", "Matrix", "Simd", "s7", "i17", "u3", "s65535", "u65535",
            "int8_t", "int16_t", "int32_t", "int64_t", "__int128", "uint8_t",
            "uint16_t", "uint32_t", "uint64_t",
        ]
        source = "main :: () {\n"
        for index, name in enumerate(primitive_types):
            source += f"    value_{index}: {name}; copy_{index} := value_{index};\n"
        source += "}\n"
        uri = self.write("main.glo", source)
        self.initialize()
        self.open(uri)
        self.assertEqual(self.client.request("textDocument/diagnostic", {
            "textDocument": {"uri": uri},
        })["items"], [])
        for index, name in enumerate(primitive_types):
            with self.subTest(name=name):
                hover = self.at("textDocument/hover", uri, f"copy_{index} :=")
                self.assertIn(name, str(hover["contents"]))
        labels = self.completion_names(uri, "Code", offset=0)
        self.assertTrue(set(primitive_types[:12]).issubset(labels))
        tokens = self.client.request("textDocument/semanticTokens/full", {
            "textDocument": {"uri": uri},
        })["data"]
        line = character = 0
        styles = {}
        for index in range(0, len(tokens), 5):
            delta_line, delta_character, length, kind, _ = tokens[index:index + 5]
            line += delta_line
            character = delta_character if delta_line else character + delta_character
            spelling = source.splitlines()[line][character:character + length]
            styles[spelling] = kind
        for name in primitive_types:
            self.assertEqual(styles[name], 1, name)

    def test_invalid_primitive_type_spellings_remain_unresolved(self):
        invalid_types = ["code", "Patern", "s0", "s08", "u65536", "i99999999999999999999"]
        source = "main :: () {\n"
        for index, name in enumerate(invalid_types):
            source += f"    value_{index}: {name};\n"
        source += "}\n"
        uri = self.write("main.glo", source)
        self.initialize()
        self.open(uri)
        diagnostics = self.client.request("textDocument/diagnostic", {
            "textDocument": {"uri": uri},
        })["items"]
        self.assertEqual([item["range"] for item in diagnostics], [
            source_range(source, name) for name in invalid_types
        ])

    def test_builtin_type_names_can_be_shadowed_by_local_declarations(self):
        uri = self.write("main.glo", (
            "Code :: struct { field: ssize; }\n"
            "main :: () { value: Code; value.field; }\n"
        ))
        self.initialize()
        self.open(uri)
        self.assertEqual(self.at("textDocument/definition", uri, "value.field", offset=6), [{
            "uri": uri, "range": source_range(self.documents[uri], "field"),
        }])

    def test_blackhole_bindings_discard_values_without_declaring_a_name(self):
        uri = self.write("main.glo", (
            "foo :: () -> ssize { return 1; }\n"
            'bar :: () -> string { return "value"; }\n'
            'pair :: () -> (ssize, string) { return 1, "value"; }\n'
            "accept :: (_: ssize, _: string) {}\n"
            "main :: () {\n"
            "    _ := foo();\n"
            "    _ := bar();\n"
            "    _ = foo();\n"
            "    _, kept := pair();\n"
            "    _, _ := pair();\n"
            '    accept(1, "value");\n'
            "}\n"
        ))
        self.initialize()
        self.open(uri)
        self.assertEqual(self.client.request("textDocument/diagnostic", {
            "textDocument": {"uri": uri},
        })["items"], [])
        for occurrence in range(self.documents[uri].count("_")):
            self.assertEqual(self.at("textDocument/definition", uri, "_", occurrence), [])
        self.assertIsNone(self.at("textDocument/hover", uri, "_ :="))
        self.assertNotIn("_", self.completion_names(uri, "_ :=", offset=0))
        symbols = self.client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        self.assertNotIn("_", names(symbols))
        self.assertIn("string", str(self.at("textDocument/hover", uri, "kept")["contents"]))

    def test_blackhole_initializers_and_other_duplicate_names_are_still_checked(self):
        uri = self.write("main.glo", (
            "main :: () {\n"
            "    _ := missing();\n"
            "    _ := 1;\n"
            "    value := 1; value := 2;\n"
            "}\n"
        ))
        self.initialize()
        self.open(uri)
        diagnostics = self.client.request("textDocument/diagnostic", {
            "textDocument": {"uri": uri},
        })["items"]
        self.assertEqual([(item["range"], item["message"]) for item in diagnostics], [
            (source_range(self.documents[uri], "value", 1),
             "Duplicate declaration in the same scope."),
            (source_range(self.documents[uri], "missing"), "Unresolved identifier."),
        ])

    def test_type_definition_follows_an_inferred_procedure_return(self):
        uri = self.write("main.glo", (
            "Item :: struct { field: ssize; }\n"
            "make :: () -> Item { result: Item; return result; }\n"
            "main :: () { inferred := make(); }\n"
        ))
        self.initialize()
        self.open(uri)
        definition = self.at("textDocument/typeDefinition", uri, "inferred")
        self.assertEqual(definition, [{
            "uri": uri, "range": source_range(self.documents[uri], "Item"),
        }])

    def test_auto_import_quick_fix_adds_a_relative_load_for_a_public_type(self):
        self.write("types.glo", "#public Missing_Type :: struct {}\n")
        uri = self.write("main.glo", "item: Missing_Type;\n")
        self.initialize()
        self.open(uri)
        actions = self.client.request("textDocument/codeAction", {
            "textDocument": {"uri": uri},
            "range": source_range(self.documents[uri], "Missing_Type"),
            "context": {"diagnostics": []},
        })
        importing = [action for action in actions if action["title"].startswith("Import ")]
        self.assertTrue(importing)
        edits = importing[0]["edit"]["changes"][uri]
        self.assertEqual(edits, [{
            "range": {"start": {"line": 0, "character": 0},
                      "end": {"line": 0, "character": 0}},
            "newText": '#load "types.glo";\n',
        }])

    def test_file_watcher_registration_response_is_accepted(self):
        self.write("main.glo", "value :: 1;\n")
        self.initialize(watch_registration=True)
        registration = self.client.wait_for(lambda message:
            message.get("method") == "client/registerCapability")
        item = registration["params"]["registrations"][0]
        self.assertEqual(item["method"], "workspace/didChangeWatchedFiles")
        self.assertEqual(item["registerOptions"]["watchers"][0]["globPattern"], "**/*.glo")
        self.client.send({"jsonrpc": "2.0", "id": registration["id"], "result": None})
        result = self.client.request("workspace/symbol", {"query": "value"})
        self.assertTrue(result)
        self.assertFalse(any(message.get("id") == registration["id"]
                             for message in self.client.pending))

    def test_formatting_honors_tab_indentation(self):
        uri = self.write("main.glo", "main :: () {\nvalue := 1;\n}\n")
        self.initialize()
        self.open(uri)
        edits = self.client.request("textDocument/formatting", {
            "textDocument": {"uri": uri}, "options": {"tabSize": 4, "insertSpaces": False},
        })
        self.assertTrue(edits)
        self.assertIn("\n\tvalue", edits[0]["newText"])
        self.assertNotIn("\n    value", edits[0]["newText"])

    def test_watched_disk_changes_and_open_overlay_precedence(self):
        dependency = self.write("dependency.glo", "#public Data :: struct { disk: ssize; }\n")
        caller = self.write("main.glo", '#load "dependency.glo";\nmain :: () { d: Data; d.; }\n')
        self.initialize()
        self.open(caller)
        self.assertIn("disk", self.completion_names(caller, "d."))
        overlay = "#public Data :: struct { overlay: ssize; }\n"
        self.open(dependency, overlay)
        self.assertIn("overlay", self.completion_names(caller, "d."))
        self.write("dependency.glo", "#public Data :: struct { changed: ssize; }\n")
        self.client.notify("workspace/didChangeWatchedFiles", {
            "changes": [{"uri": dependency, "type": 2}],
        })
        self.assertIn("overlay", self.completion_names(caller, "d."))
        self.client.notify("textDocument/didClose", {"textDocument": {"uri": dependency}})
        labels = self.completion_names(caller, "d.")
        self.assertIn("changed", labels)
        self.assertNotIn("overlay", labels)

    def test_missing_dependency_can_be_supplied_as_an_editor_buffer(self):
        caller = self.write("main.glo", '#load "missing.glo";\nitem: Missing;\n')
        missing = (self.root / "missing.glo").as_uri()
        self.initialize()
        self.open(caller)
        self.assertEqual(self.at("textDocument/definition", caller, "Missing"), [])
        self.open(missing, "#public Missing :: struct {}\n")
        self.assertEqual(self.at("textDocument/definition", caller, "Missing")[0]["uri"], missing)
        self.client.notify("textDocument/didClose", {"textDocument": {"uri": missing}})
        self.assertEqual(self.at("textDocument/definition", caller, "Missing"), [])

    def test_signature_help_and_inferred_type_hints(self):
        uri = self.write("main.glo", (
            "Point :: struct { x: ssize; }\n"
            "make :: (x: ssize, y: ssize) -> Point { result: Point; return result; }\n"
            "main :: () { inferred := make(1, 2); }\n"
        ))
        self.initialize()
        self.open(uri)
        signature = self.at("textDocument/signatureHelp", uri, "make(1, 2", offset=9)
        self.assertEqual(signature["activeParameter"], 1)
        self.assertEqual(len(signature["signatures"][0]["parameters"]), 2)
        self.assertIn("make", signature["signatures"][0]["label"])
        hints = self.client.request("textDocument/inlayHint", {
            "textDocument": {"uri": uri},
            "range": {"start": {"line": 0, "character": 0},
                      "end": {"line": self.documents[uri].count("\n"), "character": 0}},
        })
        self.assertIn(": Point", {hint["label"] for hint in hints})

    def test_signature_help_skips_the_bound_receiver_and_preserves_module_arguments(self):
        self.write("numbers.glo", (
            "#public sum :: (left: ssize, right: ssize) -> ssize { return left + right; }\n"
        ))
        uri = self.write("main.glo", (
            'Numbers :: #load "numbers.glo";\n'
            "Point :: struct { value: ssize; }\n"
            "Point ::: #public {\n"
            "    add :: (#self, x: ssize, y: ssize) -> ssize { return x + y; }\n"
            "}\n"
            "main :: () { point: Point; point.add(1, 2); Numbers.sum(1, 2); }\n"
        ))
        self.initialize()
        self.open(uri)
        for call, expected in [
            ("point.add(1", "x"), ("point.add(1, 2", "y"),
            ("Numbers.sum(1", "left"), ("Numbers.sum(1, 2", "right"),
        ]:
            with self.subTest(call=call):
                signature = self.at("textDocument/signatureHelp", uri, call, offset=len(call))
                parameters = signature["signatures"][0]["parameters"]
                label = parameters[signature["activeParameter"]]["label"]
                self.assertEqual(label.split(":", 1)[0].strip(), expected)

    def test_unopened_unicode_file_workspace_symbols_and_encoded_definition_uri(self):
        dependency = self.write("한 글#%.glo", "#public Unicode_Type :: struct {}\n")
        caller = self.write("main.glo", '#load "한 글#%.glo";\nvalue: Unicode_Type;\n')
        self.initialize()
        symbols = self.client.request("workspace/symbol", {"query": "Unicode_Type"})
        self.assertEqual(symbols[0]["location"]["uri"], dependency)
        self.open(caller)
        definition = self.at("textDocument/definition", caller, "Unicode_Type")
        self.assertEqual(definition[0]["uri"], dependency)

    def test_source_compile_time_blocks_are_never_executed(self):
        marker = self.root / "executed.txt"
        self.write("danger.glo", (
            '#import "File_System/File";\n'
            f'#comptime {{ _ := write_file("{marker}", "executed"); }}\n'
            "#public Unexecuted :: 1;\n"
        ))
        uri = self.write("main.glo", '#load "danger.glo";\nvalue :: Unexecuted;\n')
        self.initialize()
        self.open(uri)
        self.at("textDocument/hover", uri, "Unexecuted")
        self.assertFalse(marker.exists())

    def test_added_and_removed_workspace_folders_preserve_open_buffers(self):
        self.write("one/one.glo", "One_Workspace :: 1;\n")
        second = self.write("two/two.glo", "Two_Workspace :: 2;\n")
        self.initialize(root_uri=(self.root / "one").as_uri())
        self.assertEqual(self.client.request("workspace/symbol", {"query": "Two_Workspace"}), [])
        folder = {"uri": (self.root / "two").as_uri(), "name": "second"}
        self.client.notify("workspace/didChangeWorkspaceFolders", {
            "event": {"added": [folder], "removed": []},
        })
        symbols = self.client.request("workspace/symbol", {"query": "Two_Workspace"})
        self.assertEqual(symbols[0]["location"]["uri"], second)
        self.client.notify("workspace/didChangeWorkspaceFolders", {
            "event": {"added": [], "removed": [folder]},
        })
        self.assertEqual(self.client.request("workspace/symbol", {"query": "Two_Workspace"}), [])
        self.open(second)
        symbols = self.client.request("workspace/symbol", {"query": "Two_Workspace"})
        self.assertEqual(symbols[0]["location"]["uri"], second)

    def test_parser_recovers_to_later_declarations(self):
        uri = self.write("main.glo", (
            "broken :: (x: ssize { return x; }\nLater :: struct { field: ssize; }\n"
        ))
        self.initialize()
        self.open(uri)
        diagnostics = self.client.request("textDocument/diagnostic", {
            "textDocument": {"uri": uri},
        })
        self.assertTrue(diagnostics["items"])
        symbols = self.client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        self.assertIn("Later", names(symbols))
        self.assertIsNone(self.client.process.poll())

    def test_selection_range_formatting_and_spelling_quick_fix(self):
        uri = self.write("main.glo", "value :: 1;\nvalue :: 2;   \nmain :: () { vlaue; }\n")
        self.initialize()
        self.open(uri)
        selection = self.client.request("textDocument/selectionRange", {
            "textDocument": {"uri": uri}, "positions": [position(self.documents[uri], "value")],
        })
        self.assertEqual(selection[0]["range"], source_range(self.documents[uri], "value"))
        self.assertEqual(selection[0]["parent"]["range"],
                         source_range(self.documents[uri], "value :: 1;"))
        formatting = self.client.request("textDocument/formatting", {
            "textDocument": {"uri": uri}, "options": {"tabSize": 4, "insertSpaces": True},
        })
        self.assertTrue(formatting)
        self.assertNotIn("   \n", formatting[0]["newText"])
        actions = self.client.request("textDocument/codeAction", {
            "textDocument": {"uri": uri}, "range": source_range(self.documents[uri], "vlaue"),
            "context": {"diagnostics": []},
        })
        self.assertTrue(actions)
        self.assertTrue(any("edit" in action for action in actions))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path, nargs="?")
    args, test_arguments = parser.parse_known_args()
    if args.executable:
        integration.SERVER = args.executable.resolve()
    unittest.main(argv=[__file__, *test_arguments], verbosity=2)
