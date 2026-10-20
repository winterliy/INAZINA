"""Periodic, append-only DR price-deviation observations and alert transitions."""

from functions.dr_pricing import alert_state, reference_metrics
from managers.bank_journal import BankJournal


class DrDeviationMonitorManager:
    SAMPLE_SECONDS = 300
    RETENTION_SECONDS = 90 * 86400

    @classmethod
    def sample_conn(cls, conn, now=None):
        """Capture five-minute observations; confirm alert transitions across two buckets."""
        try:
            epoch = BankJournal.validate_epoch(now)
        except ValueError as exc:
            raise ValueError("invalid_monitor_epoch") from exc
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
        from functions.money import currency_precision, to_minor_units
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
            places = currency_precision(conn, str(program["dr_currency"] or "NWX"))
            dr_price_minor = (to_minor_units(program["dr_price"], places)
                              if program["dr_price"] is not None else None)
            reference_minor = (to_minor_units(metrics["referenceValue"], places)
                               if metrics.get("referenceValue") is not None else None)
            prior = conn.execute(
                "SELECT state,sample_bucket FROM dr_deviation_samples WHERE dr_code=? "
                "ORDER BY sample_id DESC LIMIT 1", (program["dr_code"],),
            ).fetchone()
            last_event = conn.execute(
                "SELECT to_state FROM dr_deviation_alert_events WHERE dr_code=? "
                "ORDER BY event_id DESC LIMIT 1", (program["dr_code"],),
            ).fetchone()
            cursor = conn.execute(
                "INSERT OR IGNORE INTO dr_deviation_samples "
                "(dr_code,sample_bucket,sampled_epoch,dr_price,dr_price_minor_units,"
                "reference_value,reference_value_minor_units,deviation_percent,state) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (program["dr_code"], bucket, epoch,
                 float(program["dr_price"]) if program["dr_price"] is not None else None,
                 dr_price_minor, metrics.get("referenceValue"), reference_minor,
                 metrics.get("referencePremiumPercent"), state),
            )
            if cursor.rowcount != 1:
                continue
            sampled += 1
            stable_state = (str(last_event["to_state"])
                            if last_event and last_event["to_state"] in {"premium", "discount"}
                            else "normal")
            consecutive_match = (
                prior is not None and int(prior["sample_bucket"]) == bucket-1
                and str(prior["state"]) == state
            )
            confirmed_state = stable_state
            if state != "unavailable" and state != stable_state and consecutive_match:
                confirmed_state = state
            if confirmed_state != stable_state and (
                    confirmed_state in {"premium", "discount"}
                    or stable_state in {"premium", "discount"}):
                conn.execute(
                    "INSERT INTO dr_deviation_alert_events "
                    "(dr_code,from_state,to_state,dr_price,dr_price_minor_units,"
                    "reference_value,reference_value_minor_units,deviation_percent,recorded_epoch) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (program["dr_code"], stable_state, confirmed_state,
                     float(program["dr_price"]) if program["dr_price"] is not None else None,
                     dr_price_minor, metrics.get("referenceValue"), reference_minor,
                     metrics.get("referencePremiumPercent"), epoch),
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
            "SELECT sample_id,dr_code,sample_bucket,sampled_epoch,dr_price,"
            "dr_price_minor_units,reference_value,reference_value_minor_units,"
            "deviation_percent,state FROM dr_deviation_samples" + where +
            " ORDER BY sampled_epoch DESC,sample_id DESC LIMIT ?", params,
        ).fetchall()
        events = conn.execute(
            "SELECT event_id,dr_code,from_state,to_state,dr_price,dr_price_minor_units,"
            "reference_value,reference_value_minor_units,"
            "deviation_percent,recorded_epoch FROM dr_deviation_alert_events" + where +
            " ORDER BY recorded_epoch DESC,event_id DESC LIMIT ?", params,
        ).fetchall()
        return {"samples": [dict(row) for row in samples],
                "events": [dict(row) for row in events],
                "retentionDays": DrDeviationMonitorManager.RETENTION_SECONDS // 86400}
