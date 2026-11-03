"""Typed errors shared by the control plane and adapters."""


class HubError(Exception):
    """Base error for AgentMesh OS."""

    code = "hub_error"

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class ConfigError(HubError):
    code = "config_error"


class ValidationError(HubError):
    code = "validation_error"


class NotFoundError(HubError):
    code = "not_found"


class ConflictError(HubError):
    code = "conflict"


class PermissionDeniedError(HubError):
    code = "permission_denied"


class BudgetExceededError(HubError):
    code = "budget_exceeded"


class AdapterError(HubError):
    code = "adapter_error"


class AdapterTimeoutError(AdapterError):
    code = "adapter_timeout"


class ResultInvalidError(HubError):
    code = "result_invalid"


class VerificationError(HubError):
    code = "verification_error"


class GitOperationError(HubError):
    code = "git_operation_error"


class RecoveryError(HubError):
    code = "recovery_error"


class ApprovalRequiredError(HubError):
    code = "approval_required"


class StateTransitionError(HubError):
    code = "state_transition_error"
