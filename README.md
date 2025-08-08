Pytead

Pytead is a tool that automatically captures real Python function calls in your application and generates pytest unit tests based on those execution traces.

📦 Installation

clone the repo and then install it as an egg (for the moment this is the easiest way ...)

pip install -e .

This provides two console scripts:

TBD: instruments your code to record function calls

TBD: generates pytest test modules from recorded traces

? How It Works

Instrumentation:

Use the Pytead CLI or the @trace2test decorator to wrap a target function. Each invocation of that function is intercepted.

Capture:

While using the scanned program, when we call a function we serialize (arguments, keyword arguments, return value, timestamp) into .pkl files in a designated storage_dir.

Test Generation:

The gentests CLI reads all .pkl files in the calls directory and renders a pytest-compatible test file. Each recorded call becomes one assert.


🎛️ Usage

1. Instrumentation via CLI

Instead of manually importing the decorator, run:

trace2test \
  --limit 5 \
  --storage-dir call_logs \
  mymodule.my_function \
  -- python3 main.py

--limit (int): maximum number of calls to record per function (default: 10).

--storage-dir (str): directory for storing trace files (default: call_logs).

You will see output like:

✓ Instrumentation on mymodule.my_function
[trace2test] cwd = '/path/to/project', storage_dir = 'call_logs'
[trace2test] log written to '/path/to/project/call_logs/mymodule_my_function__abc123.pkl'

2. Generating Tests

gentests \
  --calls-dir call_logs \
  --output tests/test_generated.py

--calls-dir: where to read .pkl traces (default: call_logs).

--output: path to write the generated pytest file (default: tests/test_pytead_generated.py).

Sample generated file:

import pytest
from mymodule import my_function

def test_my_function_1():
    assert my_function(2, 3) == 6

Then run:

pytest tests


🔭 .. To be continued

I started the project 2 days ago, please let me some time to improve it :D

Next steps : 

Support method calls and function with side effects

Deduplicate similar traces via input hashing or similarity algorithms.

Enrich traces with execution metadata (duration, argument hashes, exit codes).

Switch from pickle to jsonpickle or a custom JSON schema to preserve complex types (tuples, datetimes).

Full-featured CLI: pytead run, pytead gen-tests, pytead clean, etc.

Enrich function documentation with real examples

Chose these real examples supposed to enrich doc in order to optimize LLMs understanding of the project directly from reading the doc

🔗 Related Tools & Approaches

- Snapshot Testing (pytest-snapshot, snapshottest, Syrupy): captures function outputs via explicit snapshot.assert_match(...) calls in tests, but does not record actual inputs or run in production.

- Synthetic Test Generation (Pynguin): explores the input space to maximize coverage, but doesn't leverage real runtime execution data.

- AOP / Tracers (aspectlib, sys.settrace): intercept function calls and returns on-the-fly in production, but do not provide an automatic mechanism to generate standalone test files.

Equivalent Projects in Other Languages or Contexts

In node.js, from what i've read, unit-test-recorder (UTR) seems to be similar to this project (but i'm not familiar with js)

Keploy seems to stem from the same idea, but, from what I understand, focuses on the i/o between the program and external dependancies.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

