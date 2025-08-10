import logging


def configure_logger(level: int = logging.INFO, name: str = "pytead") -> logging.Logger:
    root = logging.getLogger("pytead")
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[pytead] %(levelname)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(level)
    return logging.getLogger(name)
