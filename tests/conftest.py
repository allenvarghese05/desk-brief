"""Shared pytest configuration.

Network access is disabled for every test by default (pytest-socket, see pyproject addopts).
Tests that need real services must be marked ``@pytest.mark.integration``;
they are excluded from PR CI.
Synthetic data lives only under ``tests/`` and must carry a "SYNTHETIC FIXTURE" or
"INJECTED FAULT" header.
"""
