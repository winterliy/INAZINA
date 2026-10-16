"""Private financial reports and role-checked administrator accounting forms."""
import asyncio
import json
from decimal import Decimal
import discord
from functions.interaction_checks import owner_interaction_check
from database.financial_db import ACCOUNTS
from managers.financial_manager import FinancialManager, month

PAGES = [('main','🏢 메인'),('balance','📊 재무상태표'),('income','💹 손익계산서'),
         ('cash','💵 현금흐름표'),('equity','🏦 자본변동표'),('liquidity','💧 유동성비율'),
         ('profitability','📈 수익성비율'),('stability','🛡️ 안정성비율'),('activity','🔄 활동성비율'),('notes','📝 주석')]


class FinancialReportView(discord.ui.View):
    def __init__(self, db, owner, report, periods=()):
        super().__init__(timeout=300)
        self.db,self.owner,self.report = db,str(owner),report
        self.page='main'
        self.sections=discord.ui.Select(placeholder='재무 보고서',options=[discord.SelectOption(label=v,value=k) for k,v in PAGES],row=0)
        self.sections.callback=self.change_page
        self.add_item(self.sections)
        months=sorted(set([month(),report['period'],*periods]),reverse=True)[:25]
        self.period_select=discord.ui.Select(placeholder='조회 월',options=[discord.SelectOption(label=p,value=p,default=p==report['period']) for p in months],row=1)
        self.period_select.callback=self.change_period
        self.add_item(self.period_select)

    async def interaction_check(self, interaction):
        return await owner_interaction_check(
            interaction, self.owner, '명령어를 실행한 사용자만 조작할 수 있습니다.'
        )

    async def change_page(self, interaction):
        self.page=self.sections.values[0]
        await interaction.response.edit_message(embed=self.build_embed(),view=self)

    async def change_period(self, interaction):
        await interaction.response.defer()
        try:
            self.report=await asyncio.to_thread(FinancialManager(self.db).report,self.report['code'],self.period_select.values[0])
        except ValueError as exc:
            await interaction.followup.send(str(exc),ephemeral=True)
            return
        await interaction.edit_original_response(embed=self.build_embed(),view=self)

    def money(self, amount):
        return f'{Decimal(amount)/(10**self.report["places"]):,.{self.report["places"]}f} {self.report["currency"]}'

    def build_embed(self):
        r=self.report; b=r['balances']; s=r['summary']; inc=r['income']
        title=dict(PAGES)[self.page]
        embed=discord.Embed(title=f'{title} · {discord.utils.escape_markdown(str(r["name"]))[:100]}',
            description=f'종목 `{r["code"]}` · **{r["period"]}** · {"결산 완료" if r["closed"] else "잠정 집계"}',color=discord.Color.blue())
        def field(name,value,inline=True):embed.add_field(name=name,value=f'```\n{value}\n```',inline=inline)
        def amount(name,value):field(name,self.money(value))
        if self.page=='main':
            for name,value in [('총자산',s['assets']),('총부채',s['liabilities']),('자기자본',s['equity']),('매출액',inc['revenue']),('영업이익',inc['operating']),('당기순이익',inc['profit'])]:amount(name,value)
            field('기록 범위',f'이번 달 분개 {r["entry_count"]}건 · 기록된 거래만 집계',False)
            if b.get('opening_adjustment'):field('기초자료 주의','도입 전 미확인 잔액이 포함되어 있습니다. 주석을 확인해주세요.',False)
            if r['checks']['live_cash'] is False:field('현금 대사 불일치','실제 법인 현금과 원장이 다릅니다. 관리자 확인이 필요합니다.',False)
        elif self.page=='balance':
            for category,label,sign in [('current_asset','유동자산',1),('noncurrent_asset','비유동자산',1),('current_liability','유동부채',-1),('noncurrent_liability','비유동부채',-1),('equity','자본',-1)]:
                rows=[f'{name}: {self.money(sign*b.get(k,0))}' for k,(name,cat) in ACCOUNTS.items() if cat==category and b.get(k,0)]
                if category=='equity':rows.append(f'누적 원장 손익: {self.money(s["cumulative_profit"])}')
                field(label,'\n'.join(rows) or '기록 없음',False)
            field('대차 검증','일치' if r['checks']['balance_sheet'] else '불일치',False)
        elif self.page=='income':
            for name,key in [('매출액','revenue'),('매출총이익','gross'),('영업이익','operating'),('세전순이익','pretax'),('법인세비용','tax'),('당기순이익','profit'),('EBITDA','ebitda')]:amount(name,inc[key])
            expenses='\n'.join(f'{name}: {self.money(r["changes"][k])}' for k,(name,cat) in ACCOUNTS.items() if cat=='expense' and r['changes'][k])
            field('비용 세부',expenses or '기록 없음',False)
            field('주당순이익 (EPS)','산출 불가' if r['eps'] is None else self.money(Decimal(str(r['eps']))))
            field('FFO / AFFO','부동산 모델 미지원',False)
        elif self.page=='cash':
            amount('기초 현금',r['opening'].get('cash',0))
            for key,name in [('opening','도입 기초 현금'),('operating','영업활동'),('investing','투자활동'),('financing','재무활동'),('adjustment','기타 조정')]:amount(name,r['cash_flows'].get(key,0))
            amount('기말 현금',b.get('cash',0))
            field('대사','일치' if r['checks']['cash_flow'] else '불일치')
            amount('CAPEX',r['capex']);amount('잉여현금흐름 (FCF)',r['fcf'])
        elif self.page=='equity':
            amount('기초 자기자본',r['previous']['equity'])
            for k,(name,category) in ACCOUNTS.items():
                if category=='equity' and r['changes'][k]:amount(name+' 변동',-r['changes'][k])
            amount('당기순이익',inc['profit']); amount('기말 자기자본',s['equity'])
        elif self.page=='notes':
            field('적용 기준','\n'.join(r['notes']),False)
            amount('도입 전 미확인 잔액',-b.get('opening_adjustment',0))
            field('보고서 기간','매월 한국시간 기준 · 마감 후 정정은 새 기간의 반대 분개로 기록',False)
        else:
            indices={'liquidity':(0,2),'profitability':(2,11),'stability':(11,18),'activity':(18,24)}
            start,end=indices[self.page]
            for name,item in list(r['ratios'].items())[start:end]:
                value='산출 불가' if item['value'] is None else f'{item["value"]:,.2f}{item["unit"]}'
                field(name,value)
            field('산식 기준','해당 월의 손익 / 평균 잔액 · 분모 0 이하 또는 평균 자료 부재 시 미산출',False)
        for option in self.sections.options:option.default=option.value==self.page
        for option in self.period_select.options:option.default=option.value==r['period']
        embed.set_footer(text='INAZINA · 회계 · 회사 기준통화 표시')
        return embed


