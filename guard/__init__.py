"""Self-contained standard-library guard for the Fleet Platform deployment lane."""

from .core import DeployGuard, GuardConfig, GuardFailure

__all__ = ["DeployGuard", "GuardConfig", "GuardFailure"]
