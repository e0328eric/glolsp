#!/usr/bin/env python3
"""Regression tests for workspace ownership, file identity and compiler snapshots."""
import argparse
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import integration
from integration import LspClient, position, source_range


def labels(result):
    items = result.get("items", []) if isinstance(result, dict) else result
    return {item["label"] for item in items}


class WorkspaceRegressions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="glolsp-regress-")
        self.addCleanup(temporary.cleanup)
        # Resolve the temporary root itself; symlink tests add their own links.
        self.root = Path(temporary.name).resolve()

    def start(self, root=None, options=None, environment=None):
        client = LspClient(environment)
        self.addCleanup(client.stop)
        initialization = {"scanWorkspace": False}
        initialization.update(options or {})
        client.request("initialize", {
            "processId": None, "rootUri": (root or self.root).as_uri(), "capabilities": {},
            "initializationOptions": initialization,
        })
        client.notify("initialized", {})
        return client

    def at(self, client, method, uri, source, needle, offset=0, **extra):
        where = position(source, needle)
        where["character"] += offset
        return client.request("textDocument/" + method, {
            "textDocument": {"uri": uri}, "position": where, **extra,
        })

    def ping(self, client, timeout=integration.TIMEOUT):
        """Time an unknown request; only the main loop answers it."""
        started = time.monotonic()
        request = client.next_id
        client.next_id += 1
        client.send({"jsonrpc": "2.0", "id": request, "method": "glolsp/ping", "params": {}})
        client.wait_for(lambda message: message.get("id") == request, timeout)
        return time.monotonic() - started

    def diagnostics(self, client, uri, version, timeout=60):
        return client.wait_for(lambda message:
            message.get("method") == "textDocument/publishDiagnostics" and
            message["params"]["uri"] == uri and message["params"].get("version") == version,
            timeout)["params"]["diagnostics"]

    # L1
    @unittest.skipUnless(Path("/proc/self/fd").is_dir(), "needs /proc")
    def test_import_completion_closes_directories(self):
        client = self.start()
        uri = (self.root / "main.glo").as_uri()
        source = '#import "\nanswer::()->s32{return 1;}\n'
        client.open(uri, source)
        client.diagnostics(uri, 1)
        descriptors = Path(f"/proc/{client.process.pid}/fd")
        before = len(list(descriptors.iterdir()))
        for _ in range(40):
            client.request("textDocument/completion", {
                "textDocument": {"uri": uri}, "position": {"line": 0, "character": 9},
            })
        self.assertEqual(len(list(descriptors.iterdir())), before)

    # L3
    def test_max_files_never_blocks_editor_buffers(self):
        for index in range(30):
            (self.root / f"scanned_{index}.glo").write_text(f"#public scanned_{index}::{index};\n")
        client = self.start(options={"scanWorkspace": True, "maxFiles": 5})
        uri = (self.root / "unsaved.glo").as_uri()
        client.open(uri, "answer::()->s32{return missing;}")
        self.assertTrue(any("missing" in d["message"] for d in client.diagnostics(uri, 1)))
        symbols = client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        self.assertEqual([symbol["name"] for symbol in symbols], ["answer"])
        scanned = client.request("workspace/symbol", {"query": "scanned_"})
        self.assertLessEqual(len(scanned), 5)

    # L3
    def test_generated_sources_do_not_exhaust_max_files(self):
        client = self.start(options={"maxFiles": 2})
        uri = (self.root / "insert.glo").as_uri()

        def source(index):
            generated = f"#public generated_{index}::()->s32{{return {index};}}"
            return f"#insert {json.dumps(generated)};\nanswer::()->s32{{return generated_{index}();}}"

        client.open(uri, source(0))
        self.assertEqual(client.diagnostics(uri, 1), [])
        for version in range(2, 10):
            client.change(uri, [{"text": source(version)}], version)
            self.assertEqual(client.diagnostics(uri, version), [])
        text = source(9)
        self.assertIn("generated_9", labels(self.at(client, "completion", uri, text, "generated_9();", 3)))
        other = (self.root / "other.glo").as_uri()
        client.open(other, "other::2;")
        symbols = client.request("textDocument/documentSymbol", {"textDocument": {"uri": other}})
        self.assertEqual([symbol["name"] for symbol in symbols], ["other"])

    # F3
    def test_export_of_module_name_is_indexed(self):
        modules = self.root / "modules"
        modules.mkdir()
        (modules / "Foo.glo").write_text("#public foo_function::()->s32{return 1;}\n")
        project = self.root / "project"
        project.mkdir()
        (project / "reexport.glo").write_text('#export "Foo";\n')
        client = self.start(project, {"importPaths": [str(modules)]})
        uri = (project / "main.glo").as_uri()
        source = '#import "./reexport.glo";\nx::()->s32{return foo_fu' + 'nction();}\n'
        client.open(uri, source)
        self.assertIn("foo_function", labels(self.at(client, "completion", uri, source, "foo_fu", 6)))
        links = client.request("textDocument/documentLink", {
            "textDocument": {"uri": (project / "reexport.glo").as_uri()},
        })
        self.assertEqual([link["target"] for link in links], [(modules / "Foo.glo").as_uri()])

    # F3: completion inside `#export "` offers modules, as after `#import "`.
    def test_export_completion_offers_modules(self):
        modules = self.root / "modules"
        modules.mkdir()
        (modules / "Foo.glo").write_text("#public foo_function::()->s32{return 1;}\n")
        project = self.root / "project"
        project.mkdir()
        client = self.start(project, {"importPaths": [str(modules)]})
        uri = (project / "main.glo").as_uri()
        source = '#export "Fo";\n'
        client.open(uri, source)
        result = self.at(client, "completion", uri, source, '"Fo', 3)
        items = {item["label"]: item for item in result["items"]}
        self.assertIn("Foo", items)
        self.assertEqual(items["Foo"]["detail"], "Glosso module")

    def symlinked_root(self):
        real = self.root / "real"
        real.mkdir()
        link = self.root / "link"
        link.symlink_to(real, target_is_directory=True)
        return real, link

    # F7
    def test_symlinked_directory_keeps_identity_after_save_and_delete(self):
        real, link = self.symlinked_root()
        client = self.start(link)
        uri = (link / "new.glo").as_uri()
        source = "a::()->s32{return 1;}\n"
        client.open(uri, source)
        self.assertEqual(client.diagnostics(uri, 1), [])
        (link / "new.glo").write_text(source)
        client.notify("textDocument/didSave", {"textDocument": {"uri": uri}})
        client.change(uri, [{"text": "a::()->s32{return missing;}\n"}], 2)
        self.assertTrue(any("missing" in d["message"] for d in client.diagnostics(uri, 2)))
        (real / "new.glo").unlink()
        client.notify("workspace/didChangeWatchedFiles", {"changes": [{"uri": uri, "type": 3}]})
        client.change(uri, [{"text": "a::()->s32{return 2;}\n"}], 3)
        self.assertEqual(client.diagnostics(uri, 3), [])
        hover = self.at(client, "hover", uri, "a::", "a")
        self.assertIn("a::()->s32", str(hover))

    # F7
    def test_symlinked_root_keeps_editor_spelling(self):
        real, link = self.symlinked_root()
        (real / "dep.glo").write_text("#public helper::()->s32{return 1;}\n")
        source = '#import "./dep.glo";\nx::()->s32{ return helper(); }\n'
        (real / "main.glo").write_text(source)
        client = self.start(link)
        uri = (link / "main.glo").as_uri()
        client.open(uri, source)
        self.assertEqual(client.diagnostics(uri, 1), [])
        definition = self.at(client, "definition", uri, source, "helper()")
        if isinstance(definition, list):
            definition = definition[0]
        self.assertEqual(definition["uri"], (link / "dep.glo").as_uri())

    # F7: the server must pass the closed URI to workspace_close.
    def test_closing_one_alias_keeps_the_other_open(self):
        real, link = self.symlinked_root()
        dependency = "#public helper::()->s32{return 1;}\n"
        (real / "dep.glo").write_text(dependency)
        source = '#import "./dep.glo";\nx::()->s32{ return helper(); }\n'
        (real / "main.glo").write_text(source)
        client = self.start(link)
        main = (link / "main.glo").as_uri()
        client.open(main, source)
        client.diagnostics(main, 1)
        canonical, alias = (real / "dep.glo").as_uri(), (link / "dep.glo").as_uri()
        client.open(canonical, dependency)
        client.open(alias, dependency)
        client.notify("textDocument/didClose", {"textDocument": {"uri": canonical}})
        client.change(alias, [{"text": "#public renamed::()->s32{return 1;}\n"}], 2)
        self.assertEqual(client.diagnostics(alias, 2), [])
        errors = client.request("textDocument/diagnostic", {"textDocument": {"uri": main}})["items"]
        self.assertTrue(any("helper" in d["message"] for d in errors), errors)

    def module_name_project(self):
        (self.root / "helpers.glo").write_text("#public helper::()->s32{return 1;}\n")
        sub = self.root / "sub"
        sub.mkdir()
        source = '#import "helpers";\nx::()->s32{return helper();}\n'
        (sub / "main.glo").write_text(source)
        return (sub / "main.glo").as_uri(), source

    # N1: like `glosso compile`, module names are not searched in workspace roots.
    def test_workspace_roots_are_not_module_paths_by_default(self):
        client = self.start()
        uri, source = self.module_name_project()
        client.open(uri, source)
        errors = [d["message"] for d in self.diagnostics(client, uri, 1)]
        self.assertTrue(any('cannot find module "helpers"' in message for message in errors), errors)
        self.assertEqual(client.request("textDocument/documentLink", {"textDocument": {"uri": uri}}), [])
        completion = client.request("textDocument/completion", {
            "textDocument": {"uri": uri}, "position": {"line": 0, "character": 12},
        })
        self.assertNotIn("helpers", labels(completion))

    # N1: `rootsAsModulePaths` restores the old search.
    def test_roots_as_module_paths_option(self):
        client = self.start(options={"rootsAsModulePaths": True})
        uri, source = self.module_name_project()
        client.open(uri, source)
        self.assertEqual(self.diagnostics(client, uri, 1), [])
        links = client.request("textDocument/documentLink", {"textDocument": {"uri": uri}})
        self.assertEqual([link["target"] for link in links], [(self.root / "helpers.glo").as_uri()])
        completion = client.request("textDocument/completion", {
            "textDocument": {"uri": uri}, "position": {"line": 0, "character": 12},
        })
        self.assertIn("helpers", labels(completion))

    # F10
    def test_untitled_and_short_file_uris_are_checked(self):
        client = self.start()
        source = "answer::()->s32{return missing;}\n"
        short = self.root / "short.glo"
        for uri in ("untitled:Untitled-1", "file:" + str(short)):
            with self.subTest(uri=uri):
                client.open(uri, source)
                errors = client.diagnostics(uri, 1)
                self.assertEqual([d["message"] for d in errors], ["unbound 'missing'"])
                self.assertEqual(errors[0]["range"], source_range(source, "missing"))
        client.open("git:/repository/view.glo?ref=HEAD", source)
        self.assertEqual(client.diagnostics("git:/repository/view.glo?ref=HEAD", 1), [])

    # F11
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_unreadable_directory_does_not_stop_scan(self):
        for index in range(12):
            directory = self.root / f"d{index:02}"
            directory.mkdir()
            (directory / f"f{index}.glo").write_text(f"#public scanned_fn_{index}::()->s32{{return 1;}}\n")
        locked = self.root / "d05" / "locked"
        locked.mkdir()
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o755)
        client = self.start(options={"scanWorkspace": True})
        self.assertEqual(len(client.request("workspace/symbol", {"query": "scanned_fn"})), 12)

    # F12
    def test_lone_carriage_return_diagnostic_positions(self):
        client = self.start()
        uri = (self.root / "cr.glo").as_uri()
        source = "a::()->s32{return 1;}\rb::()->s32{return missing;}\r"
        client.open(uri, source)
        errors = client.diagnostics(uri, 1)
        self.assertEqual(errors[0]["range"], {
            "start": {"line": 1, "character": 18}, "end": {"line": 1, "character": 25},
        })

    # F13
    def test_unused_generated_files_leave_workspace_symbols(self):
        environment = dict(os.environ, XDG_CACHE_HOME=str(self.root / "cache"))
        client = self.start(environment=environment)
        uri = (self.root / "main.glo").as_uri()
        generated = "#public old_generated::()->s32{return 42;}"
        client.open(uri, f"#insert {json.dumps(generated)};\nanswer::()->s32{{return old_generated();}}")
        self.assertEqual(client.diagnostics(uri, 1), [])
        symbols = client.request("workspace/symbol", {"query": "old_generated"})
        self.assertEqual(len(symbols), 1)
        self.assertTrue(symbols[0]["location"]["uri"].startswith((self.root / "cache").as_uri()))
        client.change(uri, [{"text": "answer::()->s32{return 1;}"}], 2)
        self.assertEqual(client.diagnostics(uri, 2), [])
        self.assertEqual(client.request("workspace/symbol", {"query": "old_generated"}), [])

    def wait_for_diagnostics(self, client, uri, version, timeout):
        """Ping the main loop until diagnostics arrive; return the longest stall."""
        deadline = time.monotonic() + timeout
        worst = 0.0
        while time.monotonic() < deadline:
            worst = max(worst, self.ping(client, 30))
            if any(message.get("method") == "textDocument/publishDiagnostics" and
                   message["params"]["uri"] == uri and message["params"].get("version") == version
                   for message in client.pending):
                return worst
            time.sleep(0.02)
        self.fail("diagnostics did not arrive")

    # P3: removing duplicate facts was quadratic (about 14 s here before).
    def test_many_compiler_facts_do_not_stall_the_main_loop(self):
        client = self.start()
        lines = ["f::(a:s64)->s64 {", "    x: s64 = a;"]
        lines += [f"    x = x + {index} * a - (x / 3);" for index in range(24000)]
        lines.append("    return x;\n}")
        uri = (self.root / "facts.glo").as_uri()
        client.open(uri, "\n".join(lines))
        self.assertLess(self.wait_for_diagnostics(client, uri, 1, 120), 6.0)

    # P2: every expression's compiler fact was found by scanning all facts
    # (about 9 s here before for the first request after checking).
    def test_compiler_fact_lookup_is_indexed(self):
        client = self.start()
        lines = ["f::(a:s64)->s64 {", "    x: s64 = a;"]
        lines += [f"    x = x + {index} * a - (x / 3);" for index in range(12000)]
        lines.append("    return x;\n}")
        uri = (self.root / "lookup.glo").as_uri()
        client.open(uri, "\n".join(lines))
        self.diagnostics(client, uri, 1, 120)
        started = time.monotonic()
        request = client.next_id
        client.next_id += 1
        client.send({"jsonrpc": "2.0", "id": request, "method": "textDocument/semanticTokens/full",
                     "params": {"textDocument": {"uri": uri}}})
        client.wait_for(lambda message: message.get("id") == request, 60)
        self.assertLess(time.monotonic() - started, 3.0)

    # P4: each change used to invalidate and resolve every import again.
    def test_watched_file_batch_is_resolved_once(self):
        files = []
        for index in range(80):
            path = self.root / f"module_{index}.glo"
            imports = "".join(f'#import "./module_{other}.glo";\n' for other in range(max(0, index - 3), index))
            path.write_text(f'#import "Io";\n{imports}#public value_{index}::{index};\n')
            files.append(path)
        client = self.start()
        uri = files[-1].as_uri()
        client.open(uri, files[-1].read_text())
        self.diagnostics(client, uri, 1)
        changes = [{"uri": files[index % len(files)].as_uri(), "type": 2} for index in range(3000)]
        started = time.monotonic()
        client.notify("workspace/didChangeWatchedFiles", {"changes": changes})
        self.ping(client, 60)
        self.assertLess(time.monotonic() - started, 5.0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path, default=integration.SERVER)
    args, rest = parser.parse_known_args()
    integration.SERVER = args.server.resolve()
    unittest.main(argv=[__file__, *rest], verbosity=2)
