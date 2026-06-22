"""collab package — re-exports the store API so tests/imports work from the
project root (`python3 -m unittest collab.test_collab`) as well as from inside
the package dir."""
from .collab import Store, CollabError, watch, main

__all__ = ["Store", "CollabError", "watch", "main"]
