"""Hidden $ commands launch owner-checked buttons, then administrator modals."""
import asyncio
import json
import io
import logging
import sqlite3
import discord
from database.user_db import TREASURY_USER_ID
from database.db_manager import DBManager
from functions.admin_views import MarketEventModal
from functions.admin_embeds import admin_embed, admin_response, admin_help_embed
from managers.admin_control_manager import ENTITIES, EXAMPLES
from managers.fx_manager import display_units

ALIASES = {'주식':'stock','상품등록':'asset','상품':'asset','국채':'bond','회사채':'corporatebond','선물':'future',
           '원자재':'commodity','펀드':'fund','인덱스펀드':'indexfund','옵션':'option','저축':'saving','대출상품':'loan',
           '외환상품':'fxproduct','통화':'currency','변동성':'volatility','경제':'settings','호재':'사건','악재':'사건',
           '국적':'user','nationality':'user','복권':'lottery'}
# Prefix trades always use the treasury identity, never the administrator wallet.
NATIONAL = {
    '매수': [('종목 코드','INZST001'),('수량','1'),('지정가 (종목 결제 통화)','1120')],
    '매도': [('종목 코드','INZST001'),('수량','1'),('지정가 (종목 결제 통화)','1120')],
    '호가': [('종목 코드','INZST001')],
    '내지갑': [],
    '내주문': [],
    '주문취소': [('주문 번호','')],
    '송금': [('수신자 ID 또는 계좌',''),('금액 (출금 통화 기준)','1000'),('출금계좌 (외환 송금은 비우면 지갑)','')],
    '계좌개설': [('상품 코드','INZSV001'),('비밀번호 (외환통장은 비우면)',''),('예치금 NWX (외환통장은 0)','0')],
    '계좌해지': [('계좌 번호',''),('비밀번호',''),('입금계좌 (비우면 지갑)','')],
    '대출': [('상품 코드','INZLN001'),('금액 NWX','1000')],
    '대출연장': [('대출 번호 또는 대출 상품 id','INZLN001')],
    '대출상환': [('상품 코드',''),('상환액 NWX','1000'),('대출 번호 (선택)','')],
    '펀드가입': [('펀드 코드','INZMF001'),('좌수','1')],
    '펀드환매': [('펀드 코드','INZMF001'),('좌수','1')],
    '선물증거금': [('선물 코드','INZFU001'),('금액 NWX','1000')],
    '옵션작성': [('설정 JSON',json.dumps(EXAMPLES['option'],ensure_ascii=False))],
    '옵션체인': [('기초주식 (비우면 전체)','')],
    '국채매수': [('국채 코드','INZTB001'),('금액 NWX','1000')],
    '환전': [('출금 통화','NWX'),('입금 통화','USD'),('출금 금액','10000')],
    '내외환': [],
    '도박': [('금액 NWX','1000')],
    '환율': [],
    '기준금리': [],
    '법정이자': [],
    '검색': [],
    '그래프': [('종목 코드','INZST001')],
    '전체자산확인': [],
    '국적확인': [],
    '국채목록': [],
    '주총': [('주식 코드','INZST001'),('장소','국가 회의실')],
}
READ_ONLY = {command for command,fields in NATIONAL.items() if not fields}

def admin_allowed(bot,user_id):
    user = bot.db.users.get_user(str(user_id))
    return bool(user and str(user['role']).strip().lower()=='admin')

class CheckedModal(discord.ui.Modal):
    def __init__(self,bot,owner,title):
        super().__init__(title=title[:45],timeout=300)
        self.bot,self.owner,self.submitted = bot,str(owner),False

    async def interaction_check(self,interaction):
        allowed = str(interaction.user.id)==self.owner and admin_allowed(self.bot,self.owner) and not self.submitted
        if not allowed:
            await interaction.response.send_message(**admin_response('❌ 창을 연 role=admin 관리자만 한 번 제출할 수 있습니다.'),ephemeral=True)
        return allowed

    async def on_error(self,interaction,error):
        logging.getLogger(__name__).error('Administrator modal failed: %s',self.title,
            exc_info=(type(error),error,error.__traceback__))
        text = '❌ 설정을 처리하지 못했습니다. 입력값과 DB 상태를 확인해주세요.'
        if interaction.response.is_done():
            await interaction.followup.send(**admin_response(text),ephemeral=True)
        else:
            await interaction.response.send_message(**admin_response(text),ephemeral=True)

    def field(self,label,default='',required=True,long=False):
        item = discord.ui.TextInput(label=label,default=str(default),required=required,
            style=discord.TextStyle.paragraph if long else discord.TextStyle.short,max_length=3500 if long else 100)
        self.add_item(item)
        return item

