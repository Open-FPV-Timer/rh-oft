"""Test stub of the RotorHazard UI classes rh_oft builds its settings panel from."""

from dataclasses import dataclass
from enum import Enum
from typing import Any, List


class UIFieldType(Enum):
    TEXT = "text"
    SELECT = "select"


@dataclass
class UIFieldSelectOption:
    value: str
    label: str


@dataclass
class UIField:
    name: str
    label: str
    field_type: Any = UIFieldType.TEXT
    value: Any = None
    desc: str = None
    placeholder: str = None
    options: List[UIFieldSelectOption] = None
