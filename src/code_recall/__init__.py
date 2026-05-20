"""code-recall: Semantic search across local coding-agent sessions."""

__version__ = "0.2.4"


def has_semantic() -> bool:
    """Check if semantic search dependencies are available."""
    try:
        import fastembed  # noqa: F401
        import sqlite_vec  # noqa: F401

        return True
    except ImportError:
        return False


def has_tui() -> bool:
    """Check if TUI dependencies are available."""
    try:
        import textual  # noqa: F401

        return True
    except ImportError:
        return False
