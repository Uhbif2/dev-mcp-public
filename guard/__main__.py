"""python -m guard deploy|watch|install."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from dev_mcp import machine
from .core import DeployGuard, GuardConfig
from .install import install


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fleet Platform deployment guard")
    parser.add_argument("command", choices=("deploy", "watch", "install"))
    parser.add_argument("--live", type=Path, default=Path(os.environ.get(
        "DEV_MCP_LIVE", str(machine.LIVE_HOME or machine.HOME))))
    parser.add_argument("--work", type=Path, default=Path(os.environ.get(
        "DEV_MCP_WORK", str(machine.HUB / "dev-mcp-work"))))
    parser.add_argument("--health-url", default=os.environ.get(
        "DEV_MCP_GUARD_HEALTHZ_URL", machine.GATEWAY_HEALTH_URL))
    args = parser.parse_args(argv)
    if args.command == "install":
        # Explicit bootstrap is a real Scheduler install, not merely a staged
        # immutable copy. register=True verifies a live versioned heartbeat
        # and restores the previous task on failure.
        install(register=True)
        return 0
    guard = DeployGuard(GuardConfig(args.live, args.work, health_url=args.health_url))
    if args.command == "watch":
        guard.watch()
        return 0
    result = guard.deploy()
    print(result)
    return 0 if result in ("deployed", "no_change") else 1


if __name__ == "__main__":
    raise SystemExit(main()) 
