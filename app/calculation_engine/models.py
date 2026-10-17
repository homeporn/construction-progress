from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Dict, Optional, Tuple

from app.decimal_utils import ZERO, subtract
from app.models import Discipline


class Status(str, Enum):
    VALID = "VALID"
    WARNING = "WARNING"
    INVALID = "INVALID"
    NO_DATA = "NO_DATA"


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    status: Status = Status.INVALID


@dataclass(frozen=True)
class WorkInput:
    work_item_id: int
    discipline: Discipline
    planned_quantity: Optional[Decimal]
    labor_norm: Optional[Decimal]
    actual: Decimal = ZERO
    confirmed: Decimal = ZERO
    unit: str = "м²"
    plan_unit: str = "м²"
    building_id: Optional[int] = None
    floor_id: Optional[int] = None
    element: str = ""
    work_type: str = ""
    in_plan: bool = True
    project_id: int = 1


@dataclass(frozen=True)
class Progress:
    status: Status
    planned_labor: Optional[Decimal] = None
    actual_earned_labor: Optional[Decimal] = None
    confirmed_earned_labor: Optional[Decimal] = None
    actual_progress: Optional[Decimal] = None
    confirmed_progress: Optional[Decimal] = None
    issues: Tuple[Issue, ...] = ()

    @property
    def remaining_labor(self):
        if self.planned_labor is None or self.actual_earned_labor is None:
            return None
        return max(subtract(self.planned_labor, self.actual_earned_labor), ZERO)


@dataclass(frozen=True)
class ItemProgress(Progress):
    source: Optional[WorkInput] = None
    earned_quantity: Optional[Decimal] = None
    confirmed_earned_quantity: Optional[Decimal] = None
    overrun_quantity: Optional[Decimal] = None

    @property
    def remaining_quantity(self):
        if self.source is None or self.source.planned_quantity is None or self.earned_quantity is None:
            return None
        return max(subtract(self.source.planned_quantity, self.earned_quantity), ZERO)


@dataclass(frozen=True)
class GroupProgress(Progress):
    weights: Dict[int, Decimal] = field(default_factory=dict)


@dataclass(frozen=True)
class Snapshot:
    report_date: str
    knowledge_cutoff: str
    plan_version_id: int
    discipline: Optional[Discipline]
    status: Status
    items: Tuple[ItemProgress, ...] = ()
    total: Optional[GroupProgress] = None
    issues: Tuple[Issue, ...] = ()


@dataclass(frozen=True)
class DateComparison:
    before: Snapshot
    after: Snapshot
    actual_quantity_delta: Dict[int, Decimal]
    confirmed_quantity_delta: Dict[int, Decimal]
    actual_earned_labor_delta: Optional[Decimal]
    confirmed_earned_labor_delta: Optional[Decimal]
