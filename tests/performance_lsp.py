#!/usr/bin/env python3
"""Measure editor latency on a real project without changing its files."""

import argparse
import json
import statistics
import time
from pathlib import Path

import integration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--server", type=Path, default=integration.SERVER)
    parser.add_argument("--root", type=Path, help="workspace root (default: source directory)")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    if args.iterations < 1 or args.timeout <= 0:
        parser.error("iterations and timeout must be positive")
    integration.SERVER = args.server.resolve()
    source = args.source.resolve()
    text = source.read_text()
    uri = source.as_uri()
    root = (args.root or source.parent).resolve()

    class Client(integration.LspClient):
        def wait_for(self, predicate, timeout=None):
            return super().wait_for(predicate, args.timeout if timeout is None else timeout)

    samples = {}

    def measure(label, action):
        start = time.perf_counter()
        result = action()
        elapsed = time.perf_counter() - start
        samples.setdefault(label, []).append(elapsed)
        record = {"phase": label, "seconds": round(elapsed, 4)}
        if label.endswith("diagnostics"):
            record["diagnostic_count"] = len(result)
        print(json.dumps(record), flush=True)
        return result

    client = Client()
    try:
        measure("initialize", lambda: client.request("initialize", {
            "rootUri": root.as_uri(), "capabilities": {},
        }))
        client.notify("initialized", {})
        position = integration.position(text, "", offset=len(text))
        params = {"textDocument": {"uri": uri}, "position": position}
        complete = lambda: client.request("textDocument/completion", params)

        def open_document():
            client.open(uri, text)
            measure("open_pending_completion", complete)
            return client.diagnostics(uri, 1)

        measure("open_diagnostics", open_document)
        measure("first_completion", complete)
        measure("cached_completion", complete)
        for version in range(2, args.iterations + 2):
            def edit():
                client.change(uri, [{"text": text + f"\n// benchmark edit {version}\n"}], version)
                measure("edit_pending_completion", complete)
                return client.diagnostics(uri, version)

            measure("edit_diagnostics", edit)
            measure("edit_completion", complete)
        print(json.dumps({"median_seconds": {
            label: round(statistics.median(values), 4) for label, values in samples.items()
        }}), flush=True)
    finally:
        client.stop()


if __name__ == "__main__":
    main()
