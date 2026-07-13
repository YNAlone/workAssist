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
