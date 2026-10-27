"""Booking-system adapters. Import submodules here so they self-register."""

from . import mock, playloc, sportrouter, sportyplus, tivazerra  # noqa: F401
from .base import Provider, all_providers, get_provider, register

__all__ = ["Provider", "all_providers", "get_provider", "register"]
