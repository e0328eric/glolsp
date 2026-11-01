#!/usr/bin/env python3
"""User TOML configuration, std discovery and override precedence over stdio."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

import integration
from integration import LspClient


@unittest.skipUnless(sys.platform.startswith(("linux", "freebsd")), "XDG configuration paths")
class ConfigurationLsp(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="glolsp-config-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "user config/glolsp/config.toml"
        self.config.parent.mkdir(parents=True)
        self.environment = dict(os.environ, XDG_CONFIG_HOME=str(self.config.parent.parent))
        self.environment.pop("GLOSSO_STD", None)
        self.default_std = Path.home() / ".local/glosso/std"
        self.project = self.root / "project"
        self.project.mkdir()

    def make_installation(self, name="Glosso SDK", development=False):
        prefix = self.root / name
        standard = prefix / "std"
        shutil.copytree(self.default_std, standard)
        (standard / "Configured.glo").write_text("#public Marker::struct{}\n")
        compiler = prefix / ("build/bin/glosso" if development else "bin/glosso")
        compiler.parent.mkdir(parents=True)
        # Only its location is needed: the LSP uses the frontend C ABI.
        compiler.write_text("this compiler executable must not be invoked\n")
        return compiler, standard

    def start(self, **options):
        client = LspClient(environment=self.environment)
        self.addCleanup(client.stop)
        client.request("initialize", {
            "processId": None, "rootUri": self.project.as_uri(), "capabilities": {},
            "initializationOptions": options,
        })
        client.notify("initialized", {})
        return client

    def assert_standard(self, client, standard, module="Configured"):
        uri = (self.project / "main.glo").as_uri()
        source = f'#import "{module}"; main::(){{}}'
        client.open(uri, source)
        self.assertEqual(client.diagnostics(uri, 1), [])
        links = client.request("textDocument/documentLink", {"textDocument": {"uri": uri}})
        self.assertEqual(links[0]["target"], (standard / f"{module}.glo").resolve().as_uri())
        client.change(uri, [{"text": f'#import "{module[:3]}'}], 2)
        self.assertTrue(client.diagnostics(uri, 2))
        result = client.request("textDocument/completion", {
            "textDocument": {"uri": uri}, "position": {"line": 0, "character": 12},
        })
        self.assertIn(module, {item["label"] for item in result["items"]})

    def test_compiler_path_with_spaces_uses_its_std_for_analysis_and_completion(self):
        compiler, standard = self.make_installation()
        self.config.write_text(f"# installed compiler\ncompiler_path = {json.dumps(str(compiler))}\n")
        client = self.start()
        self.assert_standard(client, standard)
        self.assertEqual(client.errors, [])

    @unittest.skipUnless(sys.platform == "linux", "Linux SDK fixture")
    def test_selected_compiler_sdk_supplies_native_headers_and_libraries(self):
        compiler, standard = self.make_installation()
        self.config.write_text(f"compiler_path = {json.dumps(str(compiler))}\n")
        cpu = {"x86_64": "x86_64", "aarch64": "aarch64"}.get(os.uname().machine)
        if cpu is None:
            self.skipTest("unsupported fixture target")
        target = standard.parent / f"lib/{cpu}-linux-gnu"
        include = target / "include"
        include.mkdir(parents=True)
        header = include / "glolsp_sdk_fixture.h"
        header.write_text("#define SDK_VALUE 42\n")
        marker = target / "lib/libglolsp_sdk_fixture.a"
        marker.parent.mkdir()
        marker.write_bytes(b"!<arch>\n")
        native = self.project / "fixture.c"
        native.write_text('#include <glolsp_sdk_fixture.h>\nint fixture(void) { return SDK_VALUE; }\n')
        self.environment["GLOSSO_NATIVE_CACHE_DIR"] = str(self.root / "native cache")
        client = self.start()
        uri = (self.project / "main.glo").as_uri()
        source = f'''#import "Compiler"; #import "Io";
#comptime {{
    #assert find_library("libglolsp_sdk_fixture.a") == {json.dumps(str(marker))};
    result := Native_Compile_Output.compile_c_archive("glolsp-sdk-fixture", .[{json.dumps(str(native))}],
        .compiler_args = .["-nostdinc"]);
    if result == {{ case .Ok(archive); case .Err(failure); print("{{}}", failure.message); exit(1); }}
}}
'''
        client.open(uri, source)
        self.assertEqual(client.diagnostics(uri, 1), [])
        header.unlink()
        client.change(uri, [{"text": source + "\n"}])
        errors = client.diagnostics(uri, 2)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("glolsp_sdk_fixture.h", errors[0]["message"])
        self.assertIn("Compile-time stdout:", errors[0]["message"])
        self.assertEqual(errors[0]["range"]["start"]["line"], 1)
        # An unsaved edit must still report real errors after the native build.
        header.write_text("#define SDK_VALUE 42\n")
        client.change(uri, [{"text": source + '\nanswer::()->s32{return absent;}'}], 3)
        errors = client.diagnostics(uri, 3)
        self.assertTrue(any("absent" in error["message"] for error in errors), errors)
        self.assertEqual(client.errors, [])

    def test_relative_path_is_based_on_config_directory(self):
        compiler, standard = self.make_installation()
        relative = os.path.relpath(compiler, self.config.parent)
        self.config.write_text(f"compiler_path = '{relative}' # literal TOML string\n")
        self.assert_standard(self.start(), standard)

    def test_development_compiler_and_symlinks(self):
        compiler, standard = self.make_installation(development=True)
        link = self.root / "linked compiler"
        link.symlink_to(compiler)
        self.config.write_text(f"compiler_path = {json.dumps(str(link))}\n")
        self.assert_standard(self.start(), standard)

    def test_environment_then_initialization_override_config(self):
        compiler, _ = self.make_installation("configured")
        self.config.write_text(f"compiler_path = {json.dumps(str(compiler))}\n")
        _, environment_std = self.make_installation("environment")
        self.environment["GLOSSO_STD"] = str(environment_std)
        self.assert_standard(self.start(), environment_std)
        _, initialized_std = self.make_installation("initialized")
        self.assert_standard(self.start(stdPath=str(initialized_std)), initialized_std)

    def test_missing_config_and_home_relative_compiler_use_default_installation(self):
        self.assert_standard(self.start(), self.default_std, "String_Builder")
        self.config.write_text('compiler_path = "~/.local/glosso/bin/glosso"\n')
        self.assert_standard(self.start(), self.default_std, "String_Builder")

    def test_invalid_toml_and_wrong_type_report_to_stderr_and_preserve_lsp(self):
        for source in ('compiler_path = "unterminated\n', 'compiler_path = 42\n', 'compiler_path = ""\n'):
            with self.subTest(source=source):
                self.config.write_text(source)
                client = self.start()
                self.assert_standard(client, self.default_std, "String_Builder")
                client.stop()
                self.assertIn(str(self.config), "".join(client.errors))
                self.assertIn("glolsp:", "".join(client.errors))

    def test_nonexistent_installation_reports_error_without_silent_fallback(self):
        compiler = self.root / "missing/bin/glosso"
        self.config.write_text(f"compiler_path = {json.dumps(str(compiler))}\n")
        client = self.start()
        uri = (self.project / "main.glo").as_uri()
        client.open(uri, '#import "String_Builder"; main::(){}')
        self.assertTrue(client.diagnostics(uri, 1))
        self.assertIn("compiler_path does not name a file", "".join(client.errors))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path, default=integration.SERVER)
    args, rest = parser.parse_known_args()
    integration.SERVER = args.server.resolve()
    unittest.main(argv=[__file__, *rest], verbosity=2)
