"""Converter exception hierarchy.

All domain-specific exceptions inherit from ``ConverterError`` so callers
can catch the whole family with a single ``except ConverterError``.
"""


class ConverterError(Exception):
    """Base class for all converter errors."""


class LsfParseError(ConverterError):
    """Raised when an LSF/LSFX binary file cannot be parsed."""


class RegistryError(ConverterError):
    """Raised when an AllSpark registry file (.xcd/.xmd) is invalid or missing."""


class TransformError(ConverterError):
    """Raised during the LsxResource ↔ EffectResource structural transform."""
