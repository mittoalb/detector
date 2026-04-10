#!/usr/bin/env python3
"""
Setup script for orca-detector package.
"""

from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="orca-detector",
    version="0.1.0",
    author="Alberto Mittone",
    author_email="alberto.mittone@gmail.com",
    description="EPICS areaDetector for Hamamatsu ORCA Fire cameras with Euresys frame grabbers",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/mittoalb/detector",
    packages=find_packages(),
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Science/Research",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Topic :: Scientific/Engineering :: Physics",
        "Topic :: System :: Hardware :: Hardware Drivers",
    ],
    python_requires=">=3.8",
    install_requires=[
        "harvesters>=1.4.0",
        "caproto>=1.3.0",
        "numpy>=1.21.0",
        "imageio>=2.19.0",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0.0",
            "black>=22.0.0",
            "flake8>=4.0.0",
            "mypy>=0.950",
        ],
        "viewer": [
            "pystream>=0.1.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "orca-epics=run_orca_epics:main",
        ],
    },
    include_package_data=True,
    zip_safe=False,
)