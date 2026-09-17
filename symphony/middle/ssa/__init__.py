"""Persistent SSA construction, destruction, and verification."""

from .construct import construct
from .destruct import destruct
from .verify import verify, SSAVerificationError

__all__ = ["construct", "destruct", "verify", "SSAVerificationError"]
