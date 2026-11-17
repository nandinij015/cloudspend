"""Compatibility package for the modules kept at the repository root.

The project documentation and entry points use ``cloudspend.*`` imports,
while the implementation files are stored in the repository root. Extending
the package search path lets those imports work without duplicating modules.
"""
from pathlib import Path

__path__.append(str(Path(__file__).resolve().parent.parent))
