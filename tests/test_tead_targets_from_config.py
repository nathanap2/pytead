from types import SimpleNamespace
from pathlib import Path


def test_tead_targets_fallback_from_config(tmp_path, monkeypatch, caplog):
    """
    When the user runs `pytead tead -- main.py`, argparse can leave `targets=['main.py']`
    and `cmd=[]`. Our split then moves the .py to cmd, leaving targets empty.
    This test checks that TEAD falls back to [tead].targets from .pytead/default_config.toml.
    """
    monkeypatch.chdir(tmp_path)

    # --- Write project-local config under .pytead/ ---
    (tmp_path / ".pytead").mkdir()
    (tmp_path / ".pytead" / "default_config.toml").write_text(
        "\n".join(
            [
                "[defaults]",
                "limit = 1",
                'storage_dir = "call_logs"',
                'format = "pickle"',
                "",
                "[tead]",
                'targets = ["ioutils.render_json", "ioutils.load_team_description"]',
            ]
        ),
        encoding="utf-8",
    )

    # --- Minimal package with the targeted functions ---
    (tmp_path / "ioutils").mkdir()
    (tmp_path / "ioutils" / "__init__.py").write_text(
        "\n".join(
            [
                "def render_json(x):",
                "    # trivial pass-through",
                "    return x",
                "",
                "def load_team_description(name):",
                "    return {'team': name, 'size': 3}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # --- A script that calls both functions once ---
    (tmp_path / "main.py").write_text(
        "\n".join(
            [
                "from ioutils import render_json, load_team_description",
                "render_json({'a': 1})",
                "load_team_description('Blue')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # Import after files exist
    from pytead.tead_all_in_one import run as tead_run

    # Simulate `pytead tead -- main.py` as parsed args
    args = SimpleNamespace(
        targets=["main.py"],
        cmd=[],
        # intentionally omit other fields; config + last-resort defaults apply
    )

    caplog.set_level("INFO")
    tead_run(args)

    # --- Fallback message is logged ---
    assert any(
        "falling back to config targets" in rec.getMessage().lower()
        for rec in caplog.records
    ), "Expected a fallback to [tead].targets but did not see it in logs."

    # --- Traces were written for both targeted functions ---
    calls_dir = tmp_path / "call_logs"
    assert calls_dir.exists(), "calls_dir was not created"
    assert list(
        calls_dir.glob("ioutils_render_json__*.pkl")
    ), "render_json trace not found"
    assert list(
        calls_dir.glob("ioutils_load_team_description__*.pkl")
    ), "load_team_description trace not found"

    # --- Tests were generated (single file or per-function dir) ---
    single_file = tmp_path / "tests" / "test_pytead_generated.py"
    per_func_dir = tmp_path / "tests" / "generated"
    assert single_file.exists() or per_func_dir.exists(), "No generated tests found"
