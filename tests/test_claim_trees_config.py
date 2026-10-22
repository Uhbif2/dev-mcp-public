"""Production manifest retires platform self-editing without changing other projects."""

import pytest
from pathlib import Path
import tomllib


@pytest.mark.production
def test_fleet_platform_is_retired_from_production_manifest():
    config_path = Path(__file__).resolve().parents[1] / "fleet-projects.toml"
    with config_path.open("rb") as config_file:
        document = tomllib.load(config_file)
        projects = document["projects"]

    assert document['platform']['maintenance'] == 'manual'

    assert 'Fleet Platform' not in projects

    for name, config in projects.items():
        assert config.get("claim_trees", False) is not True, (
            f"claim_trees must not be enabled for project {name!r}"
        )
