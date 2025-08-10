# Pytead (aka **T3AD** — Trace To Test And Doc)

> Capture **real** Python function calls while your app runs, then turn those traces into **pytest** tests — and (soon) into **living examples** for your docs.

I built Pytead to give LLMs and humans concrete usage examples of functions with **zero manual wiring**. It records real calls while you run your program and turns them into deterministic, parameterized tests. It’s handy for vibe-coding and perfectly fine in traditional projects.

---

## ✨ Features

* **Runtime tracing** of a target function’s *real* calls (args, kwargs, return value, timestamp).
* **Deterministic test generation** → parameterized **pytest** tests.

---

## 📦 Installation

For now, install from your local clone:

```bash
git clone https://github.com/<you>/pytead
cd pytead
pip install -e .
```

---

## 🚀 Quickstart

Say you have ...

```python
# mymodule.py
def multiply(a, b):
    return a * b
```

... and you call it in ...

```python
# main.py
from mymodule import multiply

for (x, y) in [(2, 3), (2, 3), (10, 0)]:
    multiply(x, y)
```

... let's use pytead on it :

### 1) Trace real calls while running your app

```bash
pytead run mymodule.multiply -- main.py
```

This launch your program as usual, but will write trace files while you're using it.

### 2) Generate pytest tests from traces

```bash
pytead gen
```

This writes tests like `tests/generated/test_mymodule_multiply.py` using `@pytest.mark.parametrize`.

You can run them later, after working on the code, to check your function still behave the same

```bash
pytest -q
```

---

## 🎛️ CLI reference (overview)

### `pytead run`

Instrument one or more **module-level** functions and execute a Python script.

```
pytead run [options] <module.function> [...] -- <script.py> [script args...]
```

**Common options**

* `-l, --limit INT` — max calls to record per function
* `-s, --storage-dir PATH` — where to write trace files (default: from config; packaged default: `call_logs/`)
* `--format {pickle,json,repr}` — storage format

**Notes**

* Targets must be `package.module.function`. Class methods (`module.Class.method`) are not yet supported via the CLI (decorate at definition site or wrap at module level).
* Only the **root** invocation of the traced function is recorded per call stack (thread-local depth control).
* If you forget `--` and put `script.py` after targets, Pytead will do its best to split correctly.

---

### `pytead gen`

Generate pytest tests from previously recorded traces.

```
pytead gen [options]
```

**Common options**

* `-c, --calls-dir PATH` — directory containing trace files
* `-o, --output PATH` — write a single test module
* `-d, --output-dir PATH` — write one test module **per function** into this directory
* `--formats {pickle,json,repr}...` — restrict which formats to read

**Behavior**

* Exact **duplicates** (same args/kwargs/result) are deduplicated.
* Values are embedded using `repr(...)` in the generated code.

---

### `pytead tead` (all-in-one)

Trace **and** immediately generate tests in one go.

```
pytead tead [options] <module.function> [...] -- <script.py> [script args...]
```

**Extras**

* `--pre-clean` — delete existing traces for targeted functions before tracing
* `--pre-clean-before YYYY-MM-DD|ISO8601` — only delete older traces
* `--gen-formats {pickle,json,repr}...` — restrict formats when reading for generation
* `--only-targets` — generate tests **only** for the functions targeted in this command
* `-o/--output` or `-d/--output-dir` — same as `gen` (defaults to a single file if neither is provided)

---

### `pytead clean`

Delete trace files by function, pattern, format, and/or date. Examples:

```bash

# Narrow deletion to selected functions (exact names) and formats
pytead clean --func mymodule.multiply --formats pickle json
```

Run `pytead clean -h` for the full set of options.

---

## 🧩 Decorator mode

Prefer to trace without the CLI? Decorate directly:

```python
from pytead import trace

@trace(limit=5, storage_dir="call_logs")
def multiply(a, b):
    return a * b
```

Run your program normally; traces will be written the same way. Then:

```bash
pytead gen
```

### Custom storage example

