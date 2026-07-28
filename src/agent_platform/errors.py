from __future__ import annotations


class PlatformError(Exception):
    """Base error for the multi-agent platform."""


class AgentNotFoundError(PlatformError):
    pass


class JobNotFoundError(PlatformError):
    pass


class TaskNotFoundError(PlatformError):
    pass


class ValidationError(PlatformError):
    pass


class DispatchError(PlatformError):
    pass


class StaleLeaseError(PlatformError):
    """Raised when a worker attempts to mutate a run after losing its lease."""


class WorkerJobNotFoundError(PlatformError):
    pass
