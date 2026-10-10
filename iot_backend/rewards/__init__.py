"""Session outcome policy for the fixed-stake HRC reward flow."""

from .policy import (
    INCOMPLETE,
    POLICY_VERSION,
    SENSOR_FAULT,
    VALID_NO_VIOLATION,
    VIOLATION_CONFIRMED,
    evaluate_session,
)

__all__ = [
    "POLICY_VERSION",
    "VALID_NO_VIOLATION",
    "VIOLATION_CONFIRMED",
    "SENSOR_FAULT",
    "INCOMPLETE",
    "evaluate_session",
]

from .client import RewardClient, RewardSettlementError, build_reward_client, evidence_hash

__all__ += ["RewardClient", "RewardSettlementError", "build_reward_client", "evidence_hash"]
