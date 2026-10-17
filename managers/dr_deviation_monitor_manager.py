"""Periodic, append-only DR price-deviation observations and alert transitions."""

import math
import time

from functions.dr_pricing import alert_state, reference_metrics


class DrDeviationMonitorManager:
    SAMPLE_SECONDS = 300
    RETENTION_SECONDS = 90 * 86400

    @classmethod
    def sample_conn(cls, conn, now=None):
        """Capture one sample per five-minute bucket and append state transitions."""
        epoch = float(time.time() if now is None else now)
        if not math.isfinite(epoch) or epoch < 0:
            raise ValueError("invalid_monitor_epoch")
        bucket = int(epoch // cls.SAMPLE_SECONDS)
        programs = conn.execute(
            "SELECT d.dr_code,d.underlying_code,d.underlying_per_receipt,d.program_status,"
            "a.status AS dr_status,a.price AS dr_price,a.quote_currency AS dr_currency,"
            "COALESCE(c.share_price,u.price) AS underlying_price,"
            "COALESCE(c.quote_currency,u.quote_currency) AS underlying_currency,"
            "COALESCE(c.status,u.status) AS underlying_status "
            "FROM depositary_receipts d JOIN market_assets a ON a.code=d.dr_code "
            "LEFT JOIN company c ON c.company_code=d.underlying_code "
            "LEFT JOIN market_assets u ON u.code=d.underlying_code "
            "WHERE d.program_status<>'terminated' ORDER BY d.dr_code"
        ).fetchall()
        from managers.currency_manager import CurrencyManager
        sampled = alerts = 0
        for program in programs:
            metrics = {}
            if (program["program_status"] == "active" and program["dr_status"] == "active"
                    and program["underlying_status"] == "active"
                    and program["underlying_price"] is not None):
                try:
                    fx = float(CurrencyManager.quote(
                        conn, str(program["underlying_currency"] or "NWX"),
                        str(program["dr_currency"] or "NWX"),
                    ))
                    metrics = reference_metrics(
                        program["dr_price"], program["underlying_price"],
                        program["underlying_per_receipt"], fx,
                    )
                except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
                    metrics = {}
            state = alert_state(metrics)
            prior = conn.execute(
                "SELECT state FROM dr_deviation_samples WHERE dr_code=? "
                "ORDER BY sample_id DESC LIMIT 1", (program["dr_code"],),
            ).fetchone()
            cursor = conn.execute(
                "INSERT OR IGNORE INTO dr_deviation_samples "
                "(dr_code,sample_bucket,sampled_epoch,dr_price,reference_value,deviation_percent,state) "
                "VALUES (?,?,?,?,?,?,?)",
                (program["dr_code"], bucket, epoch,
                 float(program["dr_price"]) if program["dr_price"] is not None else None,
                 metrics.get("referenceValue"), metrics.get("referencePremiumPercent"), state),
            )
            if cursor.rowcount != 1:
                continue
            sampled += 1
            previous = str(prior["state"]) if prior else "unobserved"
            if state != previous and (state in {"premium", "discount"}
                                      or previous in {"premium", "discount"}):
                conn.execute(
                    "INSERT INTO dr_deviation_alert_events "
                    "(dr_code,from_state,to_state,dr_price,reference_value,deviation_percent,recorded_epoch) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (program["dr_code"], previous, state,
                     float(program["dr_price"]) if program["dr_price"] is not None else None,
                     metrics.get("referenceValue"), metrics.get("referencePremiumPercent"), epoch),
                )
                alerts += 1
        conn.execute(
            "DELETE FROM dr_deviation_samples WHERE sampled_epoch<?",
            (epoch - cls.RETENTION_SECONDS,),
        )
        return {"sampled": sampled, "transitions": alerts, "bucket": bucket}

    @staticmethod
    def history_conn(conn, limit=100, dr_code=None):
        size = max(1, min(int(limit), 500))
        where = " WHERE dr_code=?" if dr_code else ""
        params = (str(dr_code), size) if dr_code else (size,)
        samples = conn.execute(
            "SELECT sample_id,dr_code,sample_bucket,sampled_epoch,dr_price,reference_value,"
            "deviation_percent,state FROM dr_deviation_samples" + where +
            " ORDER BY sampled_epoch DESC,sample_id DESC LIMIT ?", params,
        ).fetchall()
        events = conn.execute(
            "SELECT event_id,dr_code,from_state,to_state,dr_price,reference_value,"
            "deviation_percent,recorded_epoch FROM dr_deviation_alert_events" + where +
            " ORDER BY recorded_epoch DESC,event_id DESC LIMIT ?", params,
        ).fetchall()
        return {"samples": [dict(row) for row in samples],
                "events": [dict(row) for row in events],
                "retentionDays": DrDeviationMonitorManager.RETENTION_SECONDS // 86400}
