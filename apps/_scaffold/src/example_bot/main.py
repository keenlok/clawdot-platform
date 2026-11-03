"""Console entry point for the working example."""

from __future__ import annotations

import argparse
import asyncio
import os

from chatcore.log import setup_logging

from example_bot.app import build_app


async def run() -> None:
    app = build_app()
    try:
        await app.run()
    finally:
        await app.platform.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the generic clawdot console example.")
    parser.parse_args()
    setup_logging(os.environ.get("LOG_LEVEL", "WARNING"), [os.environ.get("LLM_API_KEY", "")])
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
