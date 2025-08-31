# Pytead (aka **T3AD** — Trace To Test And Doc)

> Capture **real** Python function calls while your app runs, then turn those traces into **pytest** tests — and (soon) into **living examples** for your docs.

I built Pytead to give LLMs and humans concrete usage examples of functions with **zero manual wiring**. It records real calls while you run your program and turns them into deterministic, parameterized tests. It’s handy for vibe-coding and perfectly fine in traditional projects.

---

## 📦 Installation

For now, install from your local clone:

```bash
git clone https://github.com/<you>/pytead
cd pytead
pip install -e .
````

---

## 🚀 Quickstart

Say you have:

```python
# mymodule.py
def multiply(a, b):
    return a * b
```

…and you call it in:

```python
# main.py
from mymodule import multiply

for (x, y) in [(2, 3), (2, 3), (10, 0)]:
    multiply(x, y)
```

Use Pytead:

### 1) Trace real calls while running your app

```bash
pytead run mymodule.multiply -- main.py
```

This launches your script **as usual** and writes trace files as calls happen.

### 2) Generate pytest tests from traces

```bash
pytead gen
```

This writes tests such as `tests/generated/test_mymodule_multiply.py` using `@pytest.mark.parametrize`.

Then run:

```bash
pytest -q
```

---

## 🎛️ CLI overview

### `pytead run`

Instrument one or more targets and execute a Python script.

```
pytead run [options] <module.function|module.Class.method> [...] -- <script.py> [script args...]
```

**Common options**

* `-l, --limit INT` — max calls to record per function/method
* `-s, --storage-dir PATH` — where to write trace files (default via config; packaged default: `call_logs/`)
* `--format {pickle,graph-json}` — storage format
* `--additional-sys-path PATH...` — extra import roots (relative paths are anchored on the project root)

**Notes**

* Targets can be `package.module.function` **or** `package.module.Class.method`.
* Only the **root** invocation per call stack is recorded (thread-local depth control).
* If you forget `--`, Pytead tries to split args robustly anyway.

---

### `pytead gen`

Generate pytest tests from previously recorded traces.

```
pytead gen [options]
```

**Common options**

* `-c, --storage-dir PATH` — directory containing trace files
* `-o, --output PATH` — write a single test module
* `-d, --output-dir PATH` — write **one test module per function** into this directory
* `--formats {pickle,graph-json}...` — restrict which formats to read
* `--additional-sys-path PATH...` — extra import roots to embed in generated tests

Note : exact **duplicates** (same args/kwargs/result) are deduplicated.

---

### `pytead tead` (trace and generate in one go)

```
pytead tead [options] <module.function|module.Class.method> [...] -- <script.py> [script args...]
```

**Extras**

* `--gen-formats {pickle,graph-json}...`
* `--only-targets` — only generate tests for the targets in this command
* `-o/--output` or `-d/--output-dir` — same as `gen` (defaults to a single file if neither is provided)

---

### `pytead types` (experimental)

Infer rough types from traces and emit `.pyi` stubs.

```
pytead types --calls-dir CALLS --out-dir TYPINGS [--formats ...]
```

---

## 🧩 Decorator mode

Prefer to trace without the CLI? Decorate directly:

```python
from pytead import trace

@trace(limit=5, storage_dir="call_logs")
def multiply(a, b):
    return a * b
```

Run your program normally; traces are written the same way. Then:

```bash
pytead gen
```

---

## 🧠 Storage formats: when to use which?


* **`graph-json`**: captures **nested object graphs** (attributes of attributes, cycles, shared references via `{"$ref": N}`), and generates **standalone-ish** tests:

  * Arguments/results are rehydrated **without calling** user constructors.
  * If type annotations are missing, Pytead falls back to a lightweight **shell** (e.g., `SimpleNamespace`) so attribute access like `obj.a.m` still works.
  * Designed for code that reads/writes object state; if nested methods must run with real behavior, prefer `pickle`.
* **`pickle`**: exact Python object round-trips (least portable, currently more limited in this repo — to be revisited).

---

## ⚙️ Configuration

Pytead loads config in layers:

1. packaged defaults (`pytead/default_config.toml`)
2. user-level config (`$XDG_CONFIG_HOME/pytead` or `~/.config/pytead`, etc.)
3. nearest project config (`.pytead/config.{toml,yaml,yml}`)

Precedence: **CLI > command section > \[defaults]** (user & project) > **packaged**.

Example:

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

[tead]
targets = ["mymodule.multiply"]
storage_dir = "call_logs"
only_targets = true
```

---

