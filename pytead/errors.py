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
