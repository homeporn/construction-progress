from enum import Enum


class Discipline(str, Enum):
    DESIGN = "DESIGN"
    CONSTRUCTION = "CONSTRUCTION"


class PlanStatus(str, Enum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