```python
from pathlib import Path
import json, uuid

class MyJsonStorage:
    extension = ".json"

    def make_path(self, storage_dir: Path, func_fullname: str) -> Path:
        storage_dir.mkdir(parents=True, exist_ok=True)
        prefix = func_fullname.replace(".", "_")
        return storage_dir / f"{prefix}__{uuid.uuid4().hex}{self.extension}"

    def dump(self, entry: dict, path: Path) -> None:
        path.write_text(json.dumps(entry, default=str), encoding="utf-8")

    def load(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

from pytead import trace

@trace(storage_dir="call_logs", storage=MyJsonStorage())
def f(x):
    ...
```

---

## ⚙️ Configuration

Pytead reads optional configuration from TOML files. **CLI flags always override** config values. Within a config file, a command section (e.g. `[run]`) overrides `[defaults]`.

### Where Pytead looks for config (in order)

**Project-local** (walk upward from the current working directory):

1. `./.pytead/config.toml`

**User-level** (fallbacks):

1. `$PYTEAD_CONFIG` (explicit file path, or a directory containing `config.toml`)
2. `$XDG_CONFIG_HOME/pytead/config.toml` (or `~/.config/pytead/config.toml`)
3. `~/.pytead/config.toml`

If no user/project config is found, Pytead loads the **packaged fallback**: `pytead/default_config.toml`.

### Precedence

1. **CLI flags** (highest)
2. Section for the command (e.g. `[run]`, `[gen]`, `[clean]`, `[tead]`)
3. `[defaults]` section

### Example: project config (`.pytead/config.toml`)

```toml
[defaults]
limit = 10
storage_dir = "call_logs"
format = "pickle"

[run]
limit = 7
targets = ["mymodule.multiply"]

[gen]
output_dir = "tests/generated"

[clean]
calls_dir = "call_logs"

[tead]
targets = ["mymodule.multiply"]
calls_dir = "call_logs"
only_targets = true
```

---

## 🛠️ How it works

* **Monkey-patching**: `pytead run/tead` imports your module, wraps `module.function` with the tracer, then runs your script. Your code calls the wrapped function transparently.
* **Root-call only**: a thread-local depth counter ensures only the outermost invocation is logged.
* **Trace formats**:

  * **pickle** (default): safest for round-tripping Python types.
  * **json**: portable; non-JSON types fall back to `repr(...)`.
  * **repr**: human-readable Python literals; loaded via `ast.literal_eval`.
* **Test generation**: tests import your function (`from pkg.mod import fn`) and assert `fn(*args, **kwargs) == expected` using parameterized cases.

---

## ⚠️ Limitations & caveats

* **Methods / attributes**: CLI targets `module.function` only (no `module.Class.method` yet).
* **Side-effects & exceptions**: not captured in the current version. Tests assume pure behavior.
* **Non-repr-able results**: generated code relies on `repr(...)`. Complex/custom objects may not round-trip.
* **Flaky functions**: time/random-dependent functions may yield nondeterministic tests.
* **Multiprocess tracing**: `limit` is best-effort (no cross-process locking).
* **Security**: trace files are **not** meant to be trusted input. Never load untrusted pickles.

---

## 🗺️ Roadmap

* Capture **exceptions** and generate `with pytest.raises(...)` cases.
* Opt-in capture of **side-effects** (stdout, file I/O summaries, env changes).
* CLI support for **`module.Class.method`** targets.
* Pluggable **serialization** (e.g., jsonpickle) in the CLI.
* Smarter **deduplication** and flaky-test detection (auto-run pytest and discard unstable cases).
* **Doc enrichment**: promote real traces as runnable examples in docstrings/Markdown.

---

## 🔗 Related tools & approaches

* **Snapshot testing** (`pytest-snapshot`, `snapshottest`, `Syrupy`): great for outputs, but don’t harvest runtime **inputs**.
* **Synthetic test generation** (**Pynguin**): explores inputs for coverage, not based on your real executions.
* **AOP / tracers** (`aspectlib`, `sys.settrace`): intercept calls but don’t emit ready-to-run pytest modules.
* **Similar spirit**: **Keploy** (focus on external I/O) and various JS “record to unit test” tools.

---

## 📝 License

[MIT](LICENCE)

