# pytead/cli/main.py
import argparse
import sys as _sys
from ..logconf import configure_logger
from .cmd_run import add_run_subparser
from .cmd_gen import add_gen_subparser
from .cmd_tead import add_tead_subparser
from .cmd_types import add_types_subparser
from ..errors import PyteadError

def main() -> None:
    parser = argparse.ArgumentParser(prog="pytead")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_run_subparser(subparsers)
    add_gen_subparser(subparsers)
    add_tead_subparser(subparsers)
    add_types_subparser(subparsers)

    args = parser.parse_args()
    try:
        args.handler(args)
    except PyteadError as exc:
        logger = configure_logger(name="pytead")
        logger.error("%s", exc)
        _sys.exit(1)

