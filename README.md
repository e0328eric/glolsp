# Glosso language server

`glolsp` is a stdio LSP server written in Glosso. It imports the standard
`Glosso` module and calls `libglosso` through its C ABI for compiler tokens, type checking, import
resolution, reflection, generated source, and compile-time evaluation.
Diagnostics come from the actual compiler, including UTF-16 locations and
related source information. No compiler subprocess is needed at runtime.

## Build and run

Install Glosso with `./build.exe install` from the compiler repository, then
build from this directory using the installed compiler:

```sh
~/.local/glosso/bin/glosso first.glo --cache-dir=build/cache
build/glolsp --stdio
```

The compiler, standard library, and `libglosso` must come from the same build.
No adjacent Glosso checkout is needed. For a custom installation prefix, use
`<prefix>/bin/glosso`. For development with adjacent source checkouts:

```sh
(cd ../glosso && ./build.exe libglosso)
../glosso/build/bin/glosso first.glo --cache-dir=build/cache
GLOSSO_STD="$PWD/../glosso/std" build/glolsp --stdio
```

`first.glo` supports `build` (default), `install`, `run`, `test`, and `help`, following
`--`. Build artifacts and caches live under `build/`. Tests need Python 3,
without third-party packages. The compiler's bounds and overflow guards remain
enabled. The build copies matching runtime files into `share/`: `libglosso.so`
and `lib/clang/` resource headers. Standard modules are read from the Glosso
installation, defaulting to `$HOME/.local/glosso/std`; they are not copied into
`share/`. Builds and reinstalls remove the obsolete `share/std` copy.
`src/glosso.glo` reexports
`#export "Glosso"(DYLIB_PREFIX = "$ORIGIN/../share");` using module parameters on Linux;
macOS uses `@executable_path/../share`. Windows places the DLL beside the executable.
Distribute `build/glolsp` together with `share/`, preserving that layout. The Linux
bundle can be moved and launched from any working directory. It requires the
installed Glosso standard library, but does not launch the compiler executable.
Rebuild to update the bundled frontend and headers.

To build and install into `$HOME/.local/glolsp`:

```sh
glosso first.glo -- install
~/.local/glolsp/bin/glolsp --stdio
```

Use `glosso first.glo -- install --prefix /path/to/glolsp` for another destination.
Installation copies the executable into `bin/` and the bundled runtime into
`share/`, preserving executable permissions. Windows also installs the DLL and
Clang headers beside the executable under `bin/`, and uses `%USERPROFILE%` for
the default home directory. The existing executable-relative `DYLIB_PREFIX`
works for this layout without changes to `src/glosso.glo`. Run `install` again
to update the installed bundle. A default user install also creates
`get_config_dir()/glolsp/config.toml` from `config.example.toml` if it is missing,
preserving any existing configuration. An explicit `--prefix` install leaves
user configuration alone.

Configure your editor with the absolute executable path, the `glosso` language
identifier, and `*.glo` files. Launch the executable directly: the build script
prints build progress, whereas the server reserves stdout for LSP frames.
The executable accepts `--stdio`, `--version`, and `--help`.
Restart the editor's LSP client after installing an update, especially if an
earlier process exited with a dynamic-library loading error.

## Editor features

- Full/incremental text synchronization with UTF-16 and CRLF positions.
- Push/pull compiler diagnostics, in-memory import overlays, and related locations.
- Completion, hover, definition, type definition, references, highlights, and rename.
- Document/workspace symbols, folding, semantic highlighting, and signature help.
- Inlay hints, selection ranges, import links, spelling/import quick fixes.
- Compiler-token-preserving document formatting.

Import completion lists standard modules after `#import ` and inside quoted
paths, including incomplete strings. Before the opening quote, accepting a
module inserts the quotes as well. Incomplete directives remain editable and
report compiler diagnostics without terminating the server.

Navigation and completion combine an error-tolerant Glosso parser with checked
facts from libglosso. Compiler-generated top-level functions, constants, globals,
and types participate in completion, hover, signatures, references and definition
navigation, including imports and aliases. Compiler-selected callees and inferred
local types refine the editor index. Generated structure members also support
completion and type definition. Partial compiler facts remain usable when another
function has an error.

Definitions open the generated source when the compiler can materialize it;
otherwise they point to the original `#insert`. Generated declarations cannot be
renamed through LSP: change the generating source instead of modifying a cache
artifact. Ordinary source rename retains lexical shadowing and workspace edits.

The snapshot is not a complete compiler semantic graph. Generated symbols can be unavailable when lexing/parsing fails before checking.
Statement-local generated bindings, audience-restricted exports, and some generic/typeclass projections
still rely on the fallback index. Ambiguous compiler facts are omitted.

Open buffers override disk, including dependencies and files that have not been
saved. Changing or closing a buffer refreshes dependent diagnostics. Watched
files and workspace-folder changes update the index. Relative `#import` paths,
module directories, and additional import search roots are supported. The server
does not run a workspace's `first.glo` to discover configuration.
By default it indexes opened files and their dependencies. Set `scanWorkspace`
to `true` to include unopened, unrelated files in workspace symbols and references;
this synchronous scan can delay requests in large repositories.

## Configuration

