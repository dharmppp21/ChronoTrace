"""Single source of truth for the package version.

Hatchling reads ``__version__`` from this file at build time (see
``[tool.hatch.version]`` in pyproject.toml), so the installed distribution and
the running package can never disagree.

Why a literal here rather than deriving the version from git tags (hatch-vcs):
day 45 adds a release check asserting that the pushed tag matches the package
version. That check can only catch a real mistake while the two are independent
sources. Derive one from the other and the assertion becomes a tautology that
passes unconditionally.
"""

__version__ = "0.1.0.dev0"
