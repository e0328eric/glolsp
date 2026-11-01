#!/usr/bin/env python3
"""Check a relocated runtime bundle using the installed Glosso std."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import integration

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", help="Also exercise install and reinstall with this compiler")
    args = parser.parse_args()
    if sys.platform != "linux":
        raise SystemExit("This relocation test currently targets Linux.")
    with tempfile.TemporaryDirectory(prefix="glolsp-standalone-") as temporary:
        relocated = Path(temporary) / "relocated bundle"
        if args.compiler:
            prefix = Path(temporary) / "install prefix"
            command = [args.compiler, "first.glo", "--cache-dir=build/cache", "--",
                       "install", "--prefix", str(prefix)]
            subprocess.run(command, cwd=ROOT, check=True)
            # Installing again must replace existing executable and read-only headers.
            legacy_std = prefix / "share/std"
            legacy_std.mkdir()
            (legacy_std / "obsolete.glo").write_text("obsolete::42;\n")
            subprocess.run(command, cwd=ROOT, check=True)
            prefix.rename(relocated)
            server = relocated / "bin/glolsp"
        else:
            (relocated / "build").mkdir(parents=True)
            server = relocated / "build/glolsp"
            shutil.copy2(ROOT / "build/glolsp", server)
            shutil.copytree(ROOT / "share", relocated / "share")
        assert os.access(server, os.X_OK), server
        assert not (relocated / "share/std").exists()
        assert any((relocated / "share/lib/clang").glob("*/include/stddef.h"))
        os.environ.pop("GLOSSO_STD", None)
        # A working directory outside the bundle catches paths accidentally
        # normalized from '$ORIGIN/../share' into 'share'.
        working = Path(temporary) / "unrelated"
        working.mkdir()
        os.chdir(working)
        integration.SERVER = server
        client = integration.LspClient()
        try:
            client.request("initialize", {
                "processId": None, "rootUri": working.as_uri(), "capabilities": {},
                "initializationOptions": {"scanWorkspace": False},
            })
            mappings = Path(f"/proc/{client.process.pid}/maps").read_text()
            assert str(relocated / "share/libglosso.so") in mappings, mappings
            assert str(ROOT / "share/libglosso.so") not in mappings
        finally:
            client.stop()
        # Includes Bindgen with standard headers, module arguments and overlays.
        subprocess.run([sys.executable, str(ROOT / "tests/compiler_lsp.py"),
                        "--server", str(server)], check=True)
        subprocess.run([sys.executable, str(ROOT / "tests/integration.py"),
                        str(server), "-k",
                        "test_default_configuration_reports_edits_and_uses_installed_std"],
                       check=True)
    print("standalone relocated bundle passed")


if __name__ == "__main__":
    main()
