"""Run one nightly read-only quick review pulse or show its proposed slices."""
from pathlib import Path
import sys

# Standalone entry point works with py -3 tools/nightly_review.py without PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dev_mcp.fleet.nightly_review import main


if __name__ == "__main__":
    raise SystemExit(main())
