# tests/test_tracing_graphjson_guardrail.py
from __future__ import annotations

from pathlib import Path
import pytest

from pytead.storage import GraphJsonStorage
from pytead.errors import GraphJsonOrphanRef


def test_graphjson_guardrail_blocks_orphan_ref_in_result(tmp_path: Path):
    """
    Arrange:
      On fabrique une entrée 'graph-json' où `result_graph` contient {'$ref': 3}
      alors qu'aucune ancre `$id: 3` n'existe dans ce même `result_graph`.
      (Le cas réel typique: l'ancre existe dans args_graph, pas dans result_graph.)
    Assert:
      - GraphJsonOrphanRef est levée AVANT toute écriture,
      - le message contient bien le chemin JSONPath et l'id,
      - aucun fichier .gjson n'est créé.
    """
    entry = {
        "func": "dummy.module.identity",
        "args_graph": {"$id": 3, "species": {"base_stats": 42}},  # ancre 3 *ailleurs*
        "kwargs_graph": {},
        "result_graph": {"$ref": 3},  # ← orpheline localement (chemin "$")
    }

    st = GraphJsonStorage()
    out = tmp_path / "should_not_exist.gjson"

    with pytest.raises(GraphJsonOrphanRef) as ei:
        st.dump(entry, out)

    msg = str(ei.value)
    assert "path=$" in msg and "ref=3" in msg, msg
    assert not out.exists(), "Le fichier ne doit pas être écrit lorsqu'un orphan-ref est détecté."