User configuration is read at startup from `get_config_dir()/glolsp/config.toml`,
using Glosso's `File_System/Directory` API. On Linux this is
`$XDG_CONFIG_HOME/glolsp/config.toml`, or `$HOME/.config/glolsp/config.toml` when
the XDG override is unset. Copy `config.example.toml` there and set:

```toml
compiler_path = "~/.local/glosso/bin/glosso"
```

The path names the Glosso executable. Its installation supplies `../std` relative
to `bin/`; development executables at `<checkout>/build/bin/glosso` use
`<checkout>/std`. Symlinks are resolved, `~/` expands to the home directory, and
relative paths are based on the configuration file's directory. Restart the LSP
client after changing the file. Analysis continues to use the bundled
`libglosso`; the configured compiler executable is not launched. Keep the
frontend and selected standard library from matching Glosso builds.
Malformed configuration is reported on stderr and falls back to the default.

Use these `initializationOptions`:

```json
{
    "stdPath": "/absolute/path/to/glosso/std",
    "importPaths": ["/absolute/path/to/project/modules"],
    "scanWorkspace": false,
    "maxFiles": 4096,
    "comptimeStepLimit": 16777216
}
```

Standard-library precedence is `stdPath`, `GLOSSO_STD`, the installation selected
by `compiler_path` in `config.toml`, then `$HOME/.local/glosso/std` (the standard
library's home-directory query supplies the platform equivalent on Windows).
Import completion and compiler diagnostics use this same directory.
The instruction limit applies per compile-time interpreter invocation; zero
means unlimited. Normal compiler I/O is captured inside libglosso so a Glosso
`print` at compile time cannot break JSON-RPC framing. Failed analysis includes
captured stdout and stderr in its first diagnostic, limited to 16 KiB per stream.
Native C builds and Bindgen use the SDK beside the selected standard library.
Compilation has the same host I/O/FFI semantics as the compiler; it is not a
sandbox, and native calls are not interrupted by the instruction budget.
Nested native-driver build operations are outside the frontend library's scope
and report diagnostics.

Analysis uses the compiler's program roots: project functions are checked even
when unused, and imported standard-library bodies are checked on demand.
Compiler analysis runs on a background worker. Completion, navigation and other
read-only features use the current document index while checking is in progress;
completion reports `isIncomplete` until checked facts arrive. Diagnostics and
rename wait for the current compiler snapshot without blocking other requests.
Edits coalesce while the worker is busy, and results from obsolete input revisions
are discarded. Pending requests support cancellation; shutdown does not wait for
compile-time I/O. Repeated requests do not reevaluate compile-time code.
Source edits, imported buffer changes, watched files and configuration changes
invalidate snapshots and generated bindings. Clients that support refresh requests
receive updated semantic tokens and inlay hints when checking finishes.
External files read by compile-time code need an editor file-change notification
or a source edit to refresh the snapshot. The next analysis creates a fresh
compiler context; there is no cancellation of an in-progress native call yet.

## Tests

```sh
~/.local/glosso/bin/glosso first.glo --cache-dir=build/cache -- test
python3 tests/compiler_lsp.py --server build/glolsp
```

Measure open, edit, and completion latency against a real project with:

```sh
python3 tests/performance_lsp.py ../focus-glosso/main.glo --server build/glolsp
```

The benchmark sends edits as unsaved overlays, leaves project files unchanged,
and prints timings and medians as JSON lines, including completion while initial
and edited diagnostics are pending. Use `--root` if the entry file is
below the workspace root, or `--iterations` to change the number of edits.

Seven native Glosso suites cover syntax, tokens, layout, protocol, workspace
ownership, and navigation analysis. The stdio suites exercise editing, rename,
imports, completion, formatting, and lifecycle behavior. `compiler_lsp.py`
checks compile-time failure/insertion, captured failure output and locations,
step limits, UTF-16
incremental edits, unsaved dependency changes, and restoring disk on close. It
also checks generated APIs, import visibility, snapshot invalidation, compiler
inference, generated-rename rejection, incomplete-directive recovery, and avoiding
repeated compile-time I/O. The integration suite checks diagnostics and standard
module completion with default editor initialization in the actual checkout.
`config_lsp.py` checks TOML configuration, XDG paths, compiler symlinks, relative
paths, installation layouts, native SDK lookup, override precedence, and invalid
configuration.

`src/worker.glo` owns the input and compiler workers; the main event loop alone
updates the workspace. `src/compiler.glo` owns the libglosso snapshots and applies
generated namespaces and checked facts. `language.glo` consumes compiler tokens;
`syntax.glo`, `model.glo`, `workspace.glo`, `analysis.glo`, and
`ide.glo` maintain the editor index. `server.glo` and `protocol.glo` handle LSP.

On Linux, `python3 tests/standalone.py` copies the executable and `share/` into
a temporary directory, starts it from an unrelated working directory with
`GLOSSO_STD` unset, checks the loaded library path and absence of `share/std`, and
runs the compiler/LSP suite using the default installed std, including Bindgen
against the copied headers.
Pass `--compiler /path/to/glosso` to test installation and reinstallation into a
temporary prefix with spaces, then run the same checks after moving that bundle.
