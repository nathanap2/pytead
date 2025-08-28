import pytest
from pytead.testkit import graph_to_data, rehydrate_from_graph, assert_match_graph_snapshot

class Container:
    # Aucune annotation de type : on veut quand même pouvoir accéder à self.a.m
    def value_m(self):
        return self.a.m

def test_rehydrate_without_annotations_allows_dot_access_on_nested_objects():
    """
    Avant le patch 'fallback shell':
      - rehydrate_from_graph laisse self.a comme un dict → self.a.m lève AttributeError.
    Après le patch:
      - self.a devient une coquille (p.ex. SimpleNamespace) → self.a.m fonctionne.
    """
    # Graphe capturé (format graph-json) pour l'instance `self` :
    args_graph = [
        {"a": {"m": 123}}  # self.a.m == 123
    ]

    # Réhydratation de `self` (sans __init__, sans annotations)
    self_instance = rehydrate_from_graph(graph_to_data(args_graph[0]), Container)

    # 🔴 Avant le patch: la ligne suivante lève AttributeError (self.a est un dict)
    # 🟢 Après le patch: passe et renvoie 123
    result = self_instance.value_m()

    # Vérifie le snapshot (scalaires OK tels quels)
    assert_match_graph_snapshot(result, 123)
