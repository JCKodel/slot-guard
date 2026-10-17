"""Minimal non-credential environment passed to test subprocesses."""
import os


def test_environment():
    # Read specific runtime locations, never copy the installer's entire environment.
    result = {}
    for name in ('PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'SYSTEMROOT'):
        value = os.getenv(name)
        if value is not None:
            result[name] = value
    return result
