"""Public plugin development tools independent of the modeling runtime."""

from importlib.metadata import version

__version__ = version("openstudio-si")

# Contract version evolves independently of the tooling release.
HARNESS_API_VERSION = "0.1.0"
