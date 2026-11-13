"""Shared enums. Kept dependency-free so every layer can import them."""

from __future__ import annotations

from enum import IntEnum, StrEnum


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    BLOCKED = "BLOCKED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    CANCELLED = "CANCELLED"


TERMINAL_TASK_STATUSES = frozenset(
    {TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELLED}
)


class ExecutionStatus(StrEnum):
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


class Intent(StrEnum):
    GENERAL = "GENERAL"
    RESEARCH = "RESEARCH"
    CODING = "CODING"
    DEBUGGING = "DEBUGGING"
    FILE = "FILE"
    DATA = "DATA"
    REPORT = "REPORT"
    GOVERNMENT = "GOVERNMENT"
    TRADING = "TRADING"
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    AUDIO = "AUDIO"
    VOICE = "VOICE"
    CONTENT = "CONTENT"
    AUTOMATION = "AUTOMATION"
    SYSTEM = "SYSTEM"
    MULTIMODAL = "MULTIMODAL"


class PermissionLevel(IntEnum):
    READ_ONLY = 0
    SAFE_WRITE = 1
    SYSTEM_WRITE = 2
    EXTERNAL_ACTION = 3


class RiskLevel(IntEnum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2
    CRITICAL = 3


class ProviderKind(StrEnum):
    LLM = "llm"
    IMAGE = "image"
    VIDEO = "video"
    VOICE = "voice"
    EMBEDDING = "embedding"


class ProviderTier(IntEnum):
    """Routing priority: lower runs first (local → free → primary cloud → secondary cloud).

    LOCAL_FALLBACK is for offline stand-ins that are clearly lower quality than the real
    thing (camera motion instead of generated video, robotic speech): they run last, so
    any configured provider of proper quality wins.
    """

    LOCAL = 0
    FREE = 1
    CLOUD_PRIMARY = 2
    CLOUD_SECONDARY = 3
    LOCAL_FALLBACK = 4

    @property
    def is_paid(self) -> bool:
        return self in (ProviderTier.CLOUD_PRIMARY, ProviderTier.CLOUD_SECONDARY)


class ProviderStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_ERROR = "AUTH_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    TEMPORARY_ERROR = "TEMPORARY_ERROR"
    UNAVAILABLE = "UNAVAILABLE"


class ErrorClass(StrEnum):
    TIMEOUT = "TIMEOUT"
    NETWORK = "NETWORK"
    AUTH = "AUTH"
    RATE_LIMIT = "RATE_LIMIT"
    VALIDATION = "VALIDATION"
    FILE = "FILE"
    CODE = "CODE"
    DEPENDENCY = "DEPENDENCY"
    MODEL = "MODEL"
    MEDIA = "MEDIA"
    TOOL = "TOOL"
    UNKNOWN = "UNKNOWN"


class GenerationStatus(StrEnum):
    QUEUED = "QUEUED"
    GENERATING = "GENERATING"
    QC = "QC"
    RETRYING = "RETRYING"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
