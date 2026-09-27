"""Sphinx configuration for the detectors documentation."""
from __future__ import annotations

import os
import sys
from datetime import datetime

# Make the package importable so autodoc works even without an install.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

project = "detectors"
author = "APS 32-ID"
copyright = f"{datetime.now():%Y}, {author}"

try:
    from detectors import __version__ as release  # noqa: F401
except Exception:
    release = "0.1"
version = release

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "myst_parser",
]

autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
}
autodoc_mock_imports = [
    "caproto", "pvaccess", "PyQt5", "h5py", "pvapy",
    "numpy", "imageio",
]
napoleon_google_docstring = True
napoleon_numpy_docstring = True

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

source_suffix = {".rst": "restructuredtext", ".md": "markdown"}
master_doc = "index"

exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]
templates_path = ["_templates"]

html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]
html_theme_options = {
    "collapse_navigation": False,
    "sticky_navigation": True,
    "navigation_depth": 3,
    "titles_only": False,
}
html_show_sourcelink = False
