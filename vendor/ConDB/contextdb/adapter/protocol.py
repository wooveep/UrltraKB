from abc import ABC, abstractmethod
from typing import Any


class BaseAdapter(ABC):
    @abstractmethod
    def convert(self, source_json: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        pass
