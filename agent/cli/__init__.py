"""The ``loupfeed`` command line. First subcommand: ``doctor``."""

from __future__ import annotations

import argparse
import json as jsonlib
import sys

from .doctor import FAIL, DoctorOptions, render, run_doctor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="loupfeed")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="verify a deployment's Teams + Atlassian wiring")
    doctor.add_argument("--json", action="store_true", help="machine-readable output")
    doctor.add_argument(
        "--write-checks",
        action="store_true",
        help="include checks that create (and clean up) real resources",
    )
    doctor.add_argument(
        "--calendar-mailbox",
        default="",
        help="mailbox for the calendar write check (with --write-checks)",
    )
    doctor.add_argument(
        "--local-base-url",
        default="http://localhost:2024",
        help="local server base URL used when no public URL is configured",
    )

    args = parser.parse_args(argv)
    if args.command == "doctor":
        results = run_doctor(
            DoctorOptions(
                write_checks=args.write_checks,
                calendar_mailbox=args.calendar_mailbox,
                local_base_url=args.local_base_url,
            )
        )
        if args.json:
            print(jsonlib.dumps([r.__dict__ for r in results], indent=2))
        else:
            print(render(results))
        return 1 if any(r.status == FAIL for r in results) else 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
