try:
    from importlib.metadata import version

    __version__ = version("DudeWheresMyLogs")
except Exception:  # pragma: no cover - fallback when running from a source tree
    __version__ = "2.3.0"
