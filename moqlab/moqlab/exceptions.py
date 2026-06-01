"""Domain exceptions for moqlab.
"""


class MoqlabError(Exception):
    """Base for all moqlab errors."""


class ConfigError(MoqlabError):
    """Raised when a topology config is malformed or fails semantic validation."""


class OrchestratorError(MoqlabError):
    """Raised when topology orchestration (Docker/Containernet) fails."""


class RunNotFoundError(MoqlabError):
    """Raised when a referenced run id has no on-disk state."""


class RunAlreadyExistsError(MoqlabError):
    """Raised when `up` is invoked with a run id that is already active."""
