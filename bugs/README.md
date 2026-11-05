# Open issues

Left over from the 2026-11-05 bug hunt. Everything else found then is fixed.

## 1. Windows: document analysis is untested

The Windows code paths in `src/worker.glo` were checked only by
cross-compiling and running under wine:

- `worker_private_stdio` moves the LSP streams to private handles
  (`DuplicateHandle`), points the standard handles at `NUL` and stderr
  (`SetStdHandle`), and rebinds the C runtime's descriptors 0 and 1 (`_dup2`).
- `worker_spawn` starts the compiler thread with `CreateThread` and a 256 MiB
  reserved stack.

Under wine the server handles `initialize`, `workspace/symbol`, a skipped
message of 64 MiB + 1 bytes, an ignored `--clientProcessId` flag, and
`shutdown`/`exit` with exit code 0. Bytes piped into stdin reach the server.
A marker written to standard output by msvcrt `_write(1)` appears on stderr,
and `_read(0)` returns 0, as intended.

Not tested: there is no Windows `glosso.dll`, so nothing that calls libglosso
(lexing, parsing, compiler diagnostics, the compiler worker) has run on Windows.
Also untested: real Windows as opposed to wine, and compile-time code that
starts subprocesses.

To do once a Windows libglosso exists: run the full `first.glo -- test` suite
on Windows, and check that compile-time code reading stdin or writing stdout
does not touch the LSP stream (the Windows form of bug C4).

## 2. A malformed header still stops the server

Intended behaviour, recorded so it is not reported again.

`rpc_read_frame` (`src/protocol.glo`) returns status -1 when a header is malformed:
`Content-Length` is missing or not a number, the header is longer than
16384 bytes, or input ends inside a header or body. The main loop
(`src/server.glo`, `case .input(frame)`) then writes
`invalid LSP frame: <reason>; exiting` to stderr and exits with code 1.

The server cannot recover from this, because without a valid length it cannot
tell where the next message starts. Skipping to the next `Content-Length:`
would be a guess that can land inside a message body.

A message whose header is valid but whose body is rejected is skipped. The
stream stays aligned and the server answers with a -32600 error. Such
messages are those larger than 64 MiB (up to `MAX_SKIPPED_BYTES`, 1 TiB) and those
with an unsupported `Content-Type` charset.

## 3. Opening a large file: lexing cost

`tests/regress_server.py: test_large_document_below_source_limit_opens`
(11 MiB of source) takes about 3.2 s, most of it on the main thread in
`workspace_replace_source` → `parse_file`, mostly lexing.

A callgrind profile of the main thread during the open showed:

| Share | Where | Cause |
|-------|-------|-------|
| ~13% | std `Glosso/Tokens.glo`, `glosso_tokens` | `glosso_text(token.kind).clone()` allocates a string for every token |
| ~11% | `src/language.glo`, `language_lex` | `if token.kind == { case "Whitespace", ... }` compares those strings with `memcmp` |

Together these are about a quarter of the open. The rest of that profile:
about 70% went to parsing as a whole, 18.6% to `analyze_document` (since
removed: folding, formatting, links and selection ranges no longer run
semantic analysis), and about 4% to frame handling.

Suggested fix, in std rather than glolsp: give `Glosso_Token.kind` an enum
or numeric id (or a view into a static table) instead of a cloned `str`.
`language_lex` can then switch on the id, and `Glosso_Tokens.free` no longer
frees one string per token. This changes the std API, so `language_lex` must
be updated in the same change.
