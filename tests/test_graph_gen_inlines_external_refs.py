from __future__ import annotations

from pathlib import Path
import textwrap

from pytead.gen_tests import write_tests_per_func


def _write(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(s).strip() + "\n", encoding="utf-8")


def test_graph_generation_inlines_external_refs(tmp_path: Path):
    """
    Repro: on forge une trace graph-json où le résultat contient {'$ref': 3}
    vers un ancrage ('$id': 3) qui n'existe QUE dans les inputs (args/kwargs).

    Attendu: le générateur doit **inliner** cet external-ref dans expected_graph,
    de sorte qu'il ne reste PLUS AUCUN `{'$ref': N}` dans le bloc attendu.

    État actuel (bug): le fichier généré garde le `{'$ref': 3}` non résolu.
    """

    # 1) Module minimal importable pendant la génération
    mod = tmp_path / "mymod.py"
    _write(
        mod,
        """
        def f(x):
            # Le contenu importe peu : la génération n'exécute pas la fonction.
            return {"uses": x}
        """,
    )

    # 2) Trace synthétique (graph-json) : ancrage côté args, $ref côté result
    entry = {
        "func": "mymod.f",
        "args_graph": [
            {"$id": 3, "value": 42}   # <-- donneur d'ancrage
        ],
        "kwargs_graph": {},
        "result_graph": {
            "uses": {"$ref": 3}       # <-- réf *externe* à inliner dans expected_graph
        },
    }
    entries_by_func = {"mymod.f": [entry]}

    # 3) Génération "un fichier par fonction"
    out_dir = tmp_path / "generated"
    write_tests_per_func(
        entries_by_func,
        out_dir,
        import_roots=[str(tmp_path)],  # pour que `mymod` soit résolu à la génération
    )

    # 4) Vérification du code généré : il ne doit plus rester de "$ref" dans expected_graph
    test_file = next((out_dir.glob("test_mymod_f_snapshots.py")))
    src = test_file.read_text(encoding="utf-8")

    # Heuristique simple mais robuste : dans ce scénario, seuls les blocs expected_graph
    # pourraient contenir "$ref" (nos args/kwargs n'en ont pas). Si on voit "$ref",
    # c'est que l'inlining n'a pas eu lieu.
    assert "'$ref'" not in src, (
        "Generator should inline external $ref into expected_graph for functions."
    )

