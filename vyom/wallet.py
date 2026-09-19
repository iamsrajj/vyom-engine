"""wallet -- the ONLY module that should ever write to wallet_transactions
or read/update users.wallet_balance_paise. Centralizing this is what
guarantees the two never drift apart (see WalletTransaction's docstring in
models.py).

Every function here FLUSHES but does NOT COMMIT -- callers are expected to
call this from inside a transaction that also writes whatever caused the
wallet change (a coupon redemption row, a farm-plan purchase row), and
commit once at the end. This mirrors the ordinary-write-first rule used
elsewhere in this app's more careful flows (e.g. zonal_stats.py's
per-index isolation before its single closing commit) -- a wallet credit
must never be visible without the order it came from, or vice versa.
"""
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from vyom.models import User, WalletTransaction


class InsufficientWalletBalance(Exception):
    pass


def get_balance_paise(db: Session, user_id: UUID) -> int:
    user = db.get(User, user_id)
    if user is None:
        raise ValueError(f"No such user: {user_id}")
    return user.wallet_balance_paise


def credit(db: Session, *, user_id: UUID, amount_paise: int, reason: str,
           reference_id: UUID | None = None) -> WalletTransaction:
    """amount_paise must be positive. Use `debit` for spends, so the sign
    convention in wallet_transactions.amount_paise stays self-consistent no
    matter which call site is used."""
    if amount_paise <= 0:
        raise ValueError("credit() requires a positive amount_paise")
    user = db.get(User, user_id)
    if user is None:
        raise ValueError(f"No such user: {user_id}")

    user.wallet_balance_paise += amount_paise
    row = WalletTransaction(
        user_id=user_id, amount_paise=amount_paise, reason=reason,
        reference_id=reference_id, balance_after_paise=user.wallet_balance_paise,
    )
    db.add(row)
    db.flush()
    return row


def debit(db: Session, *, user_id: UUID, amount_paise: int, reason: str,
          reference_id: UUID | None = None) -> WalletTransaction:
    """amount_paise must be positive (the debit amount, not pre-negated).
    Raises InsufficientWalletBalance rather than allowing the balance to go
    negative -- callers applying wallet balance at checkout should clamp
    the amount they attempt to debit to min(requested, get_balance_paise())
    themselves before calling this, so this exception should only ever fire
    on a genuine race (e.g. two concurrent checkouts)."""
    if amount_paise <= 0:
        raise ValueError("debit() requires a positive amount_paise")
    user = db.get(User, user_id)
    if user is None:
        raise ValueError(f"No such user: {user_id}")
    if user.wallet_balance_paise < amount_paise:
        raise InsufficientWalletBalance(
            f"User {user_id} has {user.wallet_balance_paise} paise, "
            f"tried to debit {amount_paise}")

    user.wallet_balance_paise -= amount_paise
    row = WalletTransaction(
        user_id=user_id, amount_paise=-amount_paise, reason=reason,
        reference_id=reference_id, balance_after_paise=user.wallet_balance_paise,
    )
    db.add(row)
    db.flush()
    return row


def list_transactions(db: Session, user_id: UUID, limit: int = 50) -> list[WalletTransaction]:
    return list(db.execute(
        select(WalletTransaction)
        .where(WalletTransaction.user_id == user_id)
        .order_by(WalletTransaction.created_at.desc())
        .limit(limit)
    ).scalars())
