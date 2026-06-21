"""
The adapter contracts. This is the seam that makes "free now, paid later"
work: every data source — scraper or paid API — implements the same tiny
interface, so the pipeline never needs to know which is which.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from pipeline.models import RawListing, Property


class SourceAdapter(ABC):
    """Returns listings. One per portal / feed / auction house."""
    name: str = "unnamed"
    cost: str = "free"            # "free" | "paid"

    @abstractmethod
    def fetch(self, criteria: dict) -> list[RawListing]:
        """Return raw listings matching the search criteria."""
        ...


class EnrichmentAdapter(ABC):
    """Attaches supplementary data to a property (EPC, plot, schools...)."""
    name: str = "unnamed"

    @abstractmethod
    def enrich(self, prop: Property) -> dict:
        """Return a dict to merge into prop.enrichment."""
        ...


def load_adapters(specs: list[tuple]) -> list:
    """Instantiate the enabled adapters from (module, class, enabled) specs."""
    import importlib
    loaded = []
    for module_path, class_name, enabled in specs:
        if not enabled:
            continue
        try:
            module = importlib.import_module(module_path)
            loaded.append(getattr(module, class_name)())
        except Exception as e:
            print(f"  ! could not load {class_name}: {e}")
    return loaded
