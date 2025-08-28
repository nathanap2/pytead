# tests/test_gen_inlines_external_refs.py

import re
from pytead.gen_tests import render_graph_snapshot_test_body

def _extract_expected_graph_block(src: str) -> str:
    """
    Best-effort: extrait le bloc littéral 'expected_graph = ...' (jusqu'à la
    première ligne vide suivante) pour cibler nos assertions.
    """
    m = re.search(r"expected_graph\s*=\s*(.+?)(?:\n\s*\n|$)", src, flags=re.S)
    return m.group(1) if m else src

def test_generator_inlines_external_refs_in_expected_for_function():
    """
    Le result_graph contient {'$ref': 3} mais l'ancre ($id: 3) n'existe que dans args_graph.
    On veut que le *générateur* inline cette ancre dans expected_graph, afin qu'il soit autonome
    (donc qu'il ne contienne plus de '$ref' orphelin).
    """
    entry = {
        "args_graph": [
            {
                "shared": {"$id": 3, "k": "v", "nums": [1, 2, 3]}
            }
        ],
        "kwargs_graph": {},
        "result_graph": {
            "uses": {"$ref": 3}
        },
    }

    # pas d'annotations → pas de rehydratation de classes dans ce test
    code = render_graph_snapshot_test_body(
        func_name="dummy_func",
        entry=entry,
        param_types={},
        owner_class=None,
    )

    expected_block = _extract_expected_graph_block(code)

    # On veut que le '$ref' ait été inliné → il ne doit PAS rester de '$ref' dans expected_graph
    assert "'$ref'" not in expected_block, (
        "Generator should inline external $ref into expected_graph for functions."
    )

    # Optionnel : on peut aussi vérifier que le contenu de l'ancre a bien été répliqué
    assert "'k': 'v'" in expected_block
    assert "[1, 2, 3]" in expected_block


