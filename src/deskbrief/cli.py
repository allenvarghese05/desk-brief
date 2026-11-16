"""Command-line entry point. Subcommands are added by later cards (T19)."""

import sys

from deskbrief import __version__


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args in (["--version"], ["-V"]):
        print(__version__)
        return 0
    print("deskbrief: no commands yet (added in T19)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
