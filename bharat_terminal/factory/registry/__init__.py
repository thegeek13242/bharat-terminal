"""Versioned feature registry — the only vocabulary the DSL can reference."""
from __future__ import annotations

from bharat_terminal.factory.registry.features import (
    FeatureSpec,
    REGISTRY,
    get_feature,
    parse_ref,
)

__all__ = ["FeatureSpec", "REGISTRY", "get_feature", "parse_ref"]
