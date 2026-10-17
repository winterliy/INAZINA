"""Atomic simulator conversion between DRs and their underlying common shares."""

from decimal import Decimal
import json
import math
import sqlite3


class DepositaryReceiptManager:
    def __init__(self, db):
        self.db = db

    def create_voting_record_snapshot(self, underlying_code, title, *, now=None):
        """Freeze common-share and DR voting entitlements at the current simulator epoch."""
        from managers.bank_journal import BankJournal
        code, label = str(underlying_code or "").strip(), str(title or "").strip()
        if not code or not label:
            return False, "❌ 기초 주식과 주주총회 제목을 입력해주세요."
        try:
            when = BankJournal.validate_epoch(now)
            with self.db.atomic() as conn:
                underlying = conn.execute(
                    "SELECT share_class,status FROM company.company WHERE company_code=?",
                    (code,),
                ).fetchone()
                if not underlying or underlying["share_class"] != "common" or underlying["status"] != "active":
                    raise ValueError("활성 보통주만 의결권 기준일을 만들 수 있습니다.")
                cur = conn.execute(
                    "INSERT INTO assets.dr_voting_meetings "
                    "(underlying_code,title,record_epoch,created_epoch) VALUES (?,?,?,?)",
                    (code, label[:160], when, when),
                )
                meeting_id = int(cur.lastrowid)
                snapshots = []
                direct = conn.execute(
                    "SELECT user_id,SUM(quantity) quantity FROM assets.user_assets "
                    "WHERE asset_code=? GROUP BY user_id", (code,),
                ).fetchall()
                for row in direct:
                    quantity = int(row["quantity"] or 0)
                    if quantity > 0:
                        snapshots.append((meeting_id, row["user_id"], row["user_id"],
                                          "underlying", code, quantity, when))

                programs = conn.execute(
                    "SELECT dr_code,underlying_per_receipt FROM assets.depositary_receipts "
                    "WHERE underlying_code=? AND program_status='active'", (code,),
                ).fetchall()
                for program in programs:
                    dr_code = program["dr_code"]
                    ratio = Decimal(str(program["underlying_per_receipt"]))
                    holdings = conn.execute(
                        "SELECT user_id,SUM(quantity) quantity FROM assets.user_assets "
                        "WHERE asset_code=? GROUP BY user_id", (dr_code,),
                    ).fetchall()
                    active_delegations = conn.execute(
                        "SELECT owner_user_id,proxy_user_id,SUM(dr_quantity) quantity "
                        "FROM assets.dr_voting_delegations WHERE dr_code=? AND status='active' "
                        "GROUP BY owner_user_id,proxy_user_id", (dr_code,),
                    ).fetchall()
                    by_owner = {}
                    for delegation in active_delegations:
                        by_owner.setdefault(delegation["owner_user_id"], []).append(delegation)
                    for holding in holdings:
                        owner = holding["user_id"]
                        held = int(holding["quantity"] or 0)
                        if held <= 0:
                            continue
                        delegations = by_owner.get(owner, [])
                        delegated = sum(int(item["quantity"] or 0) for item in delegations)
                        if delegated > held:
                            raise ValueError("보유량보다 많은 의결권 위임이 있어 기준일 처리를 중단했습니다.")
                        for voter, dr_quantity in [(owner, held-delegated)] + [
                            (item["proxy_user_id"], int(item["quantity"])) for item in delegations
                        ]:
                            if dr_quantity <= 0:
                                continue
                            entitlement = ratio * dr_quantity
                            if entitlement != entitlement.to_integral_value():
                                raise ValueError(f"{dr_code}의 전환비율로 정수 의결권을 산출할 수 없습니다.")
                            snapshots.append((meeting_id, owner, voter, "dr", dr_code,
                                              int(entitlement), when))
                conn.executemany(
                    "INSERT INTO assets.dr_voting_entitlement_snapshots "
                    "(meeting_id,holder_user_id,voter_user_id,source_type,source_code,quantity,recorded_epoch) "
                    "VALUES (?,?,?,?,?,?,?)", snapshots,
                )
                return True, {"meetingId": meeting_id, "underlyingCode": code,
                              "recordEpoch": when, "snapshotRows": len(snapshots)}
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f"❌ {exc}"

    def get_voting_entitlements(self, meeting_id):
        if isinstance(meeting_id, bool) or not isinstance(meeting_id, int) or meeting_id <= 0:
            return []
        rows = self.db.assets.execute(
            "SELECT voter_user_id,SUM(quantity) quantity FROM dr_voting_entitlement_snapshots "
            "WHERE meeting_id=? GROUP BY voter_user_id ORDER BY voter_user_id", (meeting_id,),
        )
        return [{"voterUserId": row["voter_user_id"], "quantity": int(row["quantity"])}
                for row in rows]

    def get_voting_record_snapshot(self, meeting_id):
        if isinstance(meeting_id, bool) or not isinstance(meeting_id, int) or meeting_id <= 0:
            return None
        meeting = self.db.assets.execute_one(
            "SELECT meeting_id,underlying_code,title,record_epoch,status "
            "FROM dr_voting_meetings WHERE meeting_id=?", (meeting_id,),
        )
        if not meeting:
            return None
        return {
            "meetingId": meeting["meeting_id"],
            "underlyingCode": meeting["underlying_code"],
            "title": meeting["title"],
            "recordEpoch": meeting["record_epoch"],
            "status": meeting["status"],
            "voters": self.get_voting_entitlements(meeting_id),
        }

    def list_voting_meetings(self, limit=100):
        try:
            bounded_limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError, OverflowError):
            bounded_limit = 100
        rows = self.db.assets.execute(
            "SELECT m.meeting_id,m.underlying_code,m.title,m.record_epoch,m.created_epoch,"
            "m.voting_open_epoch,m.voting_close_epoch,m.quorum_percent,"
            "COUNT(i.agenda_item_id) agenda_count "
            "FROM dr_voting_meetings m LEFT JOIN dr_voting_agenda_items i "
            "ON i.meeting_id=m.meeting_id GROUP BY m.meeting_id "
            "ORDER BY m.record_epoch DESC,m.meeting_id DESC LIMIT ?", (bounded_limit,),
        )
        return [{
            "meetingId": row["meeting_id"], "underlyingCode": row["underlying_code"],
            "title": row["title"], "recordEpoch": row["record_epoch"],
            "createdEpoch": row["created_epoch"], "opensEpoch": row["voting_open_epoch"],
            "closesEpoch": row["voting_close_epoch"],
            "quorumPercent": float(row["quorum_percent"] or 0),
            "agendaCount": int(row["agenda_count"]),
        } for row in rows]

    def add_voting_agenda_item(self, meeting_id, title, choices, *, now=None):
        if isinstance(meeting_id, bool) or not isinstance(meeting_id, int) or meeting_id <= 0:
            return False, "❌ 주주총회 번호를 확인해주세요."
        label = str(title or "").strip()
        if not label or len(label) > 240 or not isinstance(choices, list) or not 2 <= len(choices) <= 8:
            return False, "❌ 안건 제목과 2~8개의 선택지를 입력해주세요."
        options = [str(choice).strip() for choice in choices]
        if any(not option or len(option) > 80 for option in options) or len(set(options)) != len(options):
            return False, "❌ 선택지는 비어 있거나 중복될 수 없습니다."
        from managers.bank_journal import BankJournal
        try:
            when = BankJournal.validate_epoch(now)
            with self.db.atomic() as conn:
                if not conn.execute(
                    "SELECT 1 FROM assets.dr_voting_meetings WHERE meeting_id=?",
                    (meeting_id,),
                ).fetchone():
                    raise ValueError("주주총회를 찾을 수 없습니다.")
                cur = conn.execute(
                    "INSERT INTO assets.dr_voting_agenda_items "
                    "(meeting_id,title,choices_json,created_epoch) VALUES (?,?,?,?)",
                    (meeting_id, label, json.dumps(options, ensure_ascii=False), when),
                )
                return True, {"agendaItemId": int(cur.lastrowid), "meetingId": meeting_id,
                              "title": label, "choices": options}
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f"❌ {exc}"

    def set_voting_window(self, meeting_id, opens_epoch, closes_epoch, quorum_percent=0, *, now=None):
        if isinstance(meeting_id, bool) or not isinstance(meeting_id, int) or meeting_id <= 0:
            return False, "❌ 주주총회 번호를 확인해주세요."
        from managers.bank_journal import BankJournal
        try:
            opens = BankJournal.validate_epoch(opens_epoch)
            closes = BankJournal.validate_epoch(closes_epoch)
            when = BankJournal.validate_epoch(now)
            if opens <= when or closes <= opens:
                raise ValueError("투표 시작은 현재 이후, 종료는 시작 이후여야 합니다.")
            if isinstance(quorum_percent, bool):
                raise ValueError("정족수 비율은 0~100 숫자여야 합니다.")
            quorum = float(quorum_percent)
            if not math.isfinite(quorum) or not 0 <= quorum <= 100:
                raise ValueError("정족수 비율은 0~100 숫자여야 합니다.")
            with self.db.atomic() as conn:
                meeting = conn.execute(
                    "SELECT record_epoch,voting_open_epoch FROM assets.dr_voting_meetings "
                    "WHERE meeting_id=?", (meeting_id,),
                ).fetchone()
                if not meeting:
                    raise ValueError("주주총회를 찾을 수 없습니다.")
                if meeting["voting_open_epoch"] is not None:
                    raise ValueError("투표 일정은 이미 확정되어 변경할 수 없습니다.")
                if opens < float(meeting["record_epoch"]):
                    raise ValueError("투표 시작은 기준일 스냅샷 이후여야 합니다.")
                conn.execute(
                    "UPDATE assets.dr_voting_meetings SET voting_open_epoch=?,"
                    "voting_close_epoch=?,quorum_percent=? WHERE meeting_id=?",
                    (opens, closes, quorum, meeting_id),
                )
                return True, {"meetingId": meeting_id, "opensEpoch": opens,
                              "closesEpoch": closes, "quorumPercent": quorum}
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f"❌ {exc}"

    def cast_vote(self, voter_user_id, meeting_id, agenda_item_id, choice, *, now=None):
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
               for value in (meeting_id, agenda_item_id)):
            return False, "❌ 주주총회·안건 번호를 확인해주세요."
        voter, selected = str(voter_user_id or "").strip(), str(choice or "").strip()
        if not voter or not selected:
            return False, "❌ 투표자와 선택지를 확인해주세요."
        from managers.bank_journal import BankJournal
        try:
            when = BankJournal.validate_epoch(now)
            with self.db.atomic() as conn:
                agenda = conn.execute(
                    "SELECT i.choices_json,m.voting_open_epoch,m.voting_close_epoch "
                    "FROM assets.dr_voting_agenda_items i "
                    "JOIN assets.dr_voting_meetings m ON m.meeting_id=i.meeting_id "
                    "WHERE i.agenda_item_id=? AND i.meeting_id=?", (agenda_item_id, meeting_id),
                ).fetchone()
                if not agenda:
                    raise ValueError("해당 주주총회의 안건을 찾을 수 없습니다.")
                if agenda["voting_open_epoch"] is None or agenda["voting_close_epoch"] is None:
                    raise ValueError("관리자가 투표 기간을 아직 설정하지 않았습니다.")
                if when < float(agenda["voting_open_epoch"]):
                    raise ValueError("투표 시작 전입니다.")
                if when >= float(agenda["voting_close_epoch"]):
                    raise ValueError("투표가 종료되었습니다.")
                choices = json.loads(agenda["choices_json"])
                if selected not in choices:
                    raise ValueError("안건에서 선택할 수 없는 항목입니다.")
                entitlement = int(conn.execute(
                    "SELECT COALESCE(SUM(quantity),0) FROM assets.dr_voting_entitlement_snapshots "
                    "WHERE meeting_id=? AND voter_user_id=?", (meeting_id, voter),
                ).fetchone()[0] or 0)
                if entitlement <= 0:
                    raise ValueError("기준일 의결권이 없는 사용자입니다.")
                cur = conn.execute(
                    "INSERT INTO assets.dr_voting_ballots "
                    "(meeting_id,agenda_item_id,voter_user_id,choice,voting_power,cast_epoch) "
                    "VALUES (?,?,?,?,?,?)",
                    (meeting_id, agenda_item_id, voter, selected, entitlement, when),
                )
                return True, {"ballotId": int(cur.lastrowid), "meetingId": meeting_id,
                              "agendaItemId": agenda_item_id, "votingPower": entitlement}
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f"❌ {exc}"
        except sqlite3.IntegrityError as exc:
            # A uniqueness conflict means this voter already cast a ballot for this item.
            if "UNIQUE constraint failed" in str(exc):
                return False, "❌ 이 안건에는 이미 투표했습니다."
            raise

    def get_voting_results(self, meeting_id, agenda_item_id=None, *, now=None):
        if isinstance(meeting_id, bool) or not isinstance(meeting_id, int) or meeting_id <= 0:
            return []
        from managers.bank_journal import BankJournal
        try:
            as_of = BankJournal.validate_epoch(now)
        except (ValueError, TypeError, OverflowError):
            return []
        meeting = self.db.assets.execute_one(
            "SELECT voting_open_epoch,voting_close_epoch,quorum_percent "
            "FROM dr_voting_meetings WHERE meeting_id=?", (meeting_id,),
        )
        if not meeting:
            return []
        total_eligible = int(self.db.assets.execute_one(
            "SELECT COALESCE(SUM(quantity),0) FROM dr_voting_entitlement_snapshots "
            "WHERE meeting_id=?", (meeting_id,),
        )[0] or 0)
        params = [meeting_id]
        condition = "i.meeting_id=?"
        if agenda_item_id is not None:
            if isinstance(agenda_item_id, bool) or not isinstance(agenda_item_id, int) or agenda_item_id <= 0:
                return []
            condition += " AND i.agenda_item_id=?"
            params.append(agenda_item_id)
        rows = self.db.assets.execute(
            "SELECT i.agenda_item_id,i.title,i.choices_json,b.choice,"
            "COALESCE(SUM(b.voting_power),0) voting_power,COUNT(b.ballot_id) ballot_count "
            "FROM dr_voting_agenda_items i LEFT JOIN dr_voting_ballots b "
            "ON b.agenda_item_id=i.agenda_item_id WHERE " + condition +
            " GROUP BY i.agenda_item_id,b.choice ORDER BY i.agenda_item_id,b.choice", tuple(params),
        )
        grouped = {}
        for row in rows:
            item = grouped.setdefault(row["agenda_item_id"], {
                "agendaItemId": row["agenda_item_id"], "title": row["title"],
                "choices": {choice: {"votingPower": 0, "ballots": 0}
                            for choice in json.loads(row["choices_json"])},
            })
            if row["choice"] is not None:
                item["choices"][row["choice"]] = {
                    "votingPower": int(row["voting_power"]),
                    "ballots": int(row["ballot_count"]),
                }
        output = list(grouped.values())
        for item in output:
            turnout = sum(choice["votingPower"] for choice in item["choices"].values())
            open_epoch = meeting["voting_open_epoch"]
            close_epoch = meeting["voting_close_epoch"]
            closed = close_epoch is not None and as_of >= float(close_epoch)
            item.update({
                "eligibleVotingPower": total_eligible,
                "turnoutVotingPower": turnout,
                "turnoutPercent": turnout/total_eligible*100 if total_eligible else 0.0,
                "votingOpensEpoch": open_epoch,
                "votingClosesEpoch": close_epoch,
                "closed": closed,
                "quorumPercent": float(meeting["quorum_percent"] or 0),
                "quorumMet": (turnout/total_eligible*100 >= float(meeting["quorum_percent"] or 0)
                              if closed and total_eligible else (False if closed else None)),
            })
        return output

    def list_user_voting_agenda(self, voter_user_id, *, now=None):
        from managers.bank_journal import BankJournal
        try:
            epoch = BankJournal.validate_epoch(now)
        except (ValueError, TypeError, OverflowError):
            return []
        voter = str(voter_user_id or "").strip()
        if not voter:
            return []
        rows = self.db.assets.execute(
            "SELECT i.meeting_id,i.agenda_item_id,m.underlying_code,m.title meeting_title,"
            "m.record_epoch,m.voting_open_epoch,m.voting_close_epoch,m.quorum_percent,"
            "i.title agenda_title,i.choices_json,"
            "(SELECT SUM(s.quantity) FROM dr_voting_entitlement_snapshots s "
            "WHERE s.meeting_id=m.meeting_id AND s.voter_user_id=?) voting_power,"
            "(SELECT SUM(s.quantity) FROM dr_voting_entitlement_snapshots s "
            "WHERE s.meeting_id=m.meeting_id) total_eligible,"
            "(SELECT b.choice FROM dr_voting_ballots b WHERE b.agenda_item_id=i.agenda_item_id "
            "AND b.voter_user_id=?) my_choice "
            "FROM dr_voting_agenda_items i JOIN dr_voting_meetings m ON m.meeting_id=i.meeting_id "
            "WHERE EXISTS (SELECT 1 FROM dr_voting_entitlement_snapshots s "
            "WHERE s.meeting_id=m.meeting_id AND s.voter_user_id=?) "
            "ORDER BY m.record_epoch DESC,i.agenda_item_id DESC LIMIT 100",
            (voter, voter, voter),
        )
        output = []
        for row in rows:
            opens, closes = row["voting_open_epoch"], row["voting_close_epoch"]
            state = ("unscheduled" if opens is None or closes is None else
                     "upcoming" if epoch < float(opens) else
                     "closed" if epoch >= float(closes) else "open")
            output.append({
                "meetingId": row["meeting_id"], "agendaItemId": row["agenda_item_id"],
                "underlyingCode": row["underlying_code"], "meetingTitle": row["meeting_title"],
                "agendaTitle": row["agenda_title"], "choices": json.loads(row["choices_json"]),
                "recordEpoch": row["record_epoch"], "opensEpoch": opens,
                "closesEpoch": closes, "quorumPercent": float(row["quorum_percent"] or 0),
                "votingPower": int(row["voting_power"] or 0),
                "totalEligible": int(row["total_eligible"] or 0),
                "myChoice": row["my_choice"], "state": state,
            })
        return output

    def delegate_votes(self, owner_user_id, dr_code, proxy_user_id, quantity, *, now=None):
        """Record an internal, revocable delegation of DR voting rights."""
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            return False, "❌ 위임 수량은 1 이상의 정수 DR 수량이어야 합니다."
        owner, proxy = str(owner_user_id), str(proxy_user_id)
        code = str(dr_code or "").strip()
        if not code or not owner or not proxy or owner == proxy:
            return False, "❌ DR, 위임자, 대리인을 확인해주세요. 본인에게는 위임할 수 없습니다."
        from managers.bank_journal import BankJournal
        try:
            when = BankJournal.validate_epoch(now)
            with self.db.atomic() as conn:
                program = conn.execute(
                    "SELECT program_status FROM assets.depositary_receipts WHERE dr_code=?",
                    (code,),
                ).fetchone()
                users = conn.execute(
                    "SELECT user_id,status FROM users.users WHERE user_id IN (?,?)",
                    (owner, proxy),
                ).fetchall()
                if not program or program["program_status"] == "terminated":
                    raise ValueError("활성 DR 프로그램을 찾을 수 없습니다.")
                if len(users) != 2 or any(row["status"] != "active" for row in users):
                    raise ValueError("위임자와 대리인은 모두 활성 사용자여야 합니다.")
                owned = int(conn.execute(
                    "SELECT COALESCE(SUM(quantity),0) FROM assets.user_assets "
                    "WHERE user_id=? AND asset_code=?", (owner, code),
                ).fetchone()[0] or 0)
                delegated = int(conn.execute(
                    "SELECT COALESCE(SUM(dr_quantity),0) FROM assets.dr_voting_delegations "
                    "WHERE owner_user_id=? AND dr_code=? AND status='active'", (owner, code),
                ).fetchone()[0] or 0)
                sell_reserved = int(conn.execute(
                    "SELECT COALESCE(SUM(remaining_quantity),0) FROM assets.market_orders "
                    "WHERE user_id=? AND asset_code=? AND side='sell' "
                    "AND status IN ('scheduled','open','partial')", (owner, code),
                ).fetchone()[0] or 0)
                available = max(0, owned-delegated-sell_reserved)
                if quantity > available:
                    raise ValueError(f"위임 가능한 의결권은 {available:,} DR입니다.")
                cur = conn.execute(
                    "INSERT INTO assets.dr_voting_delegations "
                    "(dr_code,owner_user_id,proxy_user_id,dr_quantity,status,created_epoch) "
                    "VALUES (?,?,?,?,'active',?)",
                    (code, owner, proxy, quantity, when),
                )
                conn.execute(
                    "INSERT INTO assets.dr_voting_delegation_events "
                    "(delegation_id,event_type,actor_user_id,recorded_epoch) "
                    "VALUES (?,'delegated',?,?)", (int(cur.lastrowid), owner, when),
                )
                return True, {"delegationId": int(cur.lastrowid), "drCode": code,
                              "quantity": quantity, "status": "active"}
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f"❌ {exc}"

    def revoke_vote_delegation(self, owner_user_id, delegation_id, *, now=None):
        """Revoke only the caller's active delegation; keep the row for audit history."""
        if isinstance(delegation_id, bool) or not isinstance(delegation_id, int) or delegation_id <= 0:
            return False, "❌ 위임 번호를 확인해주세요."
        from managers.bank_journal import BankJournal
        try:
            when = BankJournal.validate_epoch(now)
            with self.db.atomic() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM assets.dr_voting_delegations "
                    "WHERE delegation_id=? AND owner_user_id=? AND status='active'",
                    (delegation_id, str(owner_user_id)),
                ).fetchone()
                if not exists:
                    raise ValueError("본인의 활성 의결권 위임을 찾을 수 없습니다.")
                conn.execute(
                    "INSERT INTO assets.dr_voting_delegation_events "
                    "(delegation_id,event_type,actor_user_id,recorded_epoch) "
                    "VALUES (?,'revoked',?,?)", (delegation_id, str(owner_user_id), when),
                )
                changed = conn.execute(
                    "UPDATE assets.dr_voting_delegations SET status='revoked',revoked_epoch=? "
                    "WHERE delegation_id=? AND owner_user_id=? AND status='active'",
                    (when, delegation_id, str(owner_user_id)),
                )
                if changed.rowcount != 1:
                    raise ValueError("본인의 활성 의결권 위임을 찾을 수 없습니다.")
                return True, {"delegationId": delegation_id, "status": "revoked"}
        except (ValueError, TypeError, OverflowError) as exc:
            return False, f"❌ {exc}"

    @staticmethod
    def _record_custody_process(conn, reference_key, sequence, event_type,
                                dr_code, user_id, action, quantity, shares, when):
        instruction_status = {
            "instruction_received": "requested",
            "custody_verified": "verified",
            "settled": "settled",
        }[event_type]
        instruction_event = instruction_status
        if sequence == 1:
            conn.execute(
                "INSERT INTO assets.dr_custody_instructions "
                "(reference_key,dr_code,user_id,movement_type,dr_quantity,underlying_quantity,"
                "status,requested_epoch,updated_epoch) VALUES (?,?,?,?,?,?,?, ?,?)",
                (reference_key, dr_code, user_id, action, quantity, shares,
                 instruction_status, when, when),
            )
        else:
            settled_epoch = when if instruction_status == "settled" else None
            changed = conn.execute(
                "UPDATE assets.dr_custody_instructions SET status=?,updated_epoch=?,"
                "settled_epoch=COALESCE(?,settled_epoch) WHERE reference_key=?",
                (instruction_status, when, settled_epoch, reference_key),
            )
            if changed.rowcount != 1:
                raise RuntimeError("DR 수탁 지시의 상태가 예상과 달라 처리를 중단했습니다.")
        conn.execute(
            "INSERT INTO assets.dr_custody_instruction_events "
            "(reference_key,event_sequence,event_type,recorded_epoch) VALUES (?,?,?,?)",
            (reference_key, sequence, instruction_event, when),
        )
        conn.execute(
            "INSERT INTO assets.dr_custody_process_events "
            "(reference_key,event_sequence,event_type,dr_code,user_id,movement_type,"
            "dr_quantity,underlying_quantity,recorded_epoch) VALUES (?,?,?,?,?,?,?,?,?)",
            (reference_key, sequence, event_type, dr_code, user_id, action,
             quantity, shares, when),
        )

    @staticmethod
    def _add_holding(conn, user_id, asset_code, quantity, unit_cost, currency_code):
        """Add a holding while keeping its weighted cost in native currency precision."""
        from functions.money import round_currency
        row = conn.execute(
            "SELECT holding_id,quantity,average_cost FROM assets.user_assets "
            "WHERE user_id=? AND asset_code=? AND IFNULL(account_number,'')='' "
            "ORDER BY holding_id LIMIT 1", (str(user_id), str(asset_code)),
        ).fetchone()
        if row:
            old_quantity = Decimal(str(row["quantity"] or 0))
            added_quantity = Decimal(str(quantity))
            total_quantity = old_quantity + added_quantity
            average_cost = round_currency(
                conn,
                (old_quantity * Decimal(str(row["average_cost"] or 0))
                 + added_quantity * Decimal(str(unit_cost))) / total_quantity,
                currency_code,
            )
            conn.execute(
                "UPDATE assets.user_assets SET quantity=?,average_cost=? WHERE holding_id=?",
                (float(total_quantity), average_cost, row["holding_id"]),
            )
        else:
            conn.execute(
                "INSERT INTO assets.user_assets(user_id,asset_code,account_number,quantity,average_cost) "
                "VALUES (?,?,NULL,?,?)",
                (str(user_id), str(asset_code), float(quantity), unit_cost),
            )

    def convert(self, user_id, dr_code, action, quantity, reference_key, *, now=None):
        """Issue DRs against deposited shares or redeem DRs for pooled custody shares.

        Conversion is limited to whole underlying shares. The custody ledger is a
        simulator book, not evidence of real-world depositary or custodian control.
        """
        if action not in {"issue", "redeem"}:
            return False, "❌ 전환 방향은 issue 또는 redeem이어야 합니다."
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            return False, "❌ 전환 수량은 1 이상의 정수 DR 수량이어야 합니다."
        reference_key = str(reference_key or "").strip()
        if not reference_key or len(reference_key) > 100:
            return False, "❌ 중복 실행 방지 참조값을 확인해주세요."

        from managers.bank_journal import BankJournal
        try:
            when = BankJournal.validate_epoch(now)
        except (TypeError, ValueError, OverflowError):
            return False, "❌ DR 전환 시각은 유효한 UNIX 시각이어야 합니다."
        try:
            with self.db.atomic() as conn:
                prior = conn.execute(
                    "SELECT user_id,dr_code,movement_type,dr_quantity,underlying_quantity FROM "
                    "assets.depositary_receipt_movements WHERE reference_key=?",
                    (reference_key,),
                ).fetchone()
                if prior:
                    if str(prior["user_id"]) != str(user_id) or str(prior["dr_code"]) != str(dr_code):
                        raise ValueError("이미 다른 DR 전환에 사용된 요청 참조값입니다.")
                    if prior["movement_type"] != action or int(prior["dr_quantity"]) != quantity:
                        raise ValueError("요청 참조값이 다른 DR 전환 내용에 이미 사용되었습니다.")
                    return True, (f"✅ 이 전환 요청은 이미 처리되었습니다. "
                                  f"{prior['movement_type']} {prior['dr_quantity']} DR")

                owner = conn.execute(
                    "SELECT status FROM users.users WHERE user_id=?", (str(user_id),)
                ).fetchone()
                if not owner or owner["status"] != "active":
                    raise ValueError("활성 사용자만 DR 전환을 이용할 수 있습니다.")
                program = conn.execute(
                    "SELECT d.*,a.status AS asset_status,a.total_supply,a.price AS dr_price,"
                    "a.quote_currency AS dr_currency,a.type AS asset_type "
                    "FROM assets.depositary_receipts d JOIN assets.market_assets a "
                    "ON a.code=d.dr_code WHERE d.dr_code=?",
                    (str(dr_code).strip(),),
                ).fetchone()
                if not program or program["asset_type"] != "depositary_receipt":
                    raise ValueError("주식예탁증서(DR) 프로그램을 찾을 수 없습니다.")
                if action == "issue" and (
                    program["program_status"] != "active"
                    or program["asset_status"] != "active"
                ):
                    raise ValueError("중지된 DR 프로그램은 신규 발행할 수 없습니다.")
                underlying = conn.execute(
                    "SELECT company_code AS code,share_price AS price,quote_currency,status,share_class "
                    "FROM company WHERE company_code=?",
                    (program["underlying_code"],),
                ).fetchone()
                if (not underlying or underlying["share_class"] != "common"
                        or (action == "issue" and underlying["status"] != "active")):
                    raise ValueError("기초 보통주가 거래 가능하지 않아 전환을 중지했습니다.")

                exact_shares = Decimal(quantity) * Decimal(str(program["underlying_per_receipt"]))
                shares = int(exact_shares)
                if shares <= 0 or exact_shares != Decimal(shares):
                    raise ValueError("전환 수량은 원주 정수 수량이 되도록 DR 수량을 맞춰주세요.")
                code = str(program["dr_code"])
                underlying_code = str(program["underlying_code"])
                user = str(user_id)
                self._record_custody_process(
                    conn, reference_key, 1, "instruction_received", code, user,
                    action, quantity, shares, when,
                )

                if action == "issue":
                    supply = min(
                        int(program["receipt_supply"]),
                        int(program["total_supply"] or program["receipt_supply"]),
                    )
                    outstanding = conn.execute(
                        "SELECT "
                        "(SELECT COALESCE(SUM(quantity),0) FROM assets.user_assets WHERE asset_code=?) + "
                        "(SELECT COALESCE(SUM(quantity),0) FROM assets.option_collateral_lots WHERE underlying_code=?) + "
                        "(SELECT COALESCE(SUM(quantity),0) FROM accounts.loan_collateral_assets "
                        " WHERE asset_code=? AND status='pledged') + "
                        "(SELECT COALESCE(SUM(remaining_quantity),0) FROM assets.market_orders "
                        " WHERE asset_code=? AND side='buy' AND status IN ('scheduled','open','partial'))",
                        (code, code, code, code),
                    ).fetchone()[0]
                    if int(outstanding or 0) + quantity > supply:
                        raise ValueError(f"DR 프로그램 발행 한도({supply:,})를 초과합니다.")

                    lots = conn.execute(
                        "SELECT holding_id,account_number,quantity,average_cost FROM assets.user_assets "
                        "WHERE user_id=? AND asset_code=? ORDER BY holding_id",
                        (user, underlying_code),
                    ).fetchall()
                    reserved_by_account = {
                        row["funding_account"]: float(row["quantity"] or 0)
                        for row in conn.execute(
                            "SELECT funding_account,SUM(remaining_quantity) AS quantity "
                            "FROM assets.market_orders WHERE user_id=? AND asset_code=? AND side='sell' "
                            "AND status IN ('scheduled','open','partial') GROUP BY funding_account",
                            (user, underlying_code),
                        ).fetchall()
                    }
                    capacities = {}
                    for lot in lots:
                        account = lot["account_number"]
                        capacities[account] = capacities.get(account, 0.0) + float(lot["quantity"] or 0)
                    for account, reserved in reserved_by_account.items():
                        capacities[account] = max(0.0, capacities.get(account, 0.0) - reserved)
                    available = sum(math.floor(max(0.0, value) + 1e-9) for value in capacities.values())
                    if available + 1e-9 < shares:
                        raise ValueError(f"전환 가능한 {underlying_code} 원주가 부족합니다. 필요 {shares:,}, 가능 {int(available):,}주입니다.")

                    self._record_custody_process(
                        conn, reference_key, 2, "custody_verified", code, user,
                        action, quantity, shares, when,
                    )

                    remaining = shares
                    total_cost = Decimal(0)
                    for lot in lots:
                        account = lot["account_number"]
                        capacity = capacities.get(account, 0.0)
                        old_quantity = float(lot["quantity"] or 0)
                        take = min(remaining, int(math.floor(capacity + 1e-9)),
                                   int(math.floor(old_quantity + 1e-9)))
                        if take <= 0:
                            continue
                        total_cost += Decimal(str(lot["average_cost"] or 0)) * take
                        changed = (conn.execute(
                            "DELETE FROM assets.user_assets WHERE holding_id=? AND quantity=?",
                            (lot["holding_id"], lot["quantity"]),
                        ) if math.isclose(take, old_quantity, rel_tol=0.0, abs_tol=1e-9) else conn.execute(
                            "UPDATE assets.user_assets SET quantity=quantity-? WHERE holding_id=? AND quantity=?",
                            (take, lot["holding_id"], lot["quantity"]),
                        ))
                        if changed.rowcount != 1:
                            raise RuntimeError("원주 보유량이 변경되어 DR 전환을 취소했습니다.")
                        capacities[account] = capacity - take
                        remaining -= take
                        if not remaining:
                            break
                    if remaining:
                        raise RuntimeError("예약 주문을 제외한 원주 수량이 부족해 전환을 취소했습니다.")
                    from functions.money import round_currency
                    from managers.currency_manager import CurrencyManager
                    underlying_cost = round_currency(conn, total_cost / shares, underlying["quote_currency"])
                    fx = CurrencyManager.quote(conn, underlying["quote_currency"], program["dr_currency"])
                    dr_cost = round_currency(
                        conn, Decimal(underlying_cost) * exact_shares / Decimal(quantity) *
                        Decimal(fx.numerator) / Decimal(fx.denominator), program["dr_currency"],
                    )
                    self._add_holding(conn, user, code, quantity, dr_cost, program["dr_currency"])
                    underlying_average_cost = underlying_cost
                    holder_underlying_average_cost = underlying_cost
                else:
                    owned = conn.execute(
                        "SELECT COALESCE(SUM(quantity),0) FROM assets.user_assets "
                        "WHERE user_id=? AND asset_code=?", (user, code),
                    ).fetchone()[0]
                    reserved = conn.execute(
                        "SELECT COALESCE(SUM(remaining_quantity),0) FROM assets.market_orders "
                        "WHERE user_id=? AND asset_code=? AND side='sell' "
                        "AND status IN ('scheduled','open','partial')", (user, code),
                    ).fetchone()[0]
                    delegated = conn.execute(
                        "SELECT COALESCE(SUM(dr_quantity),0) FROM assets.dr_voting_delegations "
                        "WHERE owner_user_id=? AND dr_code=? AND status='active'", (user, code),
                    ).fetchone()[0]
                    if (float(owned or 0) - float(reserved or 0) - float(delegated or 0)
                            + 1e-9 < quantity):
                        raise ValueError("미체결 매도 주문 및 위임된 의결권을 제외한 DR 보유 수량이 부족합니다.")
                    custody = conn.execute(
                        "SELECT COALESCE(SUM(CASE WHEN movement_type='issue' THEN underlying_quantity "
                        "ELSE -underlying_quantity END),0) AS shares, "
                        "COALESCE(SUM(CASE WHEN movement_type='issue' THEN underlying_quantity*"
                        "COALESCE(custody_underlying_average_cost,underlying_average_cost) "
                        "ELSE -underlying_quantity*COALESCE(custody_underlying_average_cost,"
                        "underlying_average_cost) END),0) AS cost "
                        "FROM assets.depositary_receipt_movements WHERE dr_code=?",
                        (code,),
                    ).fetchone()
                    if int(custody["shares"] or 0) < shares:
                        raise ValueError("상환 가능한 시뮬레이터 예탁 원주가 부족합니다.")
                    self._record_custody_process(
                        conn, reference_key, 2, "custody_verified", code, user,
                        action, quantity, shares, when,
                    )
                    dr_lots = conn.execute(
                        "SELECT holding_id,quantity,average_cost FROM assets.user_assets "
                        "WHERE user_id=? AND asset_code=? ORDER BY holding_id", (user, code),
                    ).fetchall()
                    remaining = quantity
                    total_dr_cost = Decimal(0)
                    for lot in dr_lots:
                        take = min(remaining, int(lot["quantity"] or 0))
                        if take <= 0:
                            continue
                        total_dr_cost += Decimal(str(lot["average_cost"] or 0))*take
                        changed = (conn.execute(
                            "DELETE FROM assets.user_assets WHERE holding_id=? AND quantity=?",
                            (lot["holding_id"], lot["quantity"]),
                        ) if take == int(lot["quantity"]) else conn.execute(
                            "UPDATE assets.user_assets SET quantity=quantity-? WHERE holding_id=? AND quantity=?",
                            (take, lot["holding_id"], lot["quantity"]),
                        ))
                        if changed.rowcount != 1:
                            raise RuntimeError("DR 보유량이 변경되어 상환을 취소했습니다.")
                        remaining -= take
                        if not remaining:
                            break
                    if remaining:
                        raise RuntimeError("DR 보유 수량이 부족해 상환을 취소했습니다.")
                    from functions.money import round_currency
                    underlying_average_cost = round_currency(
                        conn, Decimal(str(custody["cost"] or 0)) / Decimal(int(custody["shares"])),
                        underlying["quote_currency"],
                    )
                    from managers.currency_manager import CurrencyManager
                    fx = CurrencyManager.quote(
                        conn, program["dr_currency"], underlying["quote_currency"]
                    )
                    holder_underlying_average_cost = round_currency(
                        conn,
                        total_dr_cost*Decimal(fx.numerator)/Decimal(fx.denominator)/Decimal(shares),
                        underlying["quote_currency"],
                    )
                    self._add_holding(conn, user, underlying_code, shares,
                                      holder_underlying_average_cost, underlying["quote_currency"])

                conn.execute(
                    "INSERT INTO assets.depositary_receipt_movements(reference_key,dr_code,user_id,"
                    "movement_type,dr_quantity,underlying_quantity,underlying_average_cost,recorded_epoch,"
                    "holder_underlying_average_cost,holder_underlying_currency,"
                    "custody_underlying_average_cost,custody_underlying_currency) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (reference_key, code, user, action, quantity, shares,
                     underlying_average_cost, when, holder_underlying_average_cost,
                     underlying["quote_currency"], underlying_average_cost,
                     underlying["quote_currency"]),
                )
                self._record_custody_process(
                    conn, reference_key, 3, "settled", code, user,
                    action, quantity, shares, when,
                )
                # A DR represents beneficial ownership of its underlying shares
                # for the simulator's majority-holder/control summary.
                from managers.user_manager import UserManager
                UserManager._refresh_max_shareholder(conn, code)
                return True, (f"✅ DR {action} 전환 완료: {quantity:,} {code} ↔ "
                              f"{shares:,} {underlying_code} 원주 (시뮬레이터 원장)")
        except (ValueError, RuntimeError, KeyError, TypeError, OverflowError) as exc:
            return False, f"❌ {exc}"
