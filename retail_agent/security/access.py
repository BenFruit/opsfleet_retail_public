"""Trusted manager-to-brand entitlements.

Loaded once at startup from local configuration (a stand-in for an identity and
entitlement service) and passed explicitly to the components that need it.
Never built from chat input or model output.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType


class Entitlements:
    def __init__(self, mapping: Mapping[str, object]):
        if not isinstance(mapping, Mapping) or not mapping:
            raise ValueError("Access config must map manager IDs to lists of brands")
        brands_by_manager = {}
        for manager, brands in mapping.items():
            if (
                not isinstance(manager, str)
                or not manager.strip()
                or not isinstance(brands, (list, tuple, set, frozenset))
            ):
                raise ValueError("Invalid manager access config")
            if any(not isinstance(brand, str) or not brand.strip() for brand in brands):
                raise ValueError("Brand names must be nonempty strings")
            brands_by_manager[manager] = frozenset(brands)
        self._brands = MappingProxyType(brands_by_manager)

    @classmethod
    def from_file(cls, path: str | Path) -> "Entitlements":
        return cls(json.loads(Path(path).read_text(encoding="utf-8-sig")))

    def brands_for(self, manager_id: str) -> frozenset[str]:
        """Authorized brands, or PermissionError for unknown/empty managers (fail closed)."""
        brands = self._brands.get(manager_id)
        if not brands:
            raise PermissionError("No authorized brands for this manager")
        return brands
