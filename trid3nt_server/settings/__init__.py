"""The settings layer: the changeable judgments the system carries, in one keyword dictionary.

``module_input/settings.json`` is the dictionary, in the engines' own format, so ``describe_keywords(module="settings")`` serves it. A value resolves dictionary default -> the user file -> the run, and every read is journaled with the level it was read at.
"""

from .reader import (
    SettingsFileError, bind_run, clear_session_run, dictionary, entry,
    input_dir, listing, refuse_person_only, reset_run, resolve, session_run, setting, split_keywords,
    state_session_run, typed, user_file, user_values, write_user)

__all__ = [
    "SettingsFileError", "bind_run", "clear_session_run", "dictionary", "entry",
    "input_dir", "listing", "refuse_person_only", "reset_run", "resolve", "session_run",
    "setting", "split_keywords", "state_session_run", "typed", "user_file", "user_values",
    "write_user",
]
