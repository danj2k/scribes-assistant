"""Setup for the shared package, installed by both bot and transcriber containers."""

from setuptools import setup, find_packages

setup(
    name="scribes-shared",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "PyYAML>=6.0",
    ],
    python_requires=">=3.11",
)