class EntityModal(CheckedModal):
    def __init__(self,bot,owner,category,operation):
        super().__init__(bot,owner,f'Inazina {category} · {operation}')
        self.category,self.operation = category,operation
        self.code = self.field('종목/상품 코드 (옵션 신규 작성은 자동 생성)','',category!='option' or operation!='create')
        if operation in {'create','edit'}:
            self.name = self.field('이름 (수정 시 비우면 유지)','',False)
            self.price = self.field('가격 NWX (수정 시 비우면 유지)','',False)
            self.supply = self.field('발행량 (수정 시 비우면 유지)','',False)
            self.extra = self.field('추가 설정 JSON (수정할 필드만 입력)',
                json.dumps(EXAMPLES[category] if operation=='create' else {},ensure_ascii=False),True,True)
        else:
            self.confirm = self.field('확인: 중지 또는 재개를 입력','')

    async def on_submit(self,interaction):
        if not await self.interaction_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        self.submitted = True
        try:
            if self.operation in {'delete','restore'}:
                if self.confirm.value.strip()!=('중지' if self.operation=='delete' else '재개'):
                    raise ValueError('확인란에 정확히 중지/재개를 입력해주세요.')
                message = await asyncio.to_thread(self.bot.admin_control.entity,self.owner,self.category,self.operation,self.code.value)
            else:
                extra = json.loads(self.extra.value or '{}')
                if not isinstance(extra,dict):
                    raise ValueError('추가 설정은 JSON 객체여야 합니다.')
                message = await asyncio.to_thread(self.bot.admin_control.entity,self.owner,self.category,self.operation,
                    self.code.value,self.name.value,self.price.value,self.supply.value,extra)
        except (ValueError,TypeError,OverflowError,sqlite3.IntegrityError) as exc:
            self.submitted = False
            message = f'❌ {exc}'
        await interaction.followup.send(**admin_response(message,self.title),ephemeral=True)

