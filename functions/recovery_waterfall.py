"""Internal simulation of creditor-priority recovery allocations.

Amounts are nonnegative integer minor units of one currency. Equal-priority
claims share available proceeds pro rata, with largest remainders assigned
by claim ID for a reproducible, exactly conserved result. This calculator
never posts a payment or determines legal priority.
"""
from dataclasses import dataclass
from itertools import groupby


@dataclass(frozen=True)
class RecoveryAllocation:
    allocations: dict[str, int]
    shortfalls: dict[str, int]
    unallocated: int


def allocate_recovery(proceeds, claims):
    """Allocate proceeds to (unique claim ID, positive rank, amount) tuples."""
    def integer(value, minimum):
        return type(value) is int and value >= minimum

    if not integer(proceeds, 0):
        raise ValueError("proceeds must be nonnegative integer minor units")
    claims = list(claims)
    ids = set()
    for claim_id, rank, amount in claims:
        if (not isinstance(claim_id, str) or not claim_id or claim_id in ids
                or not integer(rank, 1) or not integer(amount, 0)):
            raise ValueError("invalid or duplicate claim")
        ids.add(claim_id)
    allocations = {claim_id: 0 for claim_id, _, _ in claims}
    remaining = proceeds
    for _, group in groupby(sorted(claims, key=lambda c: (c[1], c[0])), key=lambda c: c[1]):
        group = list(group)
        total = sum(amount for _, _, amount in group)
        available = min(remaining, total)
        if total:
            remainders = []
            for claim_id, _, amount in group:
                allocated, remainder = divmod(available * amount, total)
                allocations[claim_id] = allocated
                remainders.append((-remainder, claim_id))
            extra = available - sum(allocations[claim_id] for claim_id, _, _ in group)
            for _, claim_id in sorted(remainders)[:extra]:
                allocations[claim_id] += 1
        remaining -= available
    shortfalls = {claim_id: amount - allocations[claim_id] for claim_id, _, amount in claims}
    return RecoveryAllocation(allocations, shortfalls, remaining)


def preview_charged_off_loan(conn, *, loan_id, proceeds, bank_rank, other_claims):
    """Preview using the bank's recorded memo balance and assumed other claims.

    Does not change balances, post journals, or establish other claims' validity.
    An absent currency or canonical balance must be resolved before simulation.
    """
    if type(loan_id) is not int or loan_id <= 0:
        raise ValueError("invalid loan id")
    row = conn.execute(
        "SELECT status, quote_currency, charged_off_balance_minor_units "
        "FROM accounts.loans WHERE loan_id=?", (loan_id,)
    ).fetchone()
    if not row or row[0] != "charged_off" or not row[1] or type(row[2]) is not int or row[2] < 0:
        raise ValueError("charged-off loan with canonical balance and currency required")
    result = allocate_recovery(proceeds, [("bank", bank_rank, row[2]), *other_claims])
    return {"loanId": loan_id, "currencyCode": str(row[1]), "bankClaimMinorUnits": row[2],
            "allocations": result.allocations, "shortfalls": result.shortfalls,
            "unallocatedMinorUnits": result.unallocated, "simulationOnly": True,
            "otherClaimsAreAssumptions": True}
