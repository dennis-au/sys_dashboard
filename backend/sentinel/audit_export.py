"""Print redacted internal audit events as NDJSON for local diagnostic collection."""

from __future__ import annotations

import argparse

from .audit import ndjson_events


def main() -> None:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    output = ndjson_events(args.limit)
    if output:
        print(output)


if __name__ == "__main__":
    main()
