import argparse

from .logconf import configure_logger
from .cmd_run import add_run_subparser
from .cmd_gen import add_gen_subparser
from .clean import add_clean_subparser
from .tead_all_in_one import add_tead_subparser


def main() -> None:
    parser = argparse.ArgumentParser(prog="pytead")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Register subcommands
    add_run_subparser(subparsers)
    add_gen_subparser(subparsers)
    add_clean_subparser(subparsers)
    add_tead_subparser(subparsers)

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()

