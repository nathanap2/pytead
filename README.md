# Pytead (aka **T3AD** — Trace To Test And Doc)

> Capture **real** Python function calls while your app runs, then turn those traces into **pytest** tests — and (soon) into **living examples** for your docs.

For your information, I developed this module to improve the compatibility of my code with LLMs because I noticed that LLMs understood the role of a function better when I gave them a concrete, real-world example of how that function was used (for example, through a unit test or directly in the function documentation). I wanted to automate the process. The module is therefore designed for vibe coding or similar, but it can also be used in more traditional contexts.


---

## ✨ What it does

* **Runtime tracing** of a target function’s *real* calls (args, kwargs, return value, timestamp).
* **Deterministic test generation**: turns traces into parameterized pytest tests.

---

## 📦 Installation

```bash
# clone your fork
pip install -e .
```

Requirements: Python ≥ 3.7. License: MIT.

The install exposes a single CLI entry point:

* `pytead` — with subcommands `run` and `gen`.

---

## 🚀 Quickstart

Suppose you have:

```python
# mymodule.py

def multiply(a, b):
    return a * b
```

```python
# main.py
from mymodule import multiply

for (x, y) in [(2, 3), (2, 3), (10, 0)]:
    multiply(x, y)
```

### 1) Trace real calls while running your app

```bash
pytead run \
  --limit 5 \
  --storage-dir call_logs \
  mymodule.multiply \
  -- python3 main.py
```

You should see logs in `call_logs/` like:

```
mymodule_multiply__<uuid>.pkl
```

Each pickle contains a dict like:

```python
{
  "func": "mymodule.multiply",
  "args": (2, 3),
  "kwargs": {},
  "result": 6,
  "timestamp": "2025-08-08T19:21:15.123456"
}
```

### 2) Generate pytest tests from traces

**Single file**:

```bash
pytead gen -c call_logs -o tests/test_pytead_generated.py
```

**One file per function**:

```bash
pytead gen -c call_logs -d tests/generated
```

This will produce files like `tests/generated/test_mymodule_multiply.py` using `@pytest.mark.parametrize`.

Run them with:

```bash
pytest -q
```

---

## 🎛️ CLI reference

### `pytead run`

Instrument a **module‑level** function and execute a Python script.

```
pytead run [options] <module.function> -- <script.py> [script args...]
```

**Options**

* `-l, --limit INT` — max calls to record per function (default: 10)
* `-s, --storage-dir PATH` — where to write trace pickles (default: `call_logs/`)

**Notes**

* The target must be in the form `package.module.function` (exactly one final identifier). Class or nested methods (`module.Class.method`) are **not** supported yet via the CLI.
* Only the **root** invocation of the traced function is recorded in a call stack (thread‑local depth control).

### `pytead gen`

Generate pytest tests from previously recorded traces.

```
pytead gen [options]
```

**Options**

* `-c, --calls-dir PATH` — directory containing `.pkl` traces (default: `call_logs/`)
* `-o, --output PATH` — write a single test module (default: `tests/test_pytead_generated.py`)
* `-d, --output-dir PATH` — instead of a single file, write one test module **per function** into this directory

**Behavior**

* Exact duplicate cases (same `args`, `kwargs`, and `result`) are deduplicated.
* Values are rendered with `repr(...)` into the generated test code.

---

## 🧩 Decorator mode (alternative to CLI)

You can also decorate the function directly:

```python
from pytead import trace

@trace(limit=5, storage_dir="call_logs")
def multiply(a, b):
    return a * b
```

Running your program will then emit the same `.pkl` traces; generate tests with `pytead gen` as above.

You may also provide a custom serializer (+ a method to dump/read) :

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
        data = json.loads(path.read_text(encoding="utf-8"))
        return data

from pytead import trace
@trace(storage_dir="call_logs", storage=MyJsonStorage())
def f(x): ...
    ...

```


---

## 🛠️ How it works (design notes)

* **Monkey‑patching**: `pytead run` imports the target module, wraps `module.function` with the decorator, and then runs your script. Your code calls the wrapped function transparently.
* **Root‑call only**: a thread‑local depth counter ensures only the outermost invocation of the traced function is logged (avoids a storm of nested traces).
* **Pickle traces**: by default, traces are saved as pickle files to keep Python types round‑trippable. Generation uses `repr(...)` to embed literals in test code.
* **Parameterized tests**: for each traced function, tests are generated with `@pytest.mark.parametrize('args, kwargs, expected', [...])` and a single `assert func(*args, **kwargs) == expected`.

---

## ⚠️ Limitations & caveats (current state)

* **Methods / attributes**: the CLI targets `module.function` only; `module.Class.method` isn’t supported yet (workaround: decorate at definition site, or expose a module‑level wrapper and trace that).
* **Side effects & exceptions**: not yet captured. Tests assume **pure** behavior (idempotent, no I/O or global state).
* **Non‑reprable results**: generated code relies on `repr(...)`. Highly custom objects may not round‑trip. Prefer simple / JSON‑like data for now.
* **Flaky functions**: if a function is time‑ or randomness‑dependent, generated tests may fail nondeterministically.

---

## 🗺️ Roadmap

* Capture **exceptions** and generate `with pytest.raises(...)` cases.
* Opt‑in capture of **side‑effects** (stdout, file I/O summaries, env changes).
* Support for **`module.Class.method`** targets in the CLI.
* Pluggable **serialization** (JSON schema / jsonpickle) shipped in the CLI. -> in progress
* Smarter **deduplication** 
* **Doc enrichment**: promote real traces as runnable examples in docstrings / Markdown to aid LLM‑assisted code reading.

---

## 🔗 Related tools & approaches

* **Snapshot testing** (e.g., `pytest-snapshot`, `snapshottest`, `Syrupy`): good for pinning outputs in tests, but they don’t harvest *runtime inputs* from production runs.
* **Synthetic test generation** (e.g., **Pynguin**): explores inputs for coverage, not based on *your* real executions.
* **AOP / tracers** (e.g., `aspectlib`, `sys.settrace`): can intercept calls, but do not automatically emit ready‑to‑run pytest modules.
* **Similar spirit elsewhere**: tools like **Keploy** (focus on external I/O) and some JS utilities (e.g., unit‑test recorders) share the trace‑to‑tests idea but target different layers.

---

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENCE)
