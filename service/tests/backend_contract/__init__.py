"""Backend contract tests: every StorageBackend implementation must pass them.

To add a backend, register a factory in `registry.py`. Tests that need a
capability the backend lacks (versioning, WORM, archive) skip themselves.
"""
