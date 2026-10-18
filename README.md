# Glosso Language Server

`glolsp` is a stdio language server implemented in Glosso. It parses source into
an AST, loads workspace dependencies, resolves declarations across files, and
infers types for editor queries. `first.glo` is the build entry point, following
the adjacent `glosso` and `glosso-manual` repositories.

Build and test from this repository's root with the installed compiler:

```sh
glosso first.glo
glosso first.glo -- test
```

Use a current Glosso compiler and matching standard library, including the
move-by-default ownership rules and Prelude `Clone`/`Copy` typeclasses. The LSP's
value records explicitly derive `Clone, Copy`; workspace storage remains owned
and array traversal borrows views.

The default task produces `build/glolsp`. The `build`, `run`, `test`, and `help`
tasks are also available explicitly. Tests require Python 3; there are no
package manager dependencies. The build driver uses `Compiler.Workspace` with
the compiler executing `first.glo`. Executables and caches go under `build/`.
The server build retains runtime bounds and overflow checks.

Configure your editor to launch the absolute path to `build/glolsp`, associate
it with `*.glo`, and use the `glosso` language identifier. For example, a generic
LSP client configuration is:

```json
{
    "command": ["/absolute/path/to/glolsp/build/glolsp", "--stdio"],
    "filetypes": ["glosso"]
}
```

The executable accepts `--stdio`, `--help`, and `--version`. Standard output
carries LSP messages exclusively. Launch the executable directly from an editor;
the development `run` task also produces compiler output while building.
The server does not invoke the compiler at runtime.

Implemented editor features include:

- Full and incremental document synchronization with UTF-16 and CRLF positions.
- Workspace indexing, dependency loading, implicit Prelude, and module aliases.
- Completion of visible declarations, inferred members, import paths, keywords, and directives.
- Hover with signatures, documentation, and inferred variable types.
- Definition, declaration, type definition, references, and document highlights.
- Rename across loaded workspace files, with scope and name-conflict checks.
- Document and workspace symbols, folding, and semantic highlighting.
- Signature help, inferred variable inlay hints, and expanding selection ranges.
- Import document links and token-preserving document formatting.
- Spelling corrections and import quick fixes for unresolved public declarations.
- Push and pull diagnostics for syntax, imports, names, members, and type mismatches.

Navigation and rename use resolved declaration identities, including unopened
workspace files. Resolution observes lexical scopes, shadowing, public and unit
visibility, explicit exports, imports, embedded loads, and inherent methods.
Typeclass operations are visible alongside their owning class, including through
re-exports and module aliases. Built-in type recognition and completion share the
compiler's supported types, including `Code`, `Pattern`, and arbitrary-width integers.
The `_` blackhole discards values without introducing a declaration.
Type analysis covers declared and inferred types, aliases, procedure returns,
pointer and array access, generic substitutions, nested member chains, pattern
payloads, and tuple destructuring.
Aggregate pointers support `pointer.field`, nested chains, and field addresses.
Value and pointer receivers participate in navigation and signature help;
generic inherent heads specialize their methods for the receiver's type.
Matrix literals use `#matrix .[a, b; c, d]`. The server checks rectangular rows,
known dimensions and element types, preserves element references for navigation
and rename, and infers matrix indexing results. Literal decimal dimensions and
simple constant aliases are recognized; unevaluated dimensions stay unknown.
`#memory,trusted` blocks retain ordinary lexical scopes and name diagnostics.
Leading contracts, call assertions, named overlays, and `#derive(Clone, Copy)`
on structures and enums are accepted. Completion includes the current directives
and renamed standard-library APIs loaded from the configured standard library.
Unknown types remain unknown instead of producing speculative type errors.

Open buffers take precedence over disk contents. Changed buffers are reparsed;
unchanged files reuse their parsed ASTs. Semantic results are cached until the
workspace changes. Watched dependency changes invalidate the results, while
closing a buffer restores its disk contents. Workspace folders can be added and
removed while the server is running. Clients supporting dynamic registration
receive a watcher registration for `**/*.glo`.

The standard library defaults to `$GLOSSO_STD`, then `$HOME/.local/glosso/std`.
The server loads the implicit Prelude and resolves named imports through the
standard library and additional search paths. Both `Name.glo` and
`Name/module.glo` are supported; an ambiguous pair produces an import diagnostic.
Relative `#load` paths resolve from the importing file's directory. The server
never runs a workspace's `first.glo` to discover configuration.

Import completion suggests module names inside `#import "..."` from these search
paths, including modules in unopened files and open buffers. Directory suggestions
end in `/` to continue into nested modules. Relative `#load` and `#export` paths
complete source filenames, including their `.glo` extension. Suggestions work in
unfinished strings and replace the path while preserving its quotes.

Optional `initializationOptions` configure loading:

```json
{
    "stdPath": "/absolute/path/to/glosso/std",
    "importPaths": ["/absolute/path/to/project/modules"],
    "scanWorkspace": true,
    "maxFiles": 4096
}
```

Root scanning skips `.git` and `build` directories. Files have a 16 MiB source
limit; import traversal and directory recursion are bounded. Workspace symbol
search excludes the closed standard library unless it is itself a workspace
root. Completion edits replace the complete identifier or directive, including
the `#` prefix. Formatting preserves token spelling and leaves malformed or
unsafe-to-format source unchanged.

The implementation is split into `syntax.glo` and `model.glo` for parsing and
AST data, `workspace.glo` for source ownership and dependencies, `analysis.glo`
for binding and type inference, and `ide.glo` for editor queries. `server.glo`
handles the protocol and synchronization; `protocol.glo`, `positions.glo`,
`language.glo`, `semantic.glo`, `highlight.glo`, and `layout.glo` provide support.

The test task runs seven native Glosso suites and two stdio integration suites.
They cover parser recovery and arena growth, import visibility, pointer fields
and receivers, generic member inference, cross-file rename, disk changes and
overlays, UTF-16 edits, malformed
requests, formatting, and repeated document updates. The language, protocol,
highlighting, syntax, and layout suites use strict compiler memory checking.
The workspace and analysis suites use the compiler's default warning mode:
its ownership summaries still report warnings for mutable nested workspace
storage. Compile either suite directly to inspect these warnings; memory
checking is never disabled.
Lifecycle regressions cover aliasing, cleanup, and repeated updates as well.
Current-language regressions also cover matrix literals, trusted blocks,
derivation annotations, brace format strings, and renamed path and process APIs.
The JSON parser call has a narrow trusted boundary for the standard library's
internal container operations; source is borrowed during parsing and decoded
documents own their contents.

Analysis is an independent implementation, not the compiler's complete semantic
engine. It does not execute `#comptime`, expand macros or generated source,
evaluate reflection, or implement the compiler's full typeclass solver.
It also does not enforce the compiler's move, borrow, or unsafe-call checks.
Queries depending on those operations can return incomplete results. Ordinary
source is parsed even when incomplete, and supported declarations remain usable
after a nearby syntax error. Compiler diagnostics are not invoked because the
installed compiler executes top-level compile-time code on every backend.

Source follows four-space indentation and a maximum line length of 100
characters. Long one-line strings use `#string,oneline`, character matching uses
`#char`, and fixed-value dispatch uses switches where they make the code clearer.
Aggregate pointer fields use `pointer.field`. Pointers to arrays retain explicit
dereferences for array members, such as `pointer.*.count`.