class ActionModal(CheckedModal):
    def __init__(self,bot,owner,command,guild=None):
        super().__init__(bot,owner,f'Inazina ${command}')
        self.command,self.guild = command,guild
        if command in NATIONAL:
            fields = NATIONAL[command]
        else:
            fields = {
                'setup':[('사용자 ID',''),('닉네임',''),('국적','낙원')],
                'register':[('확인: 이 서버를 입력','')],
                '기본소득':[('1인당 지급액 NWX','1000')],
                'settings':[('경제 설정 JSON','{"market_volatility":0.3}')],
                'volatility':[('변동성·GARCH·자동 목표 설정 JSON','{"market_volatility":0.3,"auto_target_noise_multiplier":6}')],
                'user':[('사용자 ID',''),('수정 JSON','{"nationality":"낙원"}')],
                '국고':[('국고 현금 설정액 NWX',''),('사유','')],
                'catalog':[('확인: 기본상품을 입력','')],
                'lottery':[('다음 회차 설정 JSON','{"ticket_price":1000,"max_tickets":100,"prize_percent":70,"treasury_percent":20}')],
            }[command]
        self.fields = [self.field(label,default,not ('비우면' in label or '선택' in label), 'JSON' in label) for label,default in fields]

    async def on_submit(self,interaction):
        if not await self.interaction_check(interaction):
            return
        self.submitted = True
        await interaction.response.defer(ephemeral=True)
        try:
            values = [field.value.strip() for field in self.fields]
            self.bot.admin_control.require(self.owner)
            if self.command in {'검색','그래프','전체자산확인'}:
                from functions.discord_views import InfoPanelView,PriceMovementView,AllAssetsView
                if self.command=='전체자산확인':
                    rows=await asyncio.to_thread(self.bot.db.users.execute,"SELECT user_id,nickname FROM users WHERE status='active' ORDER BY nickname")
                    view=AllAssetsView(self.bot,self.owner,rows)
                    await interaction.followup.send(embed=await asyncio.to_thread(view.build_embed),view=view,ephemeral=True)
                    return
                view = (InfoPanelView(self.bot,self.owner,subject_id=TREASURY_USER_ID) if self.command=='검색'
                        else PriceMovementView(self.bot,self.owner,values[0]))
                if self.command=='검색':
                    await interaction.followup.send(embed=await asyncio.to_thread(view.build_embed),view=view,ephemeral=True)
                else:
                    embed,png = await asyncio.to_thread(view.render)
                    file = discord.File(io.BytesIO(png),filename='movement.png')
                    await interaction.followup.send(embed=embed,file=file,view=view,ephemeral=True)
                return
            if self.command=='주총':
                await self.national_meeting(interaction,values)
                return
            if self.command=='register':
                if values[0]!='이 서버' or self.guild is None:
                    raise ValueError('서버에서 실행하고 확인란에 이 서버를 입력해주세요.')
                members = [member async for member in self.guild.fetch_members(limit=None)]
                result = await asyncio.to_thread(self.bot.admin_control.register,self.owner,members)
            else:
                result = await asyncio.to_thread(self.execute,values)
            if self.command=='내지갑':
                view = TreasuryAssetsView(self.bot,self.owner,json.loads(result))
                await interaction.followup.send(embed=view.build_embed(),view=view,ephemeral=True)
            elif self.command=='환율':
                from functions.discord_views import FXRatesView
                view = FXRatesView(self.bot,self.owner,subject_id=TREASURY_USER_ID)
                await interaction.followup.send(embed=await asyncio.to_thread(view.build_embed),view=view,ephemeral=True)
            else:
                await interaction.followup.send(**admin_response(result,f'Inazina · ${self.command}'),ephemeral=True)
        except (ValueError,TypeError,OverflowError,KeyError,sqlite3.IntegrityError) as exc:
            self.submitted = False
            await interaction.followup.send(**admin_response(f'❌ {exc}',self.title),ephemeral=True)

    def execute(self,v):
        with DBManager.admin_scope(self.owner):
            return self._execute(v)

    def _execute(self,v):
        bot,c,uid = self.bot,self.command,TREASURY_USER_ID
        bot.admin_control.require(self.owner)
        if c=='setup':
            if not v[0].isdecimal():
                raise ValueError('Discord 사용자 ID를 입력해주세요.')
            result = bot.admin_control.setup(self.owner,v[0],v[1],v[2])
        elif c=='기본소득':
            result = bot.admin_mgr.distribute_basic_income(v[0])
        elif c in {'settings','volatility'}:
            return bot.admin_control.settings(self.owner,json.loads(v[0]))
        elif c=='user':
            return bot.admin_control.user(self.owner,v[0],json.loads(v[1]))
        elif c=='국고':
            amount = bot.admin_control._number(v[0],-2**53+1,2**53-1,integer=True)
            with bot.db.atomic() as conn:
                bot.admin_control.require(self.owner,conn)
                conn.execute('UPDATE users SET cash=? WHERE user_id=?',(amount,uid))
                bot.admin_control._audit(conn,self.owner,'treasury.set',{'amount':amount,'reason':v[1]})
            return f'✅ 국고 현금 {amount:,} NWX'
        elif c=='catalog':
            if v[0]!='기본상품':
                raise ValueError('확인란에 기본상품을 입력해주세요.')
            from managers.catalog_manager import CatalogManager
            catalog = CatalogManager(bot.db)
            base_added = catalog.seed()
            foreign_added = catalog.seed_foreign()
            return '✅ 기본·외화 상품을 추가했습니다.' if base_added or foreign_added else 'ℹ️ 기본·외화 상품은 이미 추가되어 있습니다.'
        elif c=='lottery':
            return bot.lottery_mgr.configure(self.owner,json.loads(v[0]))
        elif c in {'매수','매도'}:
            result = bot.order_book.place_limit_order(uid,v[0],'buy' if c=='매수' else 'sell',int(v[1]),int(v[2]))
        elif c=='주문취소':
            result = bot.order_book.cancel_order(uid,int(v[0]))
        elif c=='호가':
            result = bot.order_book.get_order_book(v[0])
        elif c=='내주문':
            result = bot.order_book.get_user_orders(uid)
        elif c=='내지갑':
            result = bot.user_mgr.get_total_assets(uid)
        elif c=='송금':
            result = bot.user_mgr.transfer(uid,v[0],v[1],v[2] or None)
        elif c=='계좌개설':
            if bot.db.fx.execute_one("SELECT 1 FROM fx_products WHERE product_code=? AND status='active'",(v[0],)):
                if int(v[2])!=0:
                    raise ValueError('외환통장 초기 예치금은 0으로 입력하고 $송금으로 입금해주세요.')
                result = '✅ 국가 외환통장: '+bot.fx_mgr.open_account(uid,v[0])
            else:
                result = bot.bank_mgr.open_account(uid,v[0],v[1],int(v[2]))
        elif c=='계좌해지':
            result = bot.bank_mgr.close_account(uid,v[0],v[2] or None,v[1])
        elif c=='대출':
            result = bot.bank_mgr.apply_loan(uid,v[0],int(v[1]))
        elif c=='대출연장':
            result = bot.bank_mgr.extend_loan(uid,v[0])
        elif c=='대출상환':
            result = bot.bank_mgr.repay_loan(uid,v[0],int(v[1]),int(v[2]) if v[2] else None)
        elif c in {'펀드가입','펀드환매'}:
            result = bot.investment_mgr.trade_fund(uid,v[0],int(v[1]),c=='펀드환매')
        elif c=='선물증거금':
            result = bot.futures_mgr.add_margin(uid,v[0],int(v[1]))
        elif c=='옵션작성':
            result = bot.option_mgr.create_and_write(uid,**json.loads(v[0]))
        elif c=='옵션체인':
            result = bot.option_mgr.get_active_contracts(v[0] or None)
        elif c=='국채매수':
            result = bot.treasury_bond_mgr.buy(uid,v[0],int(v[1]))
        elif c=='환전':
            result = bot.fx_mgr.exchange(uid,*v)
        elif c=='내외환':
            result = '\n'.join(f"{r['currency_code']}: {display_units(r['balance_units'])} ({r['value_nwx']:,} NWX)" for r in bot.fx_mgr.accounts(uid)) or '외환통장이 없습니다.'
        elif c=='환율':
            result = '\n'.join(f"1 {r['code']} = {r['rate']:,} NWX" for r in bot.fx_mgr.rates())
        elif c=='국적확인':
            country,currency=bot.fx_mgr.national_currency(uid)
            result=f'국가 국적: {country} · 기본 통화: {currency}'
        elif c=='국채목록':
            result=[dict(r) for r in bot.treasury_bond_mgr.get_active_offerings()]
        elif c=='도박':
            result = bot.user_mgr.gamble(uid,int(v[0]))
        elif c in {'기준금리','법정이자'}:
            result = f"{c}: 연 {bot.rate_mgr.get_rate('base_rate' if c=='기준금리' else 'statutory_rate'):g}%"
        else:
            raise ValueError('지원하지 않는 국가 기능입니다.')
        with bot.db.atomic() as conn:
            # Passwords are intentionally excluded from the audit log.
            bot.admin_control._audit(conn,self.owner,'national.'+c,{'identity':uid,'result':str(result)[:1000]})
        if isinstance(result,tuple) and len(result)==2 and isinstance(result[0],bool):
            return result[1]
        if isinstance(result,(dict,list)):
            return json.dumps(result,ensure_ascii=False,default=lambda r:dict(r),indent=2)
        return result

    async def national_meeting(self,interaction,values):
        from functions.discord_views import ShareholderMeetingResponseView
        bot=self.bot
        bot.admin_control.require(self.owner)
        company=bot.db.companies.get_company(values[0])
        if not company:
            raise ValueError('회사 주식 코드를 확인해주세요.')
        if dict(company).get('share_class')=='preferred':
            raise ValueError('우선주는 의결권이 없습니다. 보통주 법인 id로 주총을 소집해주세요.')
        holders=bot.db.assets.execute("SELECT user_id,SUM(quantity) shares FROM ("
            "SELECT user_id,quantity FROM user_assets WHERE asset_code=? UNION ALL "
            "SELECT writer_id,quantity FROM option_collateral_lots WHERE underlying_code=?) "
            "GROUP BY user_id ORDER BY shares DESC",(values[0],values[0]))
        if not holders or not any(str(r['user_id'])==TREASURY_USER_ID and r['shares']==holders[0]['shares'] for r in holders):
            raise ValueError('국가가 최대주주인 회사만 국가 명의로 소집할 수 있습니다.')
        notice=discord.Embed(title=f"📣 {company['name']} 국가 소집 주주총회",description='국가(국고)가 소집했습니다. 아래 버튼에서 참석 여부를 선택해주세요.')
        notice.add_field(name='장소',value=values[1][:1024],inline=False)
        sent=failed=0
        for holder in holders:
            bot.admin_control.require(self.owner)
            if not str(holder['user_id']).isdecimal():continue
            try:
                target=bot.get_user(int(holder['user_id'])) or await bot.fetch_user(int(holder['user_id']))
                view=ShareholderMeetingResponseView(bot,int(holder['user_id']),int(self.owner),company['name'],values[0],values[1])
                view.message=await target.send(embed=notice,view=view)
                sent+=1
            except discord.HTTPException:
                failed+=1
        with bot.db.atomic() as conn:
            bot.admin_control.require(self.owner,conn)
            bot.admin_control._audit(conn,self.owner,'national.주총',{'code':values[0],'sent':sent,'failed':failed})
        await interaction.followup.send(**admin_response(f'✅ 국가 소집 안내 {sent}명 성공 · {failed}명 실패. 참석 회신은 이 창을 연 관리자에게 전달됩니다.','Inazina · $주총'),ephemeral=True)

