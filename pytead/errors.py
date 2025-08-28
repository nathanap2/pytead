from __future__ import annotations


class PyteadError(Exception):
    """Base exception for pytead."""


class ConfigError(PyteadError):
    pass


class TargetResolutionError(PyteadError):
    pass


class StorageError(PyteadError):
    pass


class GenerationError(PyteadError):
    pass
    
class OrphanRefInExpected(GenerationError):
    """Raised when the generator sees unresolved {'$ref': N} in the expected
    snapshot *after* donor inlining. The message includes ref ids and JSONPaths."""
    pass
class GraphCaptureRefToUnanchored(PyteadError):
    """
    Emise (optionnellement) par graph_capture lorsque l'on s'apprête à émettre
    {'$ref': N} vers un nœud qui n'a pas d'ancre '$id' dans le graphe.
    Par défaut, on n'élève pas cette exception (politique 'warn').
    """
    pass
    
class GraphJsonOrphanRef(PyteadError):
    """
    Levée par la couche de *tracing/écriture* GraphJson lorsque `result_graph`
    contient un `{'$ref': N}` **sans ancre locale** (`$id`) dans ce même `result_graph`.
    (Même si l'ancre existe dans args/kwargs, les `$id` sont locaux par graphe.)
    Le message inclut au moins le chemin JSONPath (ex: "$.base") et l'identifiant `ref=N`.
    Aucun fichier n'est écrit lorsqu'elle est levée.
    """
