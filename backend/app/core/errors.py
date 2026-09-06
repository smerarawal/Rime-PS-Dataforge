"""Typed application errors. A stale result is not an error."""

from __future__ import annotations


class RimeError(Exception):
    """Base error for the orchestration backend."""


class InvalidRequestError(RimeError):
    pass


class ProviderUnavailableError(RimeError):
    pass


class LLMError(RimeError):
    pass


class MalformedLLMOutputError(LLMError):
    pass


class ToolExecutionError(RimeError):
    pass


class ToolTimeoutError(ToolExecutionError):
    pass
