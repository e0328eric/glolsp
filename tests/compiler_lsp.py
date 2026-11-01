#!/usr/bin/env python3
"""Real stdio requests exercising libglosso, including compile-time evaluation."""
import argparse
import json
from urllib.parse import unquote, urlparse
import tempfile
import unittest
from pathlib import Path

import integration
from integration import LspClient, position, source_range


class CompilerLsp(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="glolsp-compiler-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.client = LspClient()
        self.addCleanup(self.client.stop)
        self.client.request("initialize", {
            "processId": None, "rootUri": self.root.as_uri(),
            "capabilities": {},
            "initializationOptions": {"scanWorkspace": False, "comptimeStepLimit": 20000},
        })
        self.client.notify("initialized", {})

    def open(self, name, text):
        uri = (self.root / name).as_uri()
        self.client.open(uri, text)
        return uri, self.client.diagnostics(uri, 1)

    def pull(self, uri):
        return self.client.request("textDocument/diagnostic", {"textDocument": {"uri": uri}})["items"]

    def test_module_parameters_defaults_instances_and_unsaved_change(self):
        module = '#module_params(N: s64 = 7); #public value::()->s64 { return N; }'
        (self.root / "configured.glo").write_text(module)
        source = ('A::#import "./configured.glo"(N=42); '
                  'B::#import "./configured.glo"; '
                  '#comptime { #assert A.value()==42; #assert B.value()==7; }')
        uri, errors = self.open("main.glo", source)
        self.assertEqual(errors, [])
        self.assertEqual(self.pull(uri), [])
        self.client.change(uri, [{"text": source.replace("N=42", "N=9")}])
        errors = self.client.diagnostics(uri, 2)
        self.assertTrue(any("assert" in d["message"] for d in errors), errors)
        self.client.change(uri, [{"text": source}], version=3)
        self.assertEqual(self.client.diagnostics(uri, 3), [])

    def test_utf16_incremental_error_and_recovery(self):
        source = 'answer::()->s32 { /* 😀 */ return missing; }\r\n'
        uri, errors = self.open("utf16.glo", source)
        error = next(d for d in errors if "missing" in d["message"])
        self.assertEqual(error["source"], "libglosso")
        self.assertEqual(error["range"], source_range(source, "missing"))
        self.client.change(uri, [{"range": source_range(source, "missing"), "text": "42"}])
        self.assertEqual(self.client.diagnostics(uri, 2), [])
        self.assertEqual(self.pull(uri), [])

    def test_incomplete_directives_keep_server_alive(self):
        uri, errors = self.open("incomplete.glo", "#import,")
        self.assertTrue(errors)
        for version, source in enumerate(("#import,only (", "#load,", "#export,", "#embed,"), 2):
            with self.subTest(source=source):
                self.client.change(uri, [{"text": source}], version)
                self.assertTrue(self.client.diagnostics(uri, version))
                self.assertTrue(self.pull(uri))
        self.client.change(uri, [{"text": "main::(){}"}], 6)
        self.assertEqual(self.client.diagnostics(uri, 6), [])
        self.assertEqual(self.client.errors, [])

    def test_comptime_failure_and_generated_declaration(self):
        uri, errors = self.open("comptime.glo", '#comptime { #assert false, "compiler evaluated this"; }')
        self.assertTrue(any("compiler evaluated this" in d["message"] for d in errors))
        self.client.change(uri, [{"text": '#insert "#public generated::()->s32{return 42;}"; answer::()->s32{return generated();}'}])
        self.assertEqual(self.client.diagnostics(uri, 2), [])

    def test_comptime_output_and_budget_preserve_protocol(self):
        _, errors = self.open("output.glo", '#import "Io"; #comptime { print("not an LSP frame\\n"); }')
        self.assertEqual(errors, [])
        uri, errors = self.open("budget.glo", 'spin::()->s32 { while true {} return 0; } value::#comptime spin();')
        self.assertTrue(any("limit" in d["message"].lower() for d in errors))
        self.assertIsInstance(self.client.request("workspace/symbol", {"query": "spin"}), list)
        self.client.change(uri, [{"text": "value::42;"}])
        self.assertEqual(self.client.diagnostics(uri, 2), [])
        self.assertEqual(self.client.errors, [])

    def test_comptime_exit_reports_output_location_and_recovers(self):
        source = ('#import "Io"; #import "File_System/File";\n'
                  '#comptime { print("earlier block\\n"); }\n'
                  '#comptime { print("native build failed: missing SDK 😀\\n"); '
                  'error := File.stderr(); _ := error.write("compiler details\\n"); exit(7); }\n')
        uri, errors = self.open("exit.glo", source)
        self.assertEqual(len(errors), 1, errors)
        error = errors[0]
        self.assertEqual(error["range"]["start"], position(source, "#comptime", 1))
        self.assertIn("exited with status 7", error["message"])
        self.assertIn("Compile-time stdout:\nearlier block\nnative build failed: missing SDK 😀\n", error["message"])
        self.assertIn("Compile-time stderr:\ncompiler details\n", error["message"])
        self.assertEqual(self.pull(uri), errors)
        self.client.change(uri, [{"text": "answer::42;"}])
        self.assertEqual(self.client.diagnostics(uri, 2), [])
        self.client.change(uri, [{"text": "answer::()->s32{return absent;}"}], 3)
        errors = self.client.diagnostics(uri, 3)
        self.assertTrue(errors)
        self.assertNotIn("missing SDK", str(errors))
        self.assertEqual(self.client.errors, [])

    def test_imported_comptime_exit_retains_related_location(self):
        module = '// dependency\n#import "Io";\n#comptime { print("dependency failed\\n"); exit(3); }'
        dependency = self.root / "dependency.glo"
        dependency.write_text(module)
        uri, errors = self.open("imported-exit.glo", '#import "./dependency.glo";')
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("dependency failed", errors[0]["message"])
        related = errors[0]["relatedInformation"][0]["location"]
        self.assertEqual(related["uri"], dependency.as_uri())
        self.assertEqual(related["range"]["start"], position(module, "#comptime"))
        self.assertEqual(self.pull(uri), errors)

    def test_comptime_output_is_bounded_and_valid_utf8(self):
        payload = "x" * 16382 + "😀" + "hidden tail"
        source = ('#import "Io"; #import "File_System/File";\n'
                  '#comptime { out := File.stdout(); _ := out.write(' + json.dumps(payload, ensure_ascii=False) + '); '
                  'error := File.stderr(); _ := error.write_byte(255); exit(1); }')
        uri, errors = self.open("binary-output.glo", source)
        self.assertEqual(len(errors), 1, errors)
        message = errors[0]["message"]
        self.assertIn("exited with status 1", message)
        self.assertIn("x" * 16382 + "\n[output truncated]", message)
        self.assertNotIn("hidden tail", message)
        self.assertIn("Compile-time stderr:\n�", message)
        self.assertEqual(self.pull(uri), errors)
        self.assertEqual(self.client.errors, [])

    def test_unsaved_import_changes_and_close_restore_disk(self):
        module = self.root / "new/Module/module.glo"
        module.parent.mkdir(parents=True)
        module.write_text('#public number::()->s32{return 42;}')
        module_uri, errors = self.open("new/Module/module.glo", '#public number::()->s32{return 7;}')
        self.assertEqual(errors, [])
        uri, errors = self.open("main.glo", '#import "./new/Module"; answer::()->s32{return number();}')
        self.assertEqual(errors, [])
        self.client.change(module_uri, [{"text": '#public number::()->s32{return absent;}'}])
        self.client.diagnostics(module_uri, 2)
        errors = self.pull(uri)
        self.assertTrue(any("absent" in d["message"] for d in errors))
        self.assertTrue(any(r["location"]["uri"] == module_uri for d in errors for r in d["relatedInformation"]))
        self.client.notify("textDocument/didClose", {"textDocument": {"uri": module_uri}})
        self.assertEqual(self.client.diagnostics(module_uri), [])
        self.assertEqual(self.pull(uri), [])

    def test_navigation_formatting_and_incomplete_buffers(self):
        source = '#public square::(x:s32)->s32{return x*x;}\nanswer::()->s32{return square(6)+6;}\n'
        uri, errors = self.open("editor.glo", source)
        self.assertEqual(errors, [])
        definition = self.client.request("textDocument/definition", {
            "textDocument": {"uri": uri}, "position": position(source, "square", 1),
        })
        if isinstance(definition, list):
            definition = definition[0]
        self.assertEqual(definition["range"]["start"], position(source, "square"))
        edits = self.client.request("textDocument/formatting", {
            "textDocument": {"uri": uri}, "options": {"tabSize": 4, "insertSpaces": True},
        })
        self.assertTrue(edits)
        self.assertIn("return x * x;", edits[0]["newText"])
        self.client.change(uri, [{"text": 'answer::()->s32 { /* unfinished'}])
        self.assertTrue(self.client.diagnostics(uri, 2))
        completion = self.client.request("textDocument/completion", {
            "textDocument": {"uri": uri}, "position": {"line": 0, "character": 31},
        })
        self.assertEqual(completion.get("items", []) if isinstance(completion, dict) else completion, [])

    def at(self, method, uri, source, needle, occurrence=0, offset=0, **extra):
        at = position(source, needle, occurrence)
        at["character"] += offset
        return self.client.request("textDocument/" + method, {
            "textDocument": {"uri": uri}, "position": at, **extra,
        })

    def labels(self, uri, source, needle, occurrence=0, offset=0):
        result = self.at("completion", uri, source, needle, occurrence, offset)
        return {item["label"] for item in result["items"]}

    def assert_generated_location(self, location, owner, generated, name):
        if isinstance(location, list):
            location = location[0]
        self.assertTrue(location)
        if location["uri"] == owner:
            # A cache failure falls back to the insertion in an existing document.
            self.assertEqual(location["range"]["start"], {"line": 0, "character": 0})
        else:
            self.assertEqual(Path(unquote(urlparse(location["uri"]).path)).read_text(), generated)
            self.assertEqual(location["range"], source_range(generated, name))

    def test_generated_function_type_navigation_and_rename(self):
        generated = '#public generated::(x:u8)->u8{return x;} #public Box::struct{value:u8;}'
        source = ('#insert ' + json.dumps(generated) + ';\n'
                  'answer::()->u8 { box:Box; n:=generated(1); return generated(n)+box.value; }')
        uri, errors = self.open("generated.glo", source)
        self.assertEqual(errors, [])
        self.assertIn("generated", self.labels(uri, source, "generated(1)", offset=4))
        self.assertEqual(self.labels(uri, source, "value;", offset=1), {"value"})
        hover = self.at("hover", uri, source, "n:=")
        self.assertIn("u8", str(hover))
        self.assert_generated_location(self.at("definition", uri, source, "generated(1)"),
                                       uri, generated, "generated")
        self.assert_generated_location(self.at("typeDefinition", uri, source, "box.value"),
                                       uri, generated, "Box")
        signature = self.at("signatureHelp", uri, source, "generated(1)", offset=len("generated("))
        self.assertIn("x:u8", signature["signatures"][0]["label"])
        references = self.at("references", uri, source, "generated(1)", context={"includeDeclaration": False})
        self.assertEqual(len(references), 2)
        self.assertTrue(all(r["uri"] == uri for r in references))
        self.assertIsNone(self.at("prepareRename", uri, source, "generated(1)"))
        error = self.client.request_message("textDocument/rename", {
            "textDocument": {"uri": uri}, "position": position(source, "generated(1)"), "newName": "changed",
        })
        self.assertEqual(error["error"]["code"], -32602)
        self.assertIn("#insert", error["error"]["message"])

    def test_generated_partial_snapshot_and_invalidation(self):
        source = '#insert "#public old_name::()->s32{return 1;}";\nanswer::()->s32{return absent;}'
        uri, errors = self.open("partial.glo", source)
        self.assertTrue(errors)
        self.assertIn("old_name", self.labels(uri, source, "absent"))
        updated = source.replace("old_name", "new_name")
        self.client.change(uri, [{"text": updated}])
        self.assertTrue(self.client.diagnostics(uri, 2))
        names = self.labels(uri, updated, "absent")
        self.assertIn("new_name", names)
        self.assertNotIn("old_name", names)
        removed = 'answer::()->s32{return absent;}'
        self.client.change(uri, [{"text": removed}], 3)
        self.assertTrue(self.client.diagnostics(uri, 3))
        names = self.labels(uri, removed, "absent")
        self.assertNotIn("new_name", names)
        self.assertNotIn("old_name", names)

    def test_generated_import_visibility_aliases_and_dependency_change(self):
        module_source = '#insert "#public generated::()->s32{return 1;} secret::()->s32{return 2;}";\nlocal::()->s32{return secret();}'
        module_uri, errors = self.open("module.glo", module_source)
        self.assertEqual(errors, [])
        self.assertIn("secret", self.labels(module_uri, module_source, "secret();", offset=3))
        source = '#import "./module.glo"; M::#import "./module.glo";\nanswer::()->s32{return generated()+M.generated();}'
        uri, errors = self.open("imported.glo", source)
        self.assertEqual(errors, [])
        names = self.labels(uri, source, "generated()+")
        self.assertIn("generated", names)
        self.assertNotIn("secret", names)
        self.assertIn("generated", self.labels(uri, source, "generated();", offset=2))
        self.assertNotIn("secret", self.labels(uri, source, "generated();"))
        updated = module_source.replace("generated", "replacement")
        self.client.change(module_uri, [{"text": updated}])
        self.client.diagnostics(module_uri, 2)
        names = self.labels(uri, source, "generated()+")
        self.assertIn("replacement", names)
        self.assertNotIn("generated", names)

    def test_compiler_inferred_type_and_utf16_hints(self):
        source = 'take::(x:u8){}\r\nmain::(){/*😀*/ n:=1; take(n);}'
        uri, errors = self.open("inference.glo", source)
        self.assertEqual(errors, [])
        for needle in ("n:=", "n);"):
            self.assertIn("n: u8", str(self.at("hover", uri, source, needle)))
        hints = self.client.request("textDocument/inlayHint", {
            "textDocument": {"uri": uri},
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 1, "character": 31}},
        })
        hint = next(h for h in hints if h["label"] == ": u8")
        expected = position(source, "n:=")
        expected["character"] += 1
        self.assertEqual(hint["position"], expected)

    def test_generated_globals_and_inherited_module_visibility(self):
        generated = 'constant::42; global:s32=7;'
        module_source = '#public { #insert ' + json.dumps(generated) + '; }'
        module_uri, errors = self.open("globals.glo", module_source)
        self.assertEqual(errors, [])
        source = 'M::#import "./globals.glo"; answer::()->s32{return M.global+M.constant;}'
        uri, errors = self.open("read.glo", source)
        self.assertEqual(errors, [])
        names = self.labels(uri, source, "global+")
        self.assertIn("constant", names)
        self.assertIn("global", names)
        location = self.at("definition", uri, source, "global+")
        if location["uri"] == module_uri:
            self.assertEqual(location["range"], source_range(module_source, "#insert"))
        else:
            self.assert_generated_location(location, module_uri, generated, "global")

    def test_bindgen_generated_structure_is_indexed(self):
        header = self.root / "fixture.h"
        header.write_text("typedef struct { int field; } NativeRecord;\n")
        source = (f'''#import "Bindgen"; #import "Io";
bindings::#comptime -> str {{
    if generate_source("{header}", "c") == {{
        case .Ok(text); return text;
        case .Err(failure); print("{{}}", failure.message); exit(1);
    }}
    return "";
}}
#insert bindings;
answer::()->s32 {{ record:NativeRecord; return record.field; }}''')
        uri, errors = self.open("bindgen.glo", source)
        self.assertEqual(errors, [])
        self.assertIn("NativeRecord", self.labels(uri, source, "NativeRecord;", offset=5))
        self.assertEqual(self.labels(uri, source, "field;", offset=1), {"field"})
        self.assertIsNotNone(self.at("typeDefinition", uri, source, "record.field"))

    def test_queries_reuse_comptime_snapshot(self):
        marker = self.root / "executed.txt"
        source = ('#import "File_System/File";\n'
                  f'#comptime {{ _ := write_file("{marker}", "evaluated"); }}\n'
                  'answer::()->s32{return 42;}')
        uri, errors = self.open("cached.glo", source)
        self.assertEqual(errors, [])
        self.assertEqual(marker.read_text(), "evaluated")
        marker.write_text("snapshot reused")
        for _ in range(3):
            self.assertEqual(self.pull(uri), [])
            self.at("hover", uri, source, "answer")
            self.labels(uri, source, "answer")
        self.assertEqual(marker.read_text(), "snapshot reused")
        updated = source.replace("return 42", "return 7")
        self.client.change(uri, [{"text": updated}])
        self.assertEqual(self.client.diagnostics(uri, 2), [])
        self.assertEqual(marker.read_text(), "evaluated")
        marker.write_text("external change")
        self.client.notify("workspace/didChangeWatchedFiles", {
            "changes": [{"uri": marker.as_uri(), "type": 2}],
        })
        self.assertEqual(self.client.diagnostics(uri, 2), [])
        self.assertEqual(marker.read_text(), "evaluated")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path, default=integration.SERVER)
    args, rest = parser.parse_known_args()
    integration.SERVER = args.server.resolve()
    unittest.main(argv=[__file__, *rest], verbosity=2)
