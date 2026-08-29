from enum import StrEnum

from .errors import StateTransitionError


class IdeaState(StrEnum):
    CANDIDATE = "candidate"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    PROCESSING = "processing"
    READY = "ready"


class JobState(StrEnum):
    REVIEW_PENDING = "review_pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    RIGHTS_PENDING = "rights_pending"
    RIGHTS_FAILED = "rights_failed"
    PRODUCTION_PENDING = "production_pending"
    QC_PENDING = "qc_pending"
    QC_FAILED = "qc_failed"
    READY = "ready"


ALLOWED_JOB_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.REVIEW_PENDING: frozenset({JobState.APPROVED, JobState.REJECTED}),
    JobState.APPROVED: frozenset({JobState.RIGHTS_PENDING}),
    JobState.REJECTED: frozenset(),
    JobState.RIGHTS_PENDING: frozenset(
        {JobState.PRODUCTION_PENDING, JobState.RIGHTS_FAILED}
    ),
    JobState.RIGHTS_FAILED: frozenset({JobState.RIGHTS_PENDING}),
    JobState.PRODUCTION_PENDING: frozenset({JobState.QC_PENDING}),
    JobState.QC_PENDING: frozenset({JobState.READY, JobState.QC_FAILED}),
    JobState.QC_FAILED: frozenset({JobState.QC_PENDING}),
    JobState.READY: frozenset(),
}


APPROVED_OR_LATER = frozenset(
    {
        JobState.APPROVED,
        JobState.RIGHTS_PENDING,
        JobState.RIGHTS_FAILED,
        JobState.PRODUCTION_PENDING,
        JobState.QC_PENDING,
        JobState.QC_FAILED,
        JobState.READY,
    }
)


def ensure_job_transition(current: str, target: JobState) -> None:
    current_state = JobState(current)
    if target not in ALLOWED_JOB_TRANSITIONS[current_state]:
        raise StateTransitionError(
            f"job cannot move from {current_state.value!r} to {target.value!r}"
        )

