from importlib.metadata import version

# One source of truth: the version in pyproject.toml, via the installed package.
__version__ = version("conference-drawing")
