from pathlib import Path
import json

from pytead.storage import GraphJsonStorage

def test_graphjson_handles_non_string_dict_keys(tmp_path: Path):
    """
    Ensure GraphJsonStorage.dump does not crash when graphs contain dicts
    with non-JSON keys (e.g. tuples). Expect encoding as {"$map": ...}.
    """
    st = GraphJsonStorage()

    entry = {
        "func": "pkg.mod.fn",
        # args_graph[0] is a dict with a tuple key AND a str key (heterogeneous)
        "args_graph": [
            {
                (1, 2): {"x": 1},
                "a": 3,
            }
        ],
        "kwargs_graph": {},
        "result_graph": { (0, 0): "ok" },
    }

    # Write
    out = st.make_path(tmp_path, entry["func"])
    st.dump(entry, out)

    assert out.exists()
    assert out.suffix == ".gjson"

    # Read back
    data = st.load(out)

    # args_graph[0] should be encoded as {"$map": ...} because keys are heterogeneous
    ag0 = data["args_graph"][0]
    assert isinstance(ag0, dict)
    assert "$map" in ag0, f"expected $map encoding, got: {ag0!r}"
    pairs = ag0["$map"]
    assert isinstance(pairs, list) and pairs, "expected non-empty $map list"

    # We should find an entry for key "a" → 3
    assert any(k == "a" and v == 3 for (k, v) in pairs), f"missing ('a', 3) in $map: {pairs!r}"

    # And an entry for tuple key (1,2) → {"x": 1}; tuple key captured as [1, 2] in JSON
    assert any(k == [1, 2] and v == {"x": 1} for (k, v) in pairs), f"missing ([1,2], {{'x':1}}) in $map: {pairs!r}"

    # result_graph should also be JSON-safe
    rg = data["result_graph"]
    assert isinstance(rg, dict)
    # Since result_graph keys are non-primitive only, it's also $map-encoded
    assert "$map" in rg
    assert any(k == [0, 0] and v == "ok" for (k, v) in rg["$map"])