FINANCIAL_COMMANDS={'재무설정','회계분개','재무정정','결산','재무검증','재무계정'}


class FinancialAdminModal(discord.ui.Modal):
    def __init__(self,db,owner,command):
        super().__init__(title=f'INAZINA · {command}',timeout=300)
        self.db,self.owner,self.command=db,str(owner),command
        self.submitted=False
        self.code=discord.ui.TextInput(label='회사 또는 우선주 product_id',max_length=100)
        self.add_item(self.code)
        if command=='재무설정':
            self.config=discord.ui.TextInput(label='월간 영업 설정 JSON (회사 지정통화)',style=discord.TextStyle.paragraph,default='{"enabled":false,"revenue":0,"cogs":0,"sga":0,"depreciation":0,"tax_percent":0}',max_length=3500)
            self.add_item(self.config)
        elif command=='회계분개':
            self.note=discord.ui.TextInput(label='거래 설명',max_length=300)
            self.lines=discord.ui.TextInput(label='분개 JSON: 차변 양수 / 대변 음수',style=discord.TextStyle.paragraph,
                default='[{"account":"receivables","amount":1000},{"account":"revenue","amount":-1000}]',max_length=3500)
            self.add_item(self.note);self.add_item(self.lines)
            self.flow=discord.ui.TextInput(label='현금흐름: none/operating/investing/financing',default='none',max_length=20)
            self.add_item(self.flow)
        elif command=='재무정정':
            self.ident=discord.ui.TextInput(label='정정할 관리자 분개 번호',max_length=20)
            self.note=discord.ui.TextInput(label='정정 사유',max_length=300)
            self.add_item(self.ident);self.add_item(self.note)
        elif command=='결산':
            self.period=discord.ui.TextInput(label='마감할 월 YYYY-MM',max_length=7)
            self.add_item(self.period)

    async def on_submit(self,interaction):
        if str(interaction.user.id)!=self.owner or self.submitted:
            await interaction.response.send_message('본인만 한 번 제출할 수 있습니다.',ephemeral=True);return
        self.submitted=True
        await interaction.response.defer(ephemeral=True)
        mgr=FinancialManager(self.db)
        def execute():
            with self.db.readonly() as conn:mgr.require(conn,self.owner)
            if self.command=='재무설정':return mgr.configure(self.owner,self.code.value,json.loads(self.config.value))
            if self.command=='회계분개':return mgr.manual(self.owner,self.code.value,self.note.value,json.loads(self.lines.value),self.flow.value.strip())
            if self.command=='재무정정':return mgr.manual(self.owner,self.code.value,self.note.value,[],reversal=int(self.ident.value))
            if self.command=='결산':return mgr.close_period(self.owner,self.code.value,self.period.value)
            report=mgr.report(self.code.value)
            return '\n'.join(f'{k}: {"미지원" if v is None else "일치" if v else "불일치"}' for k,v in report['checks'].items())
        try:result=await asyncio.to_thread(execute)
        except (ValueError,KeyError,TypeError) as exc:
            self.submitted=False
            result=f'❌ {exc}'
        await interaction.followup.send(embed=discord.Embed(title=f'📒 {self.command}',description=str(result)[:4000],color=discord.Color.blue()),ephemeral=True)


class FinancialAdminView(discord.ui.View):
    def __init__(self,db,owner,command):
        super().__init__(timeout=300)
        self.db,self.owner,self.command=db,str(owner),command

    @discord.ui.button(label='회계 입력창 열기',style=discord.ButtonStyle.primary)
    async def open_modal(self,interaction,button):
        if str(interaction.user.id)!=self.owner:
            await interaction.response.send_message('본인만 사용할 수 있습니다.',ephemeral=True);return
        def check():
            with self.db.readonly() as conn:FinancialManager.require(conn,self.owner)
        try:await asyncio.to_thread(check)
        except ValueError as exc:
            await interaction.response.send_message(str(exc),ephemeral=True);return
        await interaction.response.send_modal(FinancialAdminModal(self.db,self.owner,self.command))
