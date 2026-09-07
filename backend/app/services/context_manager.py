"""Parameter merging. Gemini may propose values; this module decides the merge."""

from __future__ import annotations

from typing import Any


class ContextManager:
    """Preserves conversation parameters across follow-up turns.

    Follow-up: proposed non-null keys overlay the current parameters.
    New request: proposed parameters replace the current set.
    """

    def merge(
        self,
        current_parameters: dict[str, Any],
        proposed_parameters: dict[str, Any],
        *,
        is_follow_up: bool,
    ) -> dict[str, Any]:
        proposed = self._clean(proposed_parameters)
        current = dict(current_parameters or {})

        if is_follow_up:
            merged = dict(current)
            merged.update(proposed)
            return merged

        if proposed:
            return proposed
        return current

    def _clean(self, parameters: dict[str, Any] | None) -> dict[str, Any]:
        cleaned: dict[str, Any] = {}
        for key, value in (parameters or {}).items():
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            cleaned[key] = value
        return cleaned
