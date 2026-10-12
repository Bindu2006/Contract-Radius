"""Exceptions raised by ContractRadius. Everything derives from ContractRadiusError."""

from __future__ import annotations


class ContractRadiusError(Exception):
    """Base class for all ContractRadius errors."""


class ConfigError(ContractRadiusError):
    """The .contractradius.yaml file is not valid."""


class SpecError(ContractRadiusError):
    """An OpenAPI spec could not be read or understood."""


class FrontendError(ContractRadiusError):
    """The frontend source directory is missing or unreadable."""
