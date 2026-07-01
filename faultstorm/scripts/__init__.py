"""
Bundled scripts for faultstorm.

Provides helper functions to locate scripts shipped with the package,
so that consuming projects can install them into Docker images or
deploy them to target nodes.
"""

import os

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))


def get_script_path(name: str) -> str:
    """Get the absolute path to a bundled script.

    Args:
        name: Script filename (e.g. ``process_freezer.sh``)

    Returns:
        Absolute path to the script file

    Raises:
        FileNotFoundError: If the script does not exist
    """
    path = os.path.join(_SCRIPTS_DIR, name)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Script '{name}' not found in {_SCRIPTS_DIR}")
    return path


def get_supervisor_conf_path(name: str) -> str:
    """Get the absolute path to a bundled supervisor config.

    Args:
        name: Config filename (e.g. ``process_freezer.supervisor.conf``)

    Returns:
        Absolute path to the config file

    Raises:
        FileNotFoundError: If the config does not exist
    """
    path = os.path.join(_SCRIPTS_DIR, name)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Supervisor config '{name}' not found in {_SCRIPTS_DIR}")
    return path
