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
    
class GraphJsonOrphanRef(StorageError):
    """
    Raised by the Graph-JSON storage layer when the *result_graph* contains one or
    more bare references `{"$ref": N}` **without a local `$id` anchor** inside the
    same result graph.

    Context
    -------
    - Anchors (`$id`) are local to a single graph. Even if an anchor with the same
      integer value exists in *args_graph* or *kwargs_graph*, it does **not** make
      the reference valid inside *result_graph* during the write step.
    - When this exception is raised, **no file is written**.

    Typical message (human-readable)
    --------------------------------
    The error message includes at least the JSONPath(s) to the offending ref(s) and
    their id(s), e.g.: "path=$.base ref=3". A compact diagnostic string may also be
    appended (ids/refs counts in donors and result), along with the function FQN.

    Usage patterns
    --------------
    - Backward-compatible: you can raise with a single message string.
    - Structured: you can pass orphan pairs, function name and a compact diagnostic;
      a message will be synthesized.

    Parameters
    ----------
    message : str | None
        Optional explicit message. If omitted, a message is built from `orphans`,
        `func` and `diag`.
    orphans : Sequence[tuple[str, int]] | None
        Pairs of (json_path, ref_id) for each orphan ref found in the result graph.
    func : str | None
        Fully-qualified function name associated with the trace (if available).
    diag : str | None
        Compact human diagnostic (e.g. "args=(ids=0, refs=0) | kwargs=... | result=...").
    """

    def __init__(
        self,
        message: str | None = None,
        *,
        orphans: "Sequence[tuple[str, int]] | None" = None,
        func: str | None = None,
        diag: str | None = None,
    ) -> None:
        # Store structured details for programmatic access by higher layers or tests.
        self.orphans: list[tuple[str, int]] = list(orphans or [])
        self.func: str | None = func
        self.diag: str | None = diag

        # Build a readable message if none was provided.
        if message is None:
            details = "; ".join(f"path={p} ref={rid}" for (p, rid) in self.orphans) or "unknown location(s)"
            suffix = []
            if func:
                suffix.append(f"func={func}")
            if diag:
                suffix.append(diag)
            extra = f" ({'; '.join(suffix)})" if suffix else ""
            message = f"graph-json guardrail: orphan $ref in result_graph: {details}{extra}"

        super().__init__(message)

