"""Organization groups and scale classification helpers.

Amounts passed to ``classify_size`` must already be converted to the configured
base currency at the point of use. This avoids persisting FX conversion values
or their dates.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from collections import defaultdict
import json
import time


class OrganizationManager:
    @staticmethod
    def ownership_graph(conn, organization_code, max_depth=3, max_children=20, min_percent=0):
        """Build a bounded, read-only direct/indirect ownership graph.

        Percentages use exact decimal strings. The direct equity and vote ratios
        are computed separately from the currently issued share classes.
        """
        from database.user_db import TREASURY_USER_ID

        root = str(organization_code)
        max_depth = max(0, min(int(max_depth), 5))
        max_children = max(1, min(int(max_children), 100))
        min_percent = Decimal(str(min_percent))
        accounts = {str(row[1]): str(row[0]) for row in conn.execute(
            "SELECT company_code,user_id FROM company_accounts"
        )}
        companies = {str(row[0]): str(row[1]) for row in conn.execute(
            "SELECT company_code,name FROM company WHERE share_class='common'"
        )}
        nodes = {}
        edges = []
        expanded = set()

        def expand(code, depth, ancestry, path_equity=Decimal(1), path_votes=Decimal(1)):
            if code in expanded or depth > max_depth:
                return
            expanded.add(code)
            org = conn.execute(
                "SELECT name FROM company WHERE company_code=? AND share_class='common'", (code,)
            ).fetchone()
            if not org:
                return
            nodes["company:" + code] = {"id": "company:" + code, "kind": "company", "code": code, "name": str(org[0] or code)}
            issues = conn.execute(
                "SELECT c.company_code,c.name,c.total_shares,c.share_class,"
                "COALESCE(s.voting_rights_per_share,'1'),COALESCE(s.economic_interest_per_share,'1') "
                "FROM company c LEFT JOIN organization_share_issuances s ON s.legacy_product_code=c.company_code "
                "WHERE (c.company_code=? AND COALESCE(c.share_class,'common')='common') "
                "OR (c.parent_company_code=? AND c.share_class='preferred') ORDER BY c.company_code",
                (code, code),
            ).fetchall()
            equity_total = Decimal(0)
            votes_total = Decimal(0)
            owner_amounts = defaultdict(lambda: [Decimal(0), Decimal(0)])
            for issue in issues:
                issue_code, _, supply, _, vote_text, equity_text = issue
                supply = Decimal(str(supply or 0))
                try:
                    vote_per_share = Decimal(str(vote_text))
                    equity_per_share = Decimal(str(equity_text))
                except InvalidOperation:
                    continue
                if not supply.is_finite() or supply <= 0 or not vote_per_share.is_finite() or vote_per_share < 0 or not equity_per_share.is_finite() or equity_per_share < 0:
                    continue
                holdings = conn.execute(
                    "SELECT user_id,SUM(quantity) FROM user_assets WHERE asset_code=? GROUP BY user_id",
                    (str(issue_code),),
                ).fetchall()
                treasury_ids = {TREASURY_USER_ID} | {user_id for user_id, issuer in accounts.items() if issuer == code}
                own_treasury = sum((Decimal(str(item[1] or 0)) for item in holdings if str(item[0]) in treasury_ids), Decimal(0))
                outstanding = max(Decimal(0), supply - own_treasury)
                equity_total += outstanding * equity_per_share
                votes_total += outstanding * vote_per_share
                for holder, quantity in holdings:
                    holder = str(holder)
                    if holder in treasury_ids:
                        continue
                    quantity = Decimal(str(quantity or 0))
                    owner_amounts[holder][0] += quantity * equity_per_share
                    owner_amounts[holder][1] += quantity * vote_per_share

            display_candidates = []
            for holder, (equity, votes) in owner_amounts.items():
                equity_ratio = equity / equity_total if equity_total else Decimal(0)
                votes_ratio = votes / votes_total if votes_total else Decimal(0)
                if max(equity_ratio, votes_ratio) * 100 < min_percent:
                    continue
                parent_code = accounts.get(holder)
                if parent_code:
                    node_id = "company:" + parent_code
                    nodes[node_id] = {"id": node_id, "kind": "company", "code": parent_code,
                                      "name": companies.get(parent_code, parent_code)}
                else:
                    node_id = "user:" + holder
                    user = conn.execute("SELECT nickname FROM users WHERE user_id=?", (holder,)).fetchone()
                    nodes[node_id] = {"id": node_id, "kind": "user", "code": holder,
                                      "name": str(user[0] or holder) if user else holder}
                display_candidates.append((node_id, holder, equity_ratio, votes_ratio, parent_code))
            display_candidates.sort(key=lambda item: (item[0].startswith("user:"), -max(item[2], item[3]), item[0]))
            for node_id, holder, equity_ratio, votes_ratio, parent_code in display_candidates[:max_children]:
                root_equity = path_equity * equity_ratio
                root_votes = path_votes * votes_ratio
                edges.append({"from": node_id, "to": "company:" + code,
                              "equity_percent": str(equity_ratio * 100),
                              "voting_percent": str(votes_ratio * 100),
                              "root_equity_percent": str(root_equity * 100),
                              "root_voting_percent": str(root_votes * 100),
                              "depth": depth,
                              "direct": True})
                if parent_code and parent_code not in ancestry and depth < max_depth:
                    expand(parent_code, depth + 1, ancestry | {code}, equity_ratio, votes_ratio)

        expand(root, 0, set())
        edges.sort(key=lambda edge: (edge["to"], edge["from"]))
        return {"nodes": list(nodes.values()), "edges": edges, "max_depth": max_depth,
                "truncated": len(edges) >= max_children * max(1, len(expanded))}

    @staticmethod
    def classify_size(metrics, rules, mode="any"):
        """Classify against configured limits, or return None without evidence.

        ``rules`` maps revenue/assets/employees to the SME and mid-market upper
        bounds. No statutory or currency thresholds are assumed by the code.
        """
        if mode not in {"any", "all"}:
            raise ValueError("규모 판정 방식은 any 또는 all이어야 합니다.")
        evidence = []
        for key in ("revenue", "assets", "employees"):
            value, limits = metrics.get(key), rules.get(key)
            if value is None or not isinstance(limits, (list, tuple)) or len(limits) != 2:
                continue
            try:
                value, small_limit, middle_limit = map(Decimal, map(str, (value, limits[0], limits[1])))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if not all(number.is_finite() and number >= 0 for number in (value, small_limit, middle_limit)):
                continue
            if small_limit > middle_limit:
                raise ValueError(f"{key} 규모 경계값 순서를 확인해주세요.")
            evidence.append((value, small_limit, middle_limit))
        if not evidence:
            return None
        over_middle = [value > middle for value, _, middle in evidence]
        over_small = [value > small for value, small, _ in evidence]
        if mode == "all":
            if all(over_middle):
                return "large"
            if all(over_small):
                return "mid_market"
            return "sme"
        if any(over_middle):
            return "large"
        if any(over_small):
            return "mid_market"
        return "sme"

    @staticmethod
    def displayed_size(row):
        return row["size_override"] or row["auto_size"]

    @classmethod
    def refresh_auto_size(cls, conn, organization_code, metrics, now=None):
        settings = conn.execute(
            "SELECT rule_json FROM organization_size_settings WHERE singleton=1"
        ).fetchone()
        if not settings:
            return {"status": "unconfigured", "size": None}
        try:
            rules = json.loads(settings[0] or "{}")
        except (TypeError, ValueError):
            raise ValueError("기업 규모 판정 설정을 읽을 수 없습니다.") from None
        size = cls.classify_size(metrics, rules.get("thresholds", {}), rules.get("mode", "any"))
        if size is None:
            return {"status": "insufficient_data", "size": None}
        result = cls.update_size_candidate(conn, organization_code, size, now)
        return {**result, "size": size}

    @staticmethod
    def update_size_candidate(conn, organization_code, size, now=None):
        """Apply immediately or stage an auto-size change for its notice period."""
        if size not in {None, "sme", "mid_market", "large"}:
            raise ValueError("기업 규모를 확인해주세요.")
        now = int(time.time() if now is None else now)
        with conn:
            row = conn.execute(
                "SELECT auto_size,pending_size,size_effective_epoch FROM organizations WHERE organization_code=?",
                (str(organization_code),),
            ).fetchone()
            if not row:
                raise ValueError("기업을 찾을 수 없습니다.")
            previous = row[0]
            if size is not None and size == row[1]:
                return {"status": "already_announced", "effective_epoch": row[2]}
            settings = conn.execute(
                "SELECT announcement_seconds FROM organization_size_settings WHERE singleton=1"
            ).fetchone()
            delay = int(settings[0]) if settings else 0
            if size == previous:
                conn.execute(
                    "UPDATE organizations SET pending_size=NULL,size_effective_epoch=NULL,updated_epoch=? "
                    "WHERE organization_code=?", (now, str(organization_code))
                )
                return {"status": "unchanged", "effective_epoch": None}
            if delay == 0:
                conn.execute(
                    "UPDATE organizations SET auto_size=?,pending_size=NULL,size_effective_epoch=NULL,updated_epoch=? "
                    "WHERE organization_code=?", (size, now, str(organization_code))
                )
                return {"status": "applied", "effective_epoch": now}
            effective = now + delay
            conn.execute(
                "UPDATE organizations SET pending_size=?,size_effective_epoch=?,updated_epoch=? "
                "WHERE organization_code=?", (size, effective, now, str(organization_code))
            )
            return {"status": "announced", "effective_epoch": effective}

    @staticmethod
    def apply_due_size_changes(conn, now=None):
        now = int(time.time() if now is None else now)
        with conn:
            rows = conn.execute(
                "SELECT organization_code,pending_size FROM organizations "
                "WHERE pending_size IS NOT NULL AND size_effective_epoch<=?",
                (now,),
            ).fetchall()
            conn.execute(
                "UPDATE organizations SET auto_size=pending_size,pending_size=NULL,"
                "size_effective_epoch=NULL,updated_epoch=? "
                "WHERE pending_size IS NOT NULL AND size_effective_epoch<=?",
                (now, now),
            )
        return [(str(row[0]), str(row[1])) for row in rows]

    @staticmethod
    def create_group(conn, name, representative=None, description=""):
        name = str(name).strip()
        if not name or len(name) > 120:
            raise ValueError("기업집단 이름은 1~120자로 입력해주세요.")
        with conn:
            cursor = conn.execute(
                "INSERT INTO organization_groups(name,representative_organization_code,description) "
                "VALUES (?,?,?)", (name, representative, str(description)[:1000])
            )
            return int(cursor.lastrowid)

    @staticmethod
    def set_group_member(conn, group_id, organization_code, active=True, now=None):
        now = int(time.time() if now is None else now)
        group_id, organization_code = int(group_id), str(organization_code)
        with conn:
            group = conn.execute(
                "SELECT 1 FROM organization_groups WHERE group_id=? AND status='active'", (group_id,)
            ).fetchone()
            org = conn.execute(
                "SELECT 1 FROM organizations WHERE organization_code=?", (organization_code,)
            ).fetchone()
            if not group or not org:
                raise ValueError("활성 기업집단과 기업을 확인해주세요.")
            existing = conn.execute(
                "SELECT group_id FROM organization_group_members WHERE organization_code=? AND left_epoch IS NULL",
                (organization_code,),
            ).fetchone()
            if active and existing and int(existing[0]) != group_id:
                raise ValueError("기업은 동시에 하나의 기업집단에만 소속될 수 있습니다.")
            if active and not existing:
                conn.execute(
                    "INSERT INTO organization_group_members(group_id,organization_code,joined_epoch) VALUES (?,?,?)",
                    (group_id, organization_code, now),
                )
            elif not active and existing and int(existing[0]) == group_id:
                conn.execute(
                    "UPDATE organization_group_members SET left_epoch=? "
                    "WHERE organization_code=? AND group_id=? AND left_epoch IS NULL",
                    (now, organization_code, group_id),
                )
