# Bump both on every release and add the matching entry at the top of changelog.json
# (tests/test_app.py checks they agree).
__version__ = "0.11.0"
__build__ = 18
APP_NAME = "FortiGate Upgrader"


def version_string() -> str:
    """FortiOS style: v0.10.0 build0017"""
    return f"v{__version__} build{__build__:04d}"
