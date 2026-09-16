from __future__ import annotations

import argparse
import asyncio
import json

from .workload import DEFAULT_BURSTS


def main() -> None:
    parser = argparse.ArgumentParser(description="Intent-aware scheduling gateway")
    subcommands = parser.add_subparsers(dest="command", required=True)

    serve = subcommands.add_parser("serve", help="run the REST service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")

    demo = subcommands.add_parser("demo", help="run the congested workload comparison")
    demo.add_argument("--bursts", type=int, default=DEFAULT_BURSTS)
    demo.add_argument("--html", default=None)

    validate = subcommands.add_parser(
        "validate-real", help="run compact end-to-end examples with the OpenAI provider"
    )
    validate.add_argument("--timeout", type=float, default=240.0)

    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "intent_scheduler.app:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
        )
    elif args.command == "demo":
        from .simulation import run_demo

        report = run_demo(
            bursts=args.bursts,
            html_path=args.html,
        )
        print(report)
    else:
        from .real_validation import run_real_validation

        result = asyncio.run(
            run_real_validation(per_scenario_timeout_seconds=args.timeout)
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        if not result["validation_passed"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
