"""Processing module for message handling and context preparation."""

from family_assistant.delegation_security import DelegationSecurityLevel

from .protocol import (
    PENDING,
    CollectedOutputs,
    DelegatableService,
    DelegationPermanentError,
    DelegationTaskNotFoundError,
    DelegationTransientError,
    ObservableDelegationService,
    OutputCollectingDelegationService,
    PeekableDelegationService,
    PendingPoll,
    PollableDelegationService,
    RemoteDisposition,
    RemoteObservation,
    RemoteObservationMetadata,
    RemoteSubmission,
)
from .service import ProcessingService
from .types import ChatInteractionResult, ProcessingServiceConfig, RemoteServiceConfig

__all__ = [
    "PENDING",
    "ChatInteractionResult",
    "CollectedOutputs",
    "DelegatableService",
    "DelegationPermanentError",
    "DelegationSecurityLevel",
    "DelegationTaskNotFoundError",
    "DelegationTransientError",
    "ObservableDelegationService",
    "OutputCollectingDelegationService",
    "PeekableDelegationService",
    "PendingPoll",
    "PollableDelegationService",
    "ProcessingService",
    "ProcessingServiceConfig",
    "RemoteDisposition",
    "RemoteObservation",
    "RemoteObservationMetadata",
    "RemoteServiceConfig",
    "RemoteSubmission",
]
