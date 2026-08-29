class FactoryError(Exception):
    """Base class for expected control-plane errors."""

    code = "factory_error"


class ValidationError(FactoryError):
    code = "validation_error"


class NotFoundError(FactoryError):
    code = "not_found"


class StateTransitionError(FactoryError):
    code = "invalid_state_transition"


class IdeaConflictError(FactoryError):
    code = "idea_conflict"


class IdempotencyConflictError(FactoryError):
    code = "idempotency_conflict"


class LeaseConflictError(FactoryError):
    """A worker tried to acknowledge a task without its current fencing token."""

    code = "lease_conflict"


class QueueCapacityError(FactoryError):
    code = "queue_capacity"
