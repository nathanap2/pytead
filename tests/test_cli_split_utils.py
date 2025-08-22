from pytead._cli_utils import split_targets_and_cmd


def test_split_with_double_dash():
    t, c = split_targets_and_cmd(["a.b", "--"], ["main.py", "x"])
    assert t == ["a.b"]
    assert c == ["main.py", "x"]


def test_split_omitted_double_dash_moves_py():
    t, c = split_targets_and_cmd(["a.b", "main.py", "arg"], [])
    assert t == ["a.b"]
    assert c == ["main.py", "arg"]


def test_split_strip_leading_double_dash_in_cmd():
    t, c = split_targets_and_cmd(["a.b"], ["--", "main.py", "--flag-like"])
    assert t == ["a.b"]
    assert c == ["main.py", "--flag-like"]


def test_split_multiple_targets_and_py_token():
    t, c = split_targets_and_cmd(["a.b", "c.d", "prog.py", "1", "2"], ["--opt"])
    assert t == ["a.b", "c.d"]
    assert c == ["prog.py", "1", "2", "--opt"]
