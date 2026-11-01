"""Recorded shareholder decisions and separately authorized administrator execution."""
from decimal import Decimal
import time

from functions.money import base_currency_code, currency_precision, to_minor_units, from_minor_units


class ShareRestructuringManager:
    def __init__(self, bot):
        self.bot, self.db = bot, bot.db

    @staticmethod
    def _company(conn, code):
        row = conn.execute("SELECT * FROM company WHERE company_code=? AND status='active'", (code,)).fetchone()
        if not row or row['share_class'] != 'common':
            raise ValueError('거래 중인 보통주 법인을 선택해주세요.')
        return row

    def record(self, actor, code, ratio, decision, meeting_reference, articles_reference, par_value):
        from managers.corporate_actions_manager import CorporateActionsManager as Actions
        ratio = Actions._integer(ratio, '분할 비율', 2)
        if decision not in {'approved', 'rejected'}:
            raise ValueError('주총 의결 결과는 가결 또는 부결로 등록해주세요.')
        references = [str(value or '').strip() for value in (meeting_reference, articles_reference)]
        if any(not value or len(value) > 1000 for value in references):
            raise ValueError('주총 의사록과 정관 변경 문서의 참조를 각각 1~1000자로 입력해주세요.')
        with self.db.atomic() as conn:
            Actions._require(conn, actor)
            company = self._company(conn, code)
            currency = company['quote_currency'] or base_currency_code(conn)
            places = currency_precision(conn, currency)
            units = to_minor_units(par_value, places)
            if units <= 0 or from_minor_units(units, places) != Decimal(str(par_value)):
                raise ValueError('액면가는 통화 최소단위에 맞는 양수여야 합니다.')
            if units % ratio:
                raise ValueError('분할 후 액면가가 통화 최소단위로 정확히 나누어떨어져야 합니다.')
            if Decimal(str(company['capital'])) != from_minor_units(units, places) * int(company['total_shares']):
                raise ValueError('발행주식 수 × 현재 액면가가 자본금과 일치해야 합니다.')
            if company['par_value_minor_units'] is not None and int(company['par_value_minor_units']) != units:
                raise ValueError('등록된 현재 액면가와 다릅니다.')
            if conn.execute("SELECT 1 FROM company WHERE parent_company_code=? AND status='active' LIMIT 1", (code,)).fetchone():
                raise ValueError('연결 우선주가 있는 법인은 전체 주식 종류의 액면 조건을 먼저 정리해주세요.')
            cursor = conn.execute("""INSERT INTO shareholder_resolutions
                (company_code,ratio,decision,meeting_reference,articles_reference,par_value_minor_units,
                 capital_snapshot,shares_snapshot,currency_code,recorded_by,recorded_epoch)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (code, ratio, decision, *references, str(units), str(company['capital']),
                 company['total_shares'], currency, str(actor), int(time.time())))
            return cursor.lastrowid

    @staticmethod
    def prepare(conn, company, mode, ratio, resolution_id):
        if mode not in {'par_split', 'stock_split', 'reverse_split', 'ratio_split'}:
            raise ValueError('정식 액면분할·주식분할·액면병합·일반비율분할 중 선택해주세요.')
        par = company.get('par_value_minor_units')
        if mode in {'stock_split', 'ratio_split'} and par is not None:
            raise ValueError('액면가가 등록된 법인은 정식 액면분할을 사용해주세요.')
        if mode != 'par_split':
            if resolution_id is not None:
                raise ValueError('주총 의결 연결은 정식 액면분할에서 선택해주세요.')
            return None
        if conn.execute("SELECT 1 FROM company WHERE parent_company_code=? AND status='active' LIMIT 1", (company['company_code'],)).fetchone():
            raise ValueError('연결 우선주가 있는 법인은 전체 액면 조건을 먼저 정리해주세요.')
        row = conn.execute("""SELECT r.*,e.resolution_id AS used FROM shareholder_resolutions r
            LEFT JOIN share_resolution_executions e USING(resolution_id) WHERE r.resolution_id=?""", (resolution_id,)).fetchone()
        if not row or row['used'] is not None or row['decision'] != 'approved':
            raise ValueError('아직 집행하지 않은 가결 의결을 선택해주세요.')
        currency = company.get('quote_currency') or base_currency_code(conn)
        if row['company_code'] != company['company_code'] or row['ratio'] != ratio:
            raise ValueError('주총 의결의 법인·분할 비율과 집행 요청이 다릅니다.')
        if (row['shares_snapshot'] != company['total_shares'] or
                Decimal(row['capital_snapshot']) != Decimal(str(company['capital'])) or
                row['currency_code'] != currency or
                (par is not None and int(par) != int(row['par_value_minor_units']))):
            raise ValueError('의결 후 발행주식·자본금·통화·액면 조건이 바뀌었습니다. 새 의결을 등록해주세요.')
        return dict(row)

    @staticmethod
    def finish(conn, actor, company, mode, ratio, resolution, record_id, now):
        par = company.get('par_value_minor_units')
        if resolution:
            par = int(resolution['par_value_minor_units']) // ratio
            conn.execute("INSERT INTO share_resolution_executions VALUES (?,?,?,?)",
                         (resolution['resolution_id'], record_id, str(actor), now))
        elif mode == 'reverse_split' and par is not None:
            par = int(par) * ratio
        if par is not None:
            conn.execute("UPDATE company SET par_value_minor_units=? WHERE company_code=?", (str(par), company['company_code']))

    def history(self, code, conn=None):
        if conn is None:
            with self.db.readonly() as snapshot:
                return self.history(code, snapshot)
        return [{**dict(row), 'executed': row['executed_epoch'] is not None} for row in conn.execute("""
            SELECT r.*,e.executed_by,e.executed_epoch FROM shareholder_resolutions r
            LEFT JOIN share_resolution_executions e USING(resolution_id)
            WHERE r.company_code=? ORDER BY r.resolution_id DESC""", (code,))]
