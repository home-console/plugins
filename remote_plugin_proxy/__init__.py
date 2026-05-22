"""
Remote plugin proxy plugin (folder-based).

This package intentionally uses a folder layout so it can be split into a
standalone repository with its own tests.
"""

from .plugin import RemotePluginProxy

__all__ = ["RemotePluginProxy"]