class MarketSettingsModal(CheckedModal):
    def __init__(self,bot,owner,category):
        super().__init__(bot,owner,f'Inazina {category} 시장 설정')
        self.category=category
        self.code=self.field('종목 코드')
        self.price=self.field('현재가 NWX (비우면 유지)','',False)
        self.vol=self.field('개별 변동성 0~1 (비우면 시장 설정)','',False)
        self.fixed=self.field('가격 고정: 1 / 자동 변동: 0 (비우면 유지)','',False)
        self.condition=self.field('시장 흐름 bull / bear / flat (비우면 유지)','',False)

    async def on_submit(self,interaction):
        if not await self.interaction_check(interaction):
            return
        self.submitted=True
        await interaction.response.defer(ephemeral=True)
        try:
            extra={'market_volatility':float(self.vol.value) if self.vol.value.strip() else None}
            if self.fixed.value.strip():extra['is_fixed']=int(self.fixed.value)
            if self.condition.value.strip():
                if self.category=='asset':raise ValueError('시장 자산의 흐름은 $settings에서 전체 설정을 변경해주세요.')
                extra['market_condition']=self.condition.value.strip()
            result=await asyncio.to_thread(self.bot.admin_control.entity,self.owner,self.category,'edit',self.code.value,price=self.price.value,extra=extra)
        except (ValueError,TypeError,OverflowError,sqlite3.IntegrityError) as exc:
            self.submitted=False
            result=f'❌ {exc}'
        await interaction.followup.send(**admin_response(result,self.title),ephemeral=True)

