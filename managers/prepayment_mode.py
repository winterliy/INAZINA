"""Optional partial-prepayment mode per loan product (M7). Off by default.

Default (and every contract signed before an administrator sets a mode): a partial prepayment is applied to the next installments in
order, so installments are paid ahead and the term effectively shortens. ``reduce_installment``: the term and due dates stay and the
remaining installments are recast on the lower balance. The mode is copied onto each new contract at signing, so changing the product
later never changes a signed loan.
"""
MODES = ("shorten", "reduce_installment")


def set_product_mode(conn, product_code, mode, *, actor, now):
    from managers.compliance_manager import ComplianceManager
    ComplianceManager._require_admin(conn, actor)
    if mode not in MODES or not str(product_code or "").strip() or not conn.execute(
            "SELECT 1 FROM liability WHERE product_code=?", (str(product_code),)).fetchone():
        raise ValueError("invalid prepayment mode")
    conn.execute("INSERT INTO product_prepayment_mode(product_code,mode,updated_by,updated_epoch) VALUES (?,?,?,?) "
                 "ON CONFLICT(product_code) DO UPDATE SET mode=excluded.mode,updated_by=excluded.updated_by,updated_epoch=excluded.updated_epoch",
                 (str(product_code), mode, str(actor), int(now)))


def attach_at_signing(conn, loan_id, product_code, now):
    """Copy an enabled product mode onto the new contract (nothing is written for the default mode)."""
    row = conn.execute("SELECT mode FROM product_prepayment_mode WHERE product_code=?", (str(product_code),)).fetchone()
    if row and row["mode"] == "reduce_installment":
        conn.execute("INSERT INTO contract_prepayment_mode(loan_id,mode,created_epoch) VALUES (?,?,?)", (int(loan_id), "reduce_installment", int(now)))


def reduces_installment(conn, loan_id):
    return bool(conn.execute("SELECT 1 FROM contract_prepayment_mode WHERE loan_id=? AND mode='reduce_installment'", (int(loan_id),)).fetchone())
