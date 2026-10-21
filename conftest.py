"""Публичная версия: тесты, сверяющие боевую конфигурацию владельца, пропускаются.

В приватном репозитории они проверяют живые policy.toml, fleet-projects.toml, расширение
вкладок и проекты флота; здесь этих файлов нет намеренно (tools/public_export.py).
"""
import pytest

NEEDS_OWNER_SETUP = {
    "tests/fleet/test_goals.py::test_repo_file_has_fleet_projects",
    "tests/fleet/test_interventions.py::test_actual_russian_lessons_remain_distinct_failure_classes",
    "tests/fleet/test_interventions.py::test_seed_full_lessons_once_and_keep_drafts_machine_readable",
    "tests/fleet/test_storage_guard.py::test_installed_disk_reserve_does_not_block_small_build_tests",
    "tests/test_claim_trees_config.py::test_fleet_platform_is_retired_from_production_manifest",
    "tests/test_exec.py::test_production_allowed_bins_resolve_in_launch_environment",
    "tests/test_exec.py::test_production_every_exec_root_is_write_root",
    "tests/test_exec.py::test_production_exec_args_unrestricted_subset_of_allowed_bin",
    "tests/test_exec.py::test_production_exec_roots_inside_roots_and_no_shell",
    "tests/test_exec.py::test_production_memory_service_has_complete_agent_permissions",
    "tests/test_exec.py::test_production_policy_has_no_duplicate_permissions_or_directory_as_binary",
    "tests/test_policy.py::test_production_policy_closes_critical_names",
    "tests/test_policy.py::test_production_policy_has_no_global_write",
    "tests/test_policy.py::test_production_task_cwd_in_write_roots",
    "tests/test_policy.py::test_production_write_is_confined_to_write_roots",
}


def pytest_collection_modifyitems(config, items):
    mark = pytest.mark.skip(reason="needs the owner's production configuration (not published)")
    for item in items:
        if item.nodeid in NEEDS_OWNER_SETUP:
            item.add_marker(mark)