class TreasuryAssetsView(discord.ui.View):
    """Private, paginated treasury snapshot without truncating holdings."""
    PAGE_SIZE = 10

    def __init__(self,bot,owner,info):
        super().__init__(timeout=300)
        self.bot,self.owner,self.info,self.page = bot,str(owner),info,0
        self.category='main'
        self.sections={'accounts':[],'fx_accounts':[],'stocks':[],'bonds':[],'written':[],'loans':[]}
        self.labels={'main':'🏛️ 메인','accounts':'🏦 통장','fx_accounts':'💱 외환통장','stocks':'📈 보유 투자상품',
                     'bonds':'📜 국채','written':'✍️ 작성 옵션','loans':'💳 대출'}
        self.items = []
        for item in info['accounts']:
            self.sections['accounts'].append((f"통장 · {item['product_name']}",f"{item['number']}\n잔액 {item['balance']:,} NWX"))
        for item in info.get('fx_accounts',[]):
            self.sections['fx_accounts'].append((f"외환통장 · {item.get('product_name') or item['product_code']}",
                f"{item['account_number']}\n잔액 {display_units(item['balance_units'])} {item['currency_code']} · 평가 {item['value_nwx']:,} NWX"))
        for item in info['stocks']:
            note=f"옵션담보 잠금 · 매도 불가 · {item['collateral_contract_code']}" if item.get('is_collateral') else f"매도 주문 잠금 {item.get('sell_reserved_qty',0):,}"
            self.sections['stocks'].append((item['name'],f"{item['code']}\n수량 {item['qty']:,} · 기준가 {item['price']:,} {item.get('quote_currency','NWX')}\n평가액 {item['value']:,} NWX · {note}"))
        for item in info['bonds']:
            self.sections['bonds'].append((f"국채 · {item['name']}",f"{item['product_code']}\n수량 {item['quantity']:,}매 · 시장가 {item['market_price']:,} NWX\n평가액 {item['market_value']:,} NWX · 액면 {item['principal']:,} NWX"))
        for item in info.get('written_options',[]):
            self.sections['written'].append((f"작성 옵션 · {item['name']}",f"{item['contract_code']} · 체결 {item['outstanding_contracts']:,} / 미체결 {item['offered']:,}계약\n담보 {item['collateral_quantity']:,}주 / {item['collateral_cash']:,} NWX · 만기 <t:{item['expiration_epoch']}:D>"))
        for loan in bot.db.accounts.execute("SELECT l.*,p.name FROM loans l JOIN liability p USING(product_code) WHERE l.user_id=? AND l.status='active'",(TREASURY_USER_ID,)):
            maturity=f"<t:{int(loan['maturity_epoch'])}:D>" if loan['maturity_epoch'] else '없음'
            self.sections['loans'].append((f"대출 · {loan['name']}",f"#{loan['loan_id']} · {loan['product_code']}\n잔액 {loan['outstanding_balance']:,} NWX · 만기 {maturity}"))
        self.category_select=discord.ui.Select(placeholder='국가 지갑 카테고리',row=0,
            options=[discord.SelectOption(label=label,value=key,default=key=='main') for key,label in self.labels.items()])
        self.category_select.callback=self.select_category
        self.add_item(self.category_select)

    async def select_category(self,interaction):
        self.category,self.page=self.category_select.values[0],0
        await interaction.response.edit_message(embed=self.build_embed(),view=self)

    async def interaction_check(self,interaction):
        if str(interaction.user.id)==self.owner and admin_allowed(self.bot,self.owner):
            return True
        await interaction.response.send_message(**admin_response('❌ 이 조회를 실행한 role=admin 관리자만 사용할 수 있습니다.'),ephemeral=True)
        return False

    def build_embed(self):
        info = self.info
        self.items=self.sections.get(self.category,[])
        pages = max(1,(len(self.items)+self.PAGE_SIZE-1)//self.PAGE_SIZE)
        self.page = min(max(0,self.page),pages-1)
        embed = discord.Embed(title=f'🏛️ 국가 내지갑 · {self.labels[self.category]}',color=discord.Color.gold())
        embed.description = f"국적: **{str(info.get('nationality','미설정'))[:100]}** · 기본 통화: **{info.get('national_currency','NWX')}**"
        if self.category=='main':
            for label,key in [('현금','cash'),('통장 잔액','account_total'),('외환 평가액','fx_total'),
                    ('투자자산 평가액','stock_total'),('국채 평가액','bond_total'),('매수 주문 예치금','order_reserved_total'),
                    ('풋 옵션 현금담보','option_collateral_cash'),('옵션 작성 의무 평가','option_short_liability'),
                    ('총자산','grand_total'),('대출 잔액','loan_total'),('순자산','net_worth')]:
                sign='−' if key=='option_short_liability' and info.get(key,0) else ''
                embed.add_field(name=label,value=f"```{sign}{info.get(key,0):,} NWX```",inline=True)
            embed.add_field(name='⭐ 신용점수',value=f"```{info.get('credit_score',0):,}점```",inline=True)
        for name,value in self.items[self.page*self.PAGE_SIZE:(self.page+1)*self.PAGE_SIZE]:
            embed.add_field(name=name[:120],value=value[:300],inline=False)
        if self.category!='main' and not self.items:
            embed.add_field(name='📭 내역',value='이 카테고리에 보유 내역이 없습니다.',inline=False)
        embed.set_footer(text=f'🔒 관리자 전용 · {self.page+1}/{pages} 페이지 · 조회 시점 기준')
        for option in self.category_select.options:option.default=option.value==self.category
        self.previous.disabled = self.page==0
        self.next.disabled = self.page==pages-1
        return embed

    @discord.ui.button(label='◀ 이전',style=discord.ButtonStyle.secondary,row=1)
    async def previous(self,interaction,button):
        self.page -= 1
        await interaction.response.edit_message(embed=self.build_embed(),view=self)

    @discord.ui.button(label='다음 ▶',style=discord.ButtonStyle.secondary,row=1)
    async def next(self,interaction,button):
        self.page += 1
        await interaction.response.edit_message(embed=self.build_embed(),view=self)

class AdminCommandView(discord.ui.View):
    def __init__(self,bot,owner,command,guild=None):
        super().__init__(timeout=300)
        self.bot,self.owner,self.command,self.guild = bot,str(owner),command,guild
        self.operation = 'edit'
        if command in ENTITIES:
            operations=[('생성','create'),('수정','edit'),('삭제 (거래 중지)','delete'),('재개','restore')]
            if command in {'stock','asset','etf','future','corporatebond','fund','indexfund','commodity'}:
                operations.insert(2,('가격·변동성·고정·흐름','market'))
            select = discord.ui.Select(placeholder='상품 작업',options=[discord.SelectOption(label=label,value=value) for label,value in operations])
            async def select_operation(interaction):
                self.operation = select.values[0]
                await interaction.response.defer()
            select.callback = select_operation
            self.add_item(select)
        if command=='help':
            select = discord.ui.Select(placeholder='관리할 분류',options=[discord.SelectOption(label=c,value=c) for c in ENTITIES])
            async def choose(interaction):
                await interaction.response.send_message(**admin_response(f'${select.values[0]} 설정'),view=AdminCommandView(bot,owner,select.values[0],guild),ephemeral=True)
            select.callback = choose
            self.add_item(select)
        else:
            button = discord.ui.Button(label='국가 명의로 조회' if command in READ_ONLY else '국가 명의로 실행' if command in NATIONAL else '설정 창 열기',style=discord.ButtonStyle.primary)
            async def open_modal(interaction):
                if command in READ_ONLY:
                    await ActionModal(bot,owner,command,guild).on_submit(interaction)
                    return
                if command=='사건':
                    assets = await asyncio.to_thread(bot.market_mgr.get_event_assets)
                    modal = MarketEventModal(bot,owner,assets=assets)
                elif command in ENTITIES:
                    modal = MarketSettingsModal(bot,owner,command) if self.operation=='market' else EntityModal(bot,owner,command,self.operation)
                else:
                    modal = ActionModal(bot,owner,command,guild)
                await interaction.response.send_modal(modal)
            button.callback = open_modal
            self.add_item(button)

    async def interaction_check(self,interaction):
        if str(interaction.user.id)==self.owner and admin_allowed(self.bot,self.owner):
            return True
        await interaction.response.send_message(**admin_response('❌ 이 창을 연 role=admin 관리자만 사용할 수 있습니다.'),ephemeral=True)
        return False

NATIONAL_COMMANDS = {'계좌개설','계좌해지','내지갑','내주문','대출','대출연장','대출상환','도움말',
                     '매도','매수','선물증거금','송금','옵션작성','옵션체인','주총','펀드가입','펀드환매','환전'}


class AdminPrefixRegistry:
    def __init__(self,bot):
        self.bot = bot

    @staticmethod
    def help_text():
        from managers.admin_commands_manager import USAGE
        return ('**Inazina 관리자 도움말**\nDB role=admin 전용 · DM 응답 후 입력 채팅 삭제\n\n'
                +'\n'.join(f'${name} {usage}' for name,usage in USAGE.items())
                +'\n$사건 — DM 버튼으로 사건 모달 열기\n\n**국가 명의 일반 기능**\n'
                +' '.join('$'+name for name in sorted(NATIONAL_COMMANDS))
                +'\n국가 거래는 국고 잔액·보유자산·담보를 사용합니다. 인자가 없으면 DM에서 입력 창을 엽니다.\n'
                +'상품생성 예: $상품생성 NEW company "새 주식" 소프트웨어 100000000 100000 1000 INZ')

    async def send_result(self,author,result,command=None,**kwargs):
        title=f'Inazina · ${command}' if command else 'Inazina · 관리자'
        return await author.send(**admin_response(result,title),**kwargs)

    async def handle(self,message):
        if message.author.bot or not message.content.startswith('$'):
            return
        if not admin_allowed(self.bot,message.author.id):
            # Ordinary users receive no reply and no administrator interface.
            return
        from managers.admin_commands_manager import AdminCommandsManager,COMMANDS,USAGE
        import shlex
        from decimal import InvalidOperation
        session = None
        view = None
        try:
            raw=message.content[1:].strip().split(maxsplit=1)
            command=raw[0] if raw else 'help'
            arguments=raw[1] if len(raw)>1 else ''
            supported=COMMANDS|NATIONAL_COMMANDS|{'help','사건'}
            if command not in supported:
                await self.send_result(message.author,'❌ 알 수 없는 관리자 명령어입니다. $help를 사용해주세요.')
                return
            # Verify DM delivery before performing any operation with chat arguments.
            session=await message.author.send(embed=admin_embed('요청을 처리하고 있습니다.',f'Inazina · ${command}',pending=True),delete_after=300)
            if command in {'help','도움말'}:
                from functions.admin_help_view import AdminHelpView
                view=AdminHelpView(self.bot,message.author.id,NATIONAL_COMMANDS,message.guild)
                await session.edit(embed=view.build_embed(),view=view)
                session = None
            elif command in COMMANDS:
                result=await asyncio.to_thread(AdminCommandsManager(self.bot).execute,str(message.author.id),command,arguments)
                response=admin_response(result,f'Inazina · ${command}')
                if 'file' in response:
                    await self.send_result(message.author,result,command)
                else:
                    await session.edit(embed=response['embed'])
                session = None
            elif command in NATIONAL_COMMANDS and (arguments or command in READ_ONLY):
                if command=='주총':
                    raise ValueError('$주총은 인자 없이 실행한 뒤 DM 입력 창을 사용해주세요.')
                modal=ActionModal(self.bot,message.author.id,command,message.guild)
                values=[arguments] if command=='옵션작성' else shlex.split(arguments)
                if len(values)>len(modal.fields):
                    raise ValueError('국가 명의 명령어 인자 수를 확인해주세요.')
                values+=['']*(len(modal.fields)-len(values))
                if any(field.required and not value for field,value in zip(modal.fields,values)):
                    raise ValueError('필수 인자가 부족합니다. 인자 없이 명령어를 실행하면 입력 창을 엽니다.')
                result=await asyncio.to_thread(modal.execute,values)
                if command=='내지갑':
                    view=TreasuryAssetsView(self.bot,message.author.id,json.loads(result))
                    await session.edit(embed=view.build_embed(),view=view)
                    session = None
                else:
                    response=admin_response(result,f'Inazina · ${command}')
                    if 'file' in response:
                        await self.send_result(message.author,result,command)
                    else:
                        await session.edit(embed=response['embed'])
                    session = None
            else:
                if command=='사건' and arguments:
                    raise ValueError('$사건은 인자 없이 실행해주세요. 모든 설정은 모달에서 입력합니다.')
                view=AdminCommandView(self.bot,message.author.id,command,message.guild)
                await session.edit(content=None,embed=admin_embed('국가 명의로 실행합니다. 아래 버튼에서 입력 창을 여세요.' if command in NATIONAL_COMMANDS else '아래 버튼에서 사건 설정 모달을 여세요.',f'Inazina · ${command}'),view=view)
        except (ValueError,TypeError,OverflowError,KeyError,InvalidOperation,sqlite3.IntegrityError) as exc:
            await self.send_result(message.author,f'❌ {exc}')
        except discord.HTTPException:
            if view is not None:view.stop()
            # Controls and financial results stay private when the DM is blocked.
            logging.getLogger(__name__).warning('Could not deliver private administrator response')
        except Exception:
            logging.getLogger(__name__).exception('Administrator command failed')
            try:await self.send_result(message.author,'❌ 명령어 처리 중 오류가 발생했습니다. 로그를 확인해주세요.')
            except discord.HTTPException:pass
        finally:
            # Ctrl+C cancels this handler after discord.Client.close() has closed
            # aiohttp. Do not start another HTTP request against that session.
            closing = bool(getattr(self.bot,'is_closed',lambda:False)())
            if closing:
                if view is not None:view.stop()
            else:
                try:
                    if message.guild is not None:
                        await message.delete()
                except discord.NotFound:
                    pass
                except (discord.HTTPException,RuntimeError):
                    if view is not None:view.stop()
                    try:
                        if session is not None:await session.edit(view=None)
                        await self.send_result(message.author,'❌ 명령어 채팅을 삭제하지 못했습니다. 봇의 메시지 관리 권한을 확인해주세요.')
                    except (discord.HTTPException,RuntimeError):
                        logging.getLogger(__name__).warning('Could not remove administrator invocation')
