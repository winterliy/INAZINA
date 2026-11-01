"""Organization entities and their independently identified share issuances.

The existing ``company`` table remains the trading and holdings source of truth
while the market ticker remains on the legacy company catalog. Issuances have
stable IDs; tickers link to them so orders and historical holdings stay intact.
"""
from .db_manager import DBManager
import json


class OrganizationDB(DBManager):
    def __init__(self):
        super().__init__("organization")
        self._init_db()

    def _init_db(self):
        self.execute("""
            CREATE TABLE IF NOT EXISTS organizations (
                organization_code TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                industry TEXT NOT NULL DEFAULT '',
                country_code TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                purpose TEXT NOT NULL DEFAULT 'for_profit'
                    CHECK(purpose IN ('for_profit','nonprofit')),
                ownership_type TEXT NOT NULL DEFAULT 'private'
                    CHECK(ownership_type IN ('private','national','local','mixed','other')),
                public_enterprise_type TEXT NOT NULL DEFAULT 'none'
                    CHECK(public_enterprise_type IN ('none','public_corporation','market_based','quasi_market_based','other_public')),
                legal_form TEXT NOT NULL DEFAULT 'corporation',
                size_override TEXT CHECK(size_override IS NULL OR size_override IN ('sme','mid_market','large')),
                size_override_reason TEXT,
                auto_size TEXT CHECK(auto_size IS NULL OR auto_size IN ('sme','mid_market','large')),
                pending_size TEXT CHECK(pending_size IS NULL OR pending_size IN ('sme','mid_market','large')),
                size_effective_epoch INTEGER,
                created_epoch INTEGER NOT NULL DEFAULT (unixepoch()),
                updated_epoch INTEGER NOT NULL DEFAULT (unixepoch())
            )
        """)
        self.execute("""
            CREATE TABLE IF NOT EXISTS share_issuances (
                issuance_id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_code TEXT NOT NULL REFERENCES organizations(organization_code),
                legacy_product_code TEXT UNIQUE,
                class_name TEXT NOT NULL,
                share_class TEXT NOT NULL CHECK(share_class IN ('common','preferred')),
                voting_rights_per_share TEXT NOT NULL DEFAULT '1',
                economic_interest_per_share TEXT NOT NULL DEFAULT '1',
                rights_json TEXT NOT NULL DEFAULT '{}',
                created_epoch INTEGER NOT NULL DEFAULT (unixepoch()),
                updated_epoch INTEGER NOT NULL DEFAULT (unixepoch())
            )
        """)
        self.execute("""
            CREATE TABLE IF NOT EXISTS organization_share_issuances (
                legacy_product_code TEXT PRIMARY KEY,
                organization_code TEXT NOT NULL REFERENCES organizations(organization_code),
                class_name TEXT NOT NULL,
                share_class TEXT NOT NULL CHECK(share_class IN ('common','preferred')),
                voting_rights_per_share TEXT NOT NULL DEFAULT '1',
                economic_interest_per_share TEXT NOT NULL DEFAULT '1',
                rights_json TEXT NOT NULL DEFAULT '{}',
                created_epoch INTEGER NOT NULL DEFAULT (unixepoch()),
                updated_epoch INTEGER NOT NULL DEFAULT (unixepoch())
            )
        """)
        self.execute("CREATE INDEX IF NOT EXISTS idx_org_share_issuance_org ON organization_share_issuances(organization_code,legacy_product_code)")
        self.execute("""
            CREATE TABLE IF NOT EXISTS organization_groups (
                group_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                representative_organization_code TEXT REFERENCES organizations(organization_code),
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
                created_epoch INTEGER NOT NULL DEFAULT (unixepoch()),
                updated_epoch INTEGER NOT NULL DEFAULT (unixepoch())
            )
        """)
        self.execute("""
            CREATE TABLE IF NOT EXISTS organization_group_members (
                membership_id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL REFERENCES organization_groups(group_id),
                organization_code TEXT NOT NULL REFERENCES organizations(organization_code),
                joined_epoch INTEGER NOT NULL DEFAULT (unixepoch()),
                left_epoch INTEGER,
                CHECK(left_epoch IS NULL OR left_epoch >= joined_epoch)
            )
        """)
        self.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_active_organization_group_member
                       ON organization_group_members(organization_code) WHERE left_epoch IS NULL""")
        self.execute("CREATE INDEX IF NOT EXISTS idx_org_group_members_active ON organization_group_members(group_id,organization_code) WHERE left_epoch IS NULL")
        self.execute("""
            CREATE TABLE IF NOT EXISTS organization_size_settings (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                announcement_seconds INTEGER NOT NULL DEFAULT 0 CHECK(announcement_seconds>=0),
                rule_json TEXT NOT NULL DEFAULT '{}',
                updated_epoch INTEGER NOT NULL DEFAULT (unixepoch())
            )
        """)
        self.execute("INSERT OR IGNORE INTO organization_size_settings(singleton) VALUES (1)")
        self._import_legacy_companies()
        self._migrate_share_issuance_identity()
        self._install_company_sync_triggers()

    def _migrate_share_issuance_identity(self):
        """Give every existing ticker-backed issuance a stable independent ID."""
        self.execute(
            "INSERT OR IGNORE INTO share_issuances(organization_code,legacy_product_code,class_name,share_class,"
            "voting_rights_per_share,economic_interest_per_share,rights_json,created_epoch,updated_epoch) "
            "SELECT organization_code,legacy_product_code,class_name,share_class,voting_rights_per_share,"
            "economic_interest_per_share,rights_json,created_epoch,updated_epoch FROM organization_share_issuances"
        )
        self.execute(
            "UPDATE company SET share_issuance_id=(SELECT issuance_id FROM share_issuances s "
            "WHERE s.legacy_product_code=company.company_code) WHERE share_issuance_id IS NULL "
            "AND EXISTS(SELECT 1 FROM share_issuances s WHERE s.legacy_product_code=company.company_code)"
        )

    def _import_legacy_companies(self):
        """Create profile/class links idempotently; never rewrite legacy share data."""
        rows = self.execute(
            "SELECT company_code,name,industry,share_class,parent_company_code,"
            "dividend_priority,cumulative_dividend,dividend_mode,dividend_per_share,"
            "dividend_percent,liquidation_value_per_share,dividend_funding,"
            "dividend_cycle_seconds,dividend_is_active,status,"
            "(SELECT nationality FROM market_exchanges WHERE exchange_id=company.exchange_id) "
            "FROM company ORDER BY company_code"
        )
        for row in rows:
            if str(row[3] or "common") != "common":
                continue
            code = str(row[0])
            self.execute(
                "INSERT OR IGNORE INTO organizations(organization_code,name,industry,country_code,status) "
                "VALUES (?,?,?,?,?)",
                (code, str(row[1] or ""), str(row[2] or ""), row[15], str(row[14] or "active")),
            )
        for row in rows:
            code = str(row[0])
            share_class = str(row[3] or "common")
            organization_code = str(row[4] or "") if share_class == "preferred" else code
            if not organization_code:
                continue
            exists = self.execute_one(
                "SELECT 1 FROM company WHERE company_code=? AND share_class='common'",
                (organization_code,),
            )
            if not exists:
                continue
            exists = self.execute_one("SELECT 1 FROM organizations WHERE organization_code=?", (organization_code,))
            if not exists:
                continue
            rights = {
                "dividend_priority": int(row[5] or 0),
                "cumulative_dividend": bool(row[6]),
                "dividend_mode": str(row[7] or "fixed"),
                "dividend_per_share": str(row[8] or 0),
                "dividend_percent": str(row[9] or 0),
                "liquidation_value_per_share": str(row[10] or 0),
                "dividend_funding": str(row[11] or "treasury"),
                "dividend_cycle_seconds": int(row[12] or 0),
                "dividend_is_active": bool(row[13]),
            }
            self.execute(
                "INSERT OR IGNORE INTO organization_share_issuances "
                "(legacy_product_code,organization_code,class_name,share_class,"
                "voting_rights_per_share,rights_json) VALUES (?,?,?,?,?,?)",
                (code, organization_code,
                 "보통주" if share_class == "common" else f"{row[1] or code} 우선주",
                 share_class, "1", json.dumps(rights, ensure_ascii=False, sort_keys=True)),
            )

    def _install_company_sync_triggers(self):
        """Keep new legacy catalog rows discoverable until trading is migrated."""
        self.execute("DROP TRIGGER IF EXISTS organization_company_after_update")
        self.execute("""
            CREATE TRIGGER IF NOT EXISTS organization_company_after_insert
            AFTER INSERT ON company BEGIN
              INSERT OR IGNORE INTO organizations(organization_code,name,industry,country_code,status)
              SELECT NEW.company_code,COALESCE(NEW.name,''),COALESCE(NEW.industry,''),
                (SELECT nationality FROM market_exchanges WHERE exchange_id=NEW.exchange_id),COALESCE(NEW.status,'active')
              WHERE COALESCE(NEW.share_class,'common')='common';
              INSERT OR IGNORE INTO share_issuances
                (legacy_product_code,organization_code,class_name,share_class,voting_rights_per_share)
              SELECT NEW.company_code,NEW.company_code,COALESCE(NEW.name,NEW.company_code)||' 보통주','common','1'
              WHERE COALESCE(NEW.share_class,'common')='common';
              INSERT OR IGNORE INTO share_issuances
                (legacy_product_code,organization_code,class_name,share_class,voting_rights_per_share)
              SELECT NEW.company_code,NEW.parent_company_code,COALESCE(NEW.name,NEW.company_code)||' 우선주','preferred','1'
              WHERE NEW.share_class='preferred' AND NEW.parent_company_code IS NOT NULL
                AND EXISTS(SELECT 1 FROM organizations WHERE organization_code=NEW.parent_company_code);
              UPDATE company SET share_issuance_id=(SELECT issuance_id FROM share_issuances WHERE legacy_product_code=NEW.company_code)
                WHERE company_code=NEW.company_code AND NEW.share_issuance_id IS NULL;
              INSERT OR IGNORE INTO organization_share_issuances
                (legacy_product_code,organization_code,class_name,share_class,voting_rights_per_share)
              SELECT legacy_product_code,organization_code,class_name,share_class,voting_rights_per_share
                FROM share_issuances WHERE legacy_product_code=NEW.company_code;
            END
        """)
        self.execute("""
            CREATE TRIGGER IF NOT EXISTS organization_company_after_update
            AFTER UPDATE OF name,industry,status,exchange_id,dividend_priority,cumulative_dividend,dividend_mode,
              dividend_per_share,dividend_percent,dividend_cycle_seconds,dividend_is_active,
              liquidation_value_per_share,dividend_funding ON company BEGIN
              UPDATE organizations SET name=COALESCE(NEW.name,''),industry=COALESCE(NEW.industry,''),
                country_code=(SELECT nationality FROM market_exchanges WHERE exchange_id=NEW.exchange_id),
                status=COALESCE(NEW.status,'active'),updated_epoch=unixepoch()
              WHERE organization_code=NEW.company_code AND COALESCE(NEW.share_class,'common')='common';
              UPDATE share_issuances SET rights_json=json_object('dividend_priority',NEW.dividend_priority,
                  'cumulative_dividend',NEW.cumulative_dividend,'dividend_mode',NEW.dividend_mode,
                  'dividend_per_share',CAST(NEW.dividend_per_share AS TEXT),
                  'dividend_percent',CAST(NEW.dividend_percent AS TEXT),
                  'liquidation_value_per_share',CAST(NEW.liquidation_value_per_share AS TEXT),
                  'dividend_funding',NEW.dividend_funding,
                  'dividend_cycle_seconds',NEW.dividend_cycle_seconds,
                  'dividend_is_active',NEW.dividend_is_active),
                updated_epoch=unixepoch()
              WHERE issuance_id=NEW.share_issuance_id;
            END
        """)

