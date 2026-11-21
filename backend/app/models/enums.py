from enum import Enum

from sqlalchemy import Enum as SQLAlchemyEnum


class CrisisType(str, Enum):
    EARTHQUAKE = "EARTHQUAKE"
    FLOOD = "FLOOD"
    CYCLONE = "CYCLONE"


class CrisisStatus(str, Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class SeverityLevel(str, Enum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    SEVERE = "SEVERE"
    EXTREME = "EXTREME"


class DesignationStatus(str, Enum):
    OFFICIAL = "OFFICIAL"
    POTENTIAL = "POTENTIAL"
    UNKNOWN = "UNKNOWN"


class StructuralCondition(str, Enum):
    GOOD = "GOOD"
    FAIR = "FAIR"
    POOR = "POOR"
    UNKNOWN = "UNKNOWN"


class AccessibilityStatus(str, Enum):
    ACCESSIBLE = "ACCESSIBLE"
    LIMITED = "LIMITED"
    INACCESSIBLE = "INACCESSIBLE"
    UNKNOWN = "UNKNOWN"


class VerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    REJECTED = "REJECTED"


class RecommendationRunStatus(str, Enum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


class RouteSafetyStatus(str, Enum):
    SAFE = "SAFE"
    CAUTION = "CAUTION"
    UNSAFE = "UNSAFE"
    UNKNOWN = "UNKNOWN"


class AlertClassification(str, Enum):
    OFFICIAL_WARNING = "OFFICIAL_WARNING"
    OFFICIAL_ADVISORY = "OFFICIAL_ADVISORY"
    NEW_DISASTER_EVENT = "NEW_DISASTER_EVENT"
    WARNING_ESCALATION = "WARNING_ESCALATION"
    WARNING_UPDATE = "WARNING_UPDATE"
    WARNING_CANCELLED = "WARNING_CANCELLED"
    WARNING_EXPIRED = "WARNING_EXPIRED"
    SIGNIFICANT_HAZARD_CHANGE = "SIGNIFICANT_HAZARD_CHANGE"
    DATA_SOURCE_FAILURE = "DATA_SOURCE_FAILURE"


class AlertSourceKind(str, Enum):
    OFFICIAL_GOVERNMENT_WARNING = "OFFICIAL_GOVERNMENT_WARNING"
    OFFICIAL_OBSERVATION = "OFFICIAL_OBSERVATION"
    SYSTEM_GENERATED_ASSESSMENT = "SYSTEM_GENERATED_ASSESSMENT"
    TECHNICAL_NOTIFICATION = "TECHNICAL_NOTIFICATION"


def enum_type(enum_class: type[Enum], name: str) -> SQLAlchemyEnum:
    return SQLAlchemyEnum(
        enum_class,
        name=name,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
    )
