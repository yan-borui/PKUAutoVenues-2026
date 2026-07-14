from enum import Enum, auto

from .client import EpeUnavailableError, TransportUnavailableError
from .recognize import CaptchaRecognitionTransportError


class NoCandidateError(RuntimeError):
    """No requested reservation candidate is currently available."""


class SlotTakenError(RuntimeError):
    """The selected reservation was taken before order submission."""


class AttemptFailureAction(Enum):
    RETRY_CAPTCHA = auto()
    WAIT_FOR_EPE = auto()
    CONSUME_BUDGET = auto()


def classify_attempt_failure(error: Exception) -> AttemptFailureAction:
    if isinstance(error, CaptchaRecognitionTransportError):
        return AttemptFailureAction.RETRY_CAPTCHA
    if isinstance(error, (EpeUnavailableError, TransportUnavailableError)):
        return AttemptFailureAction.WAIT_FOR_EPE
    return AttemptFailureAction.CONSUME_BUDGET
