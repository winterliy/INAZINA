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
    def user_ownership_graph(conn, user_id, max_depth=8, max_children=100, min_percent=0):
        """Show a user's direct shareholdings and subsidiaries of companies they control.

        Direct positions include every company share class. Descendant expansion is
        limited to majority-vote control and simple paths, so cross-holdings cannot
        recurse forever. Root percentages are summed across distinct paths.
        """
        from database.user_db import TREASURY_USER_ID

        user_id = str(user_id)
        max_depth = max(0, min(int(max_depth), 8))
        max_children = max(1, min(int(max_children), 100))
        min_percent = Decimal(str(min_percent))
        accounts = {str(row[1]): str(row[0]) for row in conn.execute(
            "SELECT company_code,user_id FROM company_accounts"
        )}
        company_accounts = {company: account for account, company in accounts.items()}
        treasury_ids = {str(TREASURY_USER_ID)} | {str(row[0]) for row in conn.execute("SELECT user_id FROM country_treasuries")}
        nodes = {}
        edges = {}
        owner_cache = {}
        truncated = False

        def add_node(node_id, kind, code, name):
            nodes[node_id] = {"id": node_id, "kind": kind, "code": code, "name": str(name or code)}

        def ownership(code):
            if code in owner_cache:
                return owner_cache[code]
            issuer_treasuries = treasury_ids | {company_accounts.get(code)}
            issuer_treasuries.discard(None)
            issues = conn.execute(
                "SELECT c.company_code,c.total_shares,s.voting_rights_per_share,"
                "s.economic_interest_per_share FROM share_issuances s JOIN company c ON c.share_issuance_id=s.issuance_id "
                ""
                "WHERE s.organization_code=?", (code,),
            ).fetchall()
            totals = [Decimal(0), Decimal(0)]
            owners = defaultdict(lambda: [Decimal(0), Decimal(0)])
            for issue_code, supply_text, vote_text, equity_text in issues:
                try:
                    supply, votes, equity = map(Decimal, (str(supply_text or 0), str(vote_text or 0), str(equity_text or 0)))
                except InvalidOperation:
                    continue
                if not all(value.is_finite() for value in (supply, votes, equity)) or supply <= 0 or votes < 0 or equity < 0:
                    continue
                holdings = defaultdict(Decimal)
                for holder, quantity in conn.execute(
                    "SELECT user_id,quantity FROM user_assets WHERE asset_code=?", (str(issue_code),)
                ):
                    try:
                        amount = Decimal(str(quantity or 0))
                    except InvalidOperation:
                        continue
                    if amount.is_finite() and amount > 0:
                        holdings[str(holder)] += amount
                own = sum((quantity for holder, quantity in holdings.items() if holder in issuer_treasuries), Decimal(0))
                outstanding = max(Decimal(0), supply - own)
                totals[0] += outstanding * equity
                totals[1] += outstanding * votes
                for holder, quantity in holdings.items():
                    if holder in issuer_treasuries:
                        continue
                    owners[holder][0] += quantity * equity
                    owners[holder][1] += quantity * votes
            ratios = {holder: (values[0] / totals[0] if totals[0] else Decimal(0),
                               values[1] / totals[1] if totals[1] else Decimal(0))
                      for holder, values in owners.items()}
            owner_cache[code] = ratios
            return ratios

        user = conn.execute("SELECT nickname FROM users WHERE user_id=?", (user_id,)).fetchone()
        root_id = "user:" + user_id
        add_node(root_id, "user", user_id, user[0] if user else user_id)
        direct_orgs = set()
        for (code,) in conn.execute(
            "SELECT DISTINCT s.organization_code FROM user_assets a "
            "JOIN company c ON c.company_code=a.asset_code JOIN share_issuances s ON s.issuance_id=c.share_issuance_id "
            "WHERE a.user_id=?", (user_id,),
        ):
            direct_orgs.add(str(code))
        root_candidates = []
        for code in direct_orgs:
            ratios = ownership(code).get(user_id)
            if not ratios:
                continue
            equity_ratio, voting_ratio = ratios
            if max(equity_ratio, voting_ratio) * 100 < min_percent:
                continue
            root_candidates.append((code, equity_ratio, voting_ratio))
        root_candidates.sort(key=lambda item: (-max(item[1], item[2]), -item[2], item[0]))
        roots = []
        if len(root_candidates) > max_children:
            truncated = True
        for code, equity_ratio, voting_ratio in root_candidates[:max_children]:
            org = conn.execute("SELECT name FROM organizations WHERE organization_code=?", (code,)).fetchone()
            node_id = "company:" + code
            add_node(node_id, "company", code, org[0] if org else code)
            edges[(root_id, node_id)] = {
                "from": root_id, "to": node_id,
                "equity_percent": str(equity_ratio * 100), "voting_percent": str(voting_ratio * 100),
                "root_equity_percent": str(equity_ratio * 100), "root_voting_percent": str(voting_ratio * 100),
                "depth": 0, "direct": True,
            }
            if voting_ratio > Decimal("0.5"):
                controlled_paths = {user_id: (Decimal(1), Decimal(1)),
                                    company_accounts.get(code): (equity_ratio, voting_ratio)}
                controlled_paths.pop(None, None)
                roots.append((code, controlled_paths, frozenset({code})))

        # A controlled group can hold a majority through several of its members.
        # Keep each direct block visible, then expand the issuer when the group's
        # combined voting rights exceed half of the outstanding vote.
        visited_states = set()

        def expand_group(controlled_code, controlled_paths, ancestry, depth):
            nonlocal truncated
            state = (controlled_code, tuple(sorted(controlled_paths)))
            if state in visited_states:
                return
            if len(edges) >= 1000:
                truncated = True
                return
            visited_states.add(state)
            if depth > max_depth:
                truncated = True
                return
            held_codes = set()
            for holder in controlled_paths:
                for (issuer,) in conn.execute(
                    "SELECT DISTINCT s.organization_code FROM user_assets a "
                    "JOIN company c ON c.company_code=a.asset_code "
                    "JOIN share_issuances s ON s.issuance_id=c.share_issuance_id "
                    "WHERE a.user_id=?", (holder,),
                ):
                    held_codes.add(str(issuer))
            candidates = []
            for issuer in held_codes - set(ancestry):
                ratios = ownership(issuer)
                group_equity = sum((ratios.get(holder, (Decimal(0), Decimal(0)))[0]
                                    for holder in controlled_paths), Decimal(0))
                group_votes = sum((ratios.get(holder, (Decimal(0), Decimal(0)))[1]
                                   for holder in controlled_paths), Decimal(0))
                if group_votes > Decimal("0.5"):
                    candidates.append((issuer, ratios, group_equity, group_votes))
            candidates.sort(key=lambda item: (-item[3], -item[2], item[0]))
            if len(candidates) > max_children:
                truncated = True
            for issuer, ratios, group_equity, group_votes in candidates[:max_children]:
                if len(edges) >= 1000:
                    truncated = True
                    return
                org = conn.execute("SELECT name FROM organizations WHERE organization_code=?", (issuer,)).fetchone()
                node_id = "company:" + issuer
                add_node(node_id, "company", issuer, org[0] if org else issuer)
                child_paths = {}
                for holder, (path_equity, path_votes) in controlled_paths.items():
                    equity_ratio, voting_ratio = ratios.get(holder, (Decimal(0), Decimal(0)))
                    child_paths[holder] = (path_equity, path_votes)
                    if max(equity_ratio, voting_ratio) * 100 < min_percent or not (equity_ratio or voting_ratio):
                        continue
                    parent_code = company_accounts.get(holder)
                    if parent_code:
                        parent_id = "company:" + parent_code
                        parent = conn.execute("SELECT name FROM organizations WHERE organization_code=?", (parent_code,)).fetchone()
                        add_node(parent_id, "company", parent_code, parent[0] if parent else parent_code)
                    else:
                        parent_id = "user:" + holder
                        user = conn.execute("SELECT nickname FROM users WHERE user_id=?", (holder,)).fetchone()
                        add_node(parent_id, "user", holder, user[0] if user else holder)
                    edge = edges.setdefault((parent_id, node_id), {
                        "from": parent_id, "to": node_id,
                        "equity_percent": str(equity_ratio * 100), "voting_percent": str(voting_ratio * 100),
                        "root_equity_percent": "0", "root_voting_percent": "0",
                        "depth": depth, "direct": False,
                    })
                    edge["root_equity_percent"] = str(Decimal(edge["root_equity_percent"]) + path_equity * equity_ratio * 100)
                    edge["root_voting_percent"] = str(Decimal(edge["root_voting_percent"]) + path_votes * voting_ratio * 100)
                child_account = company_accounts.get(issuer)
                if child_account:
                    child_paths[child_account] = (
                        sum((path[0] * ratios.get(holder, (Decimal(0), Decimal(0)))[0]
                             for holder, path in controlled_paths.items()), Decimal(0)),
                        sum((path[1] * ratios.get(holder, (Decimal(0), Decimal(0)))[1]
                             for holder, path in controlled_paths.items()), Decimal(0)),
                    )
                expand_group(issuer, child_paths, ancestry | {issuer}, depth + 1)

        for code, controlled_paths, ancestry in roots:
            expand_group(code, controlled_paths, ancestry, 1)

        return {"nodes": list(nodes.values()), "edges": sorted(edges.values(), key=lambda edge: (edge["depth"], edge["to"], edge["from"])),
                "max_depth": max_depth, "truncated": truncated, "root_kind": "user"}

    @staticmethod
    def ownership_graph(conn, organization_code, max_depth=8, max_children=100, min_percent=0):
        """Build a bounded, read-only direct/indirect ownership graph.

        Percentages use exact decimal strings. The direct equity and vote ratios
        are computed separately from the currently issued share classes.
        """
        from database.user_db import TREASURY_USER_ID

        root = str(organization_code)
        max_depth = max(0, min(int(max_depth), 8))
        max_children = max(1, min(int(max_children), 100))
        min_percent = Decimal(str(min_percent))
        accounts = {str(row[1]): str(row[0]) for row in conn.execute(
            "SELECT company_code,user_id FROM company_accounts"
        )}
        treasury_user_ids = {str(TREASURY_USER_ID)} | {str(row[0]) for row in conn.execute("SELECT user_id FROM country_treasuries")}
        companies = {str(row[0]): str(row[1]) for row in conn.execute(
            "SELECT organization_code,name FROM organizations"
        )}
        nodes = {}
        edge_map = {}
        attributions = {}
        expansion_count = 0
        truncated = False

        def expand(code, depth, ancestry, path_equity=Decimal(1), path_votes=Decimal(1)):
            nonlocal expansion_count, truncated
            if code in ancestry or depth > max_depth:
                return
            if expansion_count >= 1000:
                truncated = True
                return
            expansion_count += 1
            org = conn.execute(
                "SELECT name FROM organizations WHERE organization_code=?", (code,)
            ).fetchone()
            if not org:
                return
            nodes["company:" + code] = {"id": "company:" + code, "kind": "company", "code": code, "name": str(org[0] or code)}
            issues = conn.execute(
                "SELECT c.company_code,c.name,c.total_shares,s.share_class,"
                "s.voting_rights_per_share,s.economic_interest_per_share "
                "FROM share_issuances s JOIN company c ON c.share_issuance_id=s.issuance_id "
                ""
                "WHERE s.organization_code=? ORDER BY c.company_code", (code,),
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
                holdings = defaultdict(Decimal)
                for holder, quantity in conn.execute(
                    "SELECT user_id,quantity FROM user_assets WHERE asset_code=?",
                    (str(issue_code),),
                ):
                    holdings[str(holder)] += Decimal(str(quantity or 0))
                treasury_ids = treasury_user_ids | {user_id for user_id, issuer in accounts.items() if issuer == code}
                own_treasury = sum((quantity for holder, quantity in holdings.items() if holder in treasury_ids), Decimal(0))
                outstanding = max(Decimal(0), supply - own_treasury)
                equity_total += outstanding * equity_per_share
                votes_total += outstanding * vote_per_share
                for holder, quantity in holdings.items():
                    if holder in treasury_ids:
                        continue
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
            if len(display_candidates) > max_children:
                truncated = True
            for node_id, holder, equity_ratio, votes_ratio, parent_code in display_candidates[:max_children]:
                root_equity = path_equity * equity_ratio
                root_votes = path_votes * votes_ratio
                key = (node_id, "company:" + code)
                edge = edge_map.setdefault(key, {
                    "from": node_id, "to": "company:" + code,
                    "equity_percent": str(equity_ratio * 100),
                    "voting_percent": str(votes_ratio * 100),
                    "root_equity_percent": "0", "root_voting_percent": "0",
                    "depth": depth, "direct": depth == 0,
                    "cycle": bool(parent_code and parent_code in ancestry),
                })
                edge["root_equity_percent"] = str(Decimal(edge["root_equity_percent"]) + root_equity * 100)
                edge["root_voting_percent"] = str(Decimal(edge["root_voting_percent"]) + root_votes * 100)
                can_expand = parent_code and parent_code not in ancestry and depth < max_depth
                if can_expand:
                    expand(parent_code, depth + 1, ancestry | {code}, root_equity, root_votes)
                else:
                    if parent_code and parent_code not in ancestry and depth >= max_depth:
                        truncated = True
                    owner = attributions.setdefault(node_id, {
                        "id": node_id, "kind": "company" if parent_code else "user",
                        "code": parent_code or holder, "name": nodes[node_id]["name"],
                        "root_equity_percent": "0", "root_voting_percent": "0",
                    })
                    owner["root_equity_percent"] = str(Decimal(owner["root_equity_percent"]) + root_equity * 100)
                    owner["root_voting_percent"] = str(Decimal(owner["root_voting_percent"]) + root_votes * 100)

        expand(root, 0, set())
        edges = sorted(edge_map.values(), key=lambda edge: (edge["to"], edge["from"]))
        return {"nodes": list(nodes.values()), "edges": edges, "max_depth": max_depth,
                "truncated": truncated, "root_kind": "organization",
                "beneficial_owners": sorted(attributions.values(),
                    key=lambda owner: (-max(Decimal(owner["root_equity_percent"]), Decimal(owner["root_voting_percent"])), owner["id"]))}

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