## 🧪 What generated tests look like

### State-based formats

* One test module that:

  * bootstraps imports (`testkit.setup`),
  * holds a deduplicated list of cases,
  * replays the call and **compares the result** (or, when applicable, object state).

### Graph snapshots (`graph-json`)

* **One test file per function** that:

  * bootstraps import roots (project root + any additional paths),
  * imports `assert_match_graph_snapshot` and `rehydrate_from_graph` from `pytead.testkit`,
  * for each trace:

    * **rehydrates** arguments (no `__init__` calls) and calls the function/method,
    * compares the **rendered graph** (no `$id`; external refs inlined) via `assert_match_graph_snapshot`.


**Normalization in tests** (so they remain stable and valid Python):

* **Tuples vs lists**: tuples are normalized to lists on both sides for comparisons (JSON encodes lists).
* **NaN / ±Inf**: sanitized to `None` in generated code and in runtime comparisons.
* **Aliasing**: captured as `{"$ref": N}`. For equality checks, the runtime side is “de-aliased” so structurally equal graphs compare equal even if the real object reused substructures.


---

## 🔬 Advanced notes

### Import bootstrapping & `sys.path.append`

Generated tests insert import roots with this order:
1) **project root**
2) **script directory**,
3) **additional_sys_path**


### Graph snapshot semantics

* **Markers**:

  * `{"$map": [[k_graph, v_graph], ...]}` for mappings with non-JSON keys,
  * `{"$set": [...], "$frozen": bool}` for sets and frozensets,
  * `{"$ref": N}` for shared references (aliasing).
* In tests, `graph_to_data` rebuilds hashable keys (e.g., list keys → tuples, set keys → frozensets) and handles the markers consistently. Comparisons use a canonicalized view.

### Rehydration (no-init) & shell fallback

* `rehydrate_from_graph` builds instances **without calling** `__init__`, then assigns attributes.
* When **type hints** exist, they guide deep rehydration (lists, dicts, sets, nested objects — also no-init).
* When hints are missing, Pytead applies a **shell** fallback so `obj.a.m` is readable without real nested classes.

### When to prefer `pickle`

* If the tested code **calls methods** on nested objects and you want those methods to run with real behavior, `graph-json` (which shellifies innards) is not enough — use `pickle` or a future characterization mode.

---

## 🧭 Roadmap

* Capture **exceptions** and generate `with pytest.raises(...)`.
* Opt-in capture of **side-effects** (stdout, file I/O summaries, env changes).
* Full CLI UX for `module.Class.method`.
* Option to **preserve tuples** in graph snapshots.
* Aliasing modes: “strict” (verify aliasing) vs “de-aliased” (current default).
* **Characterization testing** mode for complex behavior.
* Multiprocess tracing: cross-process quotas/locking.
* Pluggable serialization (e.g., `jsonpickle`) via CLI.
* Smarter dedup and flakiness detection (auto-run pytest and drop unstable cases).

---

## 🔗 Related tools & approaches

* **Snapshot testing** (`pytest-snapshot`, `snapshottest`, `Syrupy`): great for outputs, but don’t harvest runtime **inputs**.
* **Synthetic test generation**: Pynguin explores inputs for coverage but is not based on your real executions.
* **Automatic type inference**: Monkeytype infers types from runtime; Pytead also focuses on tests and living docs.
* **AOP / tracers** (`aspectlib`, `sys.settrace`): intercept calls but don’t emit ready-to-run pytest modules.
* Related spirit: **Keploy** (focus on external I/O) and various JS “record to unit test” tools.

---

## 📚 Glossary

* **Trace / Entry**: one recorded call (fully-qualified target, args/kwargs, result, timestamp).
* **State-based format**: `pickle` — parameterized tests driven by values/state snapshots.
* **Graph snapshot (`graph-json`)**: deep data capture of object graphs (attributes), independent of concrete classes.
* **Aliasing**: multiple paths pointing to the **same** object in memory; encoded as `{"$ref": N}`.
* **De-alias (for comparison)**: expand/ignore alias markers to compare structures fairly.
* **Shell fallback**: turn nested dict-like structures into lightweight attribute bags to allow `obj.attr` access without real classes.
* **Rehydration (no-init)**: build instances without calling `__init__` and assign attributes.
* **Import roots**: paths inserted into `sys.path` in generated tests so your modules import cleanly.
* **Project root**: detected root (presence of `pyproject.toml` or `.pytead`).
* **FQN** (Fully-Qualified Name): `package.module[.Class].function`.

---

## 📝 License

[MIT](LICENCE)

