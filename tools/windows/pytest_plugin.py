"""Keep macOS-specific launcher tests visible as skips on a Windows build."""
import sys
import pytest

MAC_LAUNCH_TESTS = {
    'test_real_launcher_preserves_working_data_and_repeated_test_database',
    'test_check_mode_never_creates_venv_or_installs_packages',
    'test_check_prepare_conflict_is_rejected_before_any_python_or_network',
    'test_default_launch_without_arguments_and_without_automatic_pip',
    'test_launcher_keeps_unicode_path_as_one_argument',
    'test_only_explicit_prepare_can_invoke_package_installation',
    'test_unprepared_launch_does_not_silently_create_environment',
}


def pytest_collection_modifyitems(items):
    if sys.platform != 'win32': return
    for item in items:
        if item.name in MAC_LAUNCH_TESTS:
            item.add_marker(pytest.mark.skip(reason='macOS Bash start.command; covered by the Linux/macOS regression'))
        if item.name == 'test_acceptance_database_cannot_alias_working_database[symlink]' or item.name == 'test_destination_is_exclusive_and_protected':
            item.add_marker(pytest.mark.skip(reason='Symlink creation may require Windows Developer Mode; no admin required for the application'))
