"""Home-currency summaries, while contract details retain their actual denominations."""
from decimal import Decimal, ROUND_FLOOR
from managers.fx_manager import SCALE, display_units

FIELDS={
    '💵 현금':'cash','현금':'cash','🏦 통장 잔액':'account_total','통장 잔액':'account_total',
    '🌐 외환통장 평가액':'foreign_accounts','외환 평가액':'foreign_accounts',
    '📈 투자자산 합계':'investment_total','투자자산 평가액':'stock_total','국채 평가액':'bond_total',
    '🧾 ELS 인수 원금':'els_total','ELS 청약·보유 원금':'els_total',
    '💰 미지급 배당 명목액':'preferred_claim_total','미지급 배당 명목액':'preferred_claim_total',
    '🔒 매수 주문 예치금':'order_reserved_total','매수 주문 예치금':'order_reserved_total',
    '🔐 풋 옵션 현금담보':'option_collateral_cash','풋 옵션 현금담보':'option_collateral_cash',
    '📄 옵션 작성 의무 평가':'option_short_liability','옵션 작성 의무 평가':'option_short_liability',
    '🏆 총 자산':'grand_total','총자산':'grand_total','💳 대출 잔액':'loan_total','대출 잔액':'loan_total',
    '🧾 순자산':'net_worth','순자산':'net_worth',
}

def native_summary(embed,info):
    if 'home_cash_units' not in info:return embed
    home=info['national_currency'];rate=Decimal(str(info['home_rate_nwx']))
    for i,field in enumerate(embed.fields):
        key=FIELDS.get(field.name)
        if key is None:continue
        if key=='cash':units=info['home_cash_units']
        elif key=='account_total':units=int(Decimal(str(info.get('home_account_total_home',0)))*SCALE)
        elif key=='foreign_accounts':units=int(Decimal(str(info['foreign_account_total_home']))*SCALE)
        else:
            amount=sum(Decimal(str(info.get(k,0))) for k in ('stock_total','bond_total','els_total')) if key=='investment_total' else Decimal(str(info.get(key,0)))
            units=int((amount/rate*SCALE).to_integral_value(rounding=ROUND_FLOOR))
        suffix=field.value.split('```',2)[-1] if field.value.count('```')>=2 else ''
        sign='−' if key=='option_short_liability' and units else ''
        embed.set_field_at(i,name=field.name,value=f'```{sign}{display_units(units,info.get('home_decimal_places',6))} {home}```'+suffix,inline=field.inline)
    embed.add_field(name='💱 외화 현금 평가액',value=f"```{display_units(info['foreign_cash_units'],info.get('home_decimal_places',6))} {home}```",inline=True)
    return embed

def wallet_lines(info):
    return [f"• **{row['currency_code']}** · {display_units(row['balance_units'],row.get('decimal_places'))} {row['currency_code']} · {'외환' if row['is_foreign'] else '기본화폐'}" for row in info.get('wallets',[])]


class MoneyDisplay:
    """Snapshot quote/precision once per response, then format without further queries."""
    def __init__(self,db,user_id):
        from managers.currency_manager import CurrencyManager
        with db.readonly() as conn:
            self.relative_currency=CurrencyManager.home(conn,user_id)
            self.places={r['code']:int(r['decimal_places']) for r in conn.execute("SELECT code,decimal_places FROM currencies WHERE status='active'")}
            self.nwx_quote=CurrencyManager.quote(conn,'NWX',self.relative_currency)

    def relative(self,value,source='NWX'):
        from fractions import Fraction
        if source!='NWX':raise ValueError('이 요약 값은 NWX 평가액이어야 합니다.')
        amount=Fraction(str(value))*self.nwx_quote
        number=Decimal(amount.numerator)/Decimal(amount.denominator)
        return f"{number:,.{self.places[self.relative_currency]}f} {self.relative_currency}"

    def text(self,text,relative=True):
        import re
        from fractions import Fraction
        if not text:return text
        pattern=r'(?<![\w.])([−-]?\d[\d,]*(?:\.\d+)?)[ \t]+('+ '|'.join(re.escape(k) for k in self.places) +r')\b'
        def money(match):
            amount=match[1].replace(',','').replace('−','-');code=match[2]
            if relative and code=='NWX':return self.relative(amount)
            return f"{Decimal(amount):,.{self.places[code]}f} {code}"
        return re.sub(pattern,money,str(text))

    def embed(self,embed,relative=True):
        if embed is None:return None
        result=embed.copy()
        if result.description:result.description=self.text(result.description,relative)
        for i,field in enumerate(result.fields):
            result.set_field_at(i,name=field.name,value=self.text(field.value,relative),inline=field.inline)
        return result


def relative_embed(method):
    from functools import wraps
    @wraps(method)
    def formatted(self,*args,**kwargs):
        embed=method(self,*args,**kwargs)
        display=getattr(self,'money_display',None)
        return display.embed(embed) if display else embed
    return formatted
