"""Cached SQLite expression compilation for exact monetary arithmetic."""
from functools import lru_cache
import re
from contextvars import ContextVar
_ALIASES=ContextVar("wide_money_aliases",default=frozenset())
import sqlglot
from sqlglot import exp
from .money_columns import money_kind, possible_money, REGISTERED


def call(name,*args):
    return exp.Anonymous(this=name,expressions=list(args))


def monetary(node):
    return any(possible_money(c.name.lower()) or c.name.lower() in _ALIASES.get() for c in node.find_all(exp.Column))


def widen_bounds(node):
    from functions.amount_limits import MAX_NATIVE,MAX_MINOR,MAX_SUBMINOR
    old={2**52,2**53-1,2**63-1,9000000000000000000,(2**53-1)*100}
    columns=[c.name.lower() for c in node.find_all(exp.Column)]
    limit=MAX_SUBMINOR if any('subminor' in name for name in columns) else MAX_MINOR if any(name.endswith(('_minor','_minor_units','_units')) for name in columns) else MAX_NATIVE
    for key,value in list(node.args.items()):
        negative=isinstance(value,exp.Neg)
        literal=value.this if negative else value
        if isinstance(literal,exp.Literal) and not literal.is_string:
            try: matched=int(literal.this) in old
            except ValueError: matched=False
            if matched:
                replacement=exp.Literal.string('~M~'+str(limit))
                node.set(key,call('wide_sub',exp.Literal.number(0),replacement) if negative else replacement)
    return node


def transform(node, assignment=False, money_context=False):
    if assignment and isinstance(node,exp.EQ):
        money_context=money_context or monetary(node.this)
    if isinstance(node,exp.ColumnDef):
        money_context=money_context or possible_money(node.name.lower())
    for key,value in list(node.args.items()):
        if isinstance(value,exp.Expression):
            node.set(key,transform(value, isinstance(node,(exp.Update,exp.OnConflict)) and key=='expressions', money_context))
        elif isinstance(value,list):
            node.set(key,[transform(v, isinstance(node,(exp.Update,exp.OnConflict)) and key=='expressions', money_context) if isinstance(v,exp.Expression) else v for v in value])
    if isinstance(node,exp.Literal) and not node.is_string:
        from decimal import Decimal
        if abs(Decimal(node.this))>2**63-1:
            return exp.Literal.string('~M~'+node.this)
    arithmetic={exp.Add:'wide_add',exp.Sub:'wide_sub',exp.Mul:'wide_mul',exp.Div:'wide_div',exp.Mod:'wide_mod'}
    exact=money_context or monetary(node)
    if isinstance(node,exp.Neg) and exact:
        return call('wide_sub',exp.Literal.number(0),node.this)
    if type(node) in arithmetic and exact:
        name=arithmetic[type(node)]
        if isinstance(node,exp.Div) and any((money_kind('',c.name.lower())=='MONEY' or any(money_kind(table,c.name.lower())=='MONEY' for table,fields in REGISTERED.items() if c.name.lower() in fields)) for c in node.find_all(exp.Column)):
            name='wide_div_real'
        return call(name,node.this,node.expression)
    if isinstance(node,exp.Cast) and exact:
        target=str(node.args['to'].this).upper()
        if target.endswith(('INT','BIGINT','SMALLINT')):return call('wide_int',node.this)
        if target.endswith(('FLOAT','DOUBLE','REAL')):return call('wide_real',node.this)
        if target.endswith(('TEXT','VARCHAR')):return call('wide_text',node.this)
    if isinstance(node,exp.Sum) and exact:return call('wide_sum',node.this)
    if isinstance(node,exp.Avg) and exact:return call('wide_avg',node.this)
    if isinstance(node,exp.Ordered) and exact:
        node.set('this',exp.Collate(this=call('wide_canonical',node.this),expression=exp.Var(this='wide_numeric')))
        return node
    if isinstance(node,exp.Abs) and exact:return call('wide_abs',node.this)
    if isinstance(node,exp.Round) and exact:return call('wide_round',node.this,node.args.get('decimals') or exp.Literal.number(0))
    if isinstance(node,exp.Typeof) and monetary(node):return call('wide_typeof',node.this)
    if isinstance(node,(exp.Min,exp.Max)) and exact:
        return call('wide_min_agg' if isinstance(node,exp.Min) else 'wide_max_agg',node.this)
    if isinstance(node,(exp.Greatest,exp.Least)) and exact:
        return call('wide_max' if isinstance(node,exp.Greatest) else 'wide_min',node.this,*node.expressions)
    if isinstance(node,exp.Anonymous) and node.name.upper()=='TOTAL' and exact:return call('wide_total',*node.expressions)
    if type(node) in {exp.EQ,exp.NEQ,exp.GT,exp.GTE,exp.LT,exp.LTE} and not assignment and monetary(node):
        node=widen_bounds(node)
        return type(node)(this=call('wide_cmp',node.this,node.expression),expression=exp.Literal.number(0))
    if isinstance(node,exp.Glob) and exact:
        node.set('this',call('wide_text',node.this))
        return node
    if isinstance(node,exp.Length) and exact:
        node.set('this',call('wide_text',node.this))
        return node
    if isinstance(node,exp.Is) and exact:
        return call('wide_is',node.this,node.expression)
    if isinstance(node,exp.Between) and monetary(node):
        node=widen_bounds(node)
        return exp.And(this=exp.GTE(this=call('wide_cmp',node.this.copy(),node.args['low']),expression=exp.Literal.number(0)),expression=exp.LTE(this=call('wide_cmp',node.this,node.args['high']),expression=exp.Literal.number(0)))
    return node


@lru_cache(maxsize=12000)
def compile_sql(statement):
    stripped=statement.lstrip()
    explain=re.match(r'EXPLAIN\s+(?:QUERY\s+PLAN\s+)?',stripped,re.I)
    if explain:return stripped[:explain.end()]+compile_sql(stripped[explain.end():])
    if re.match(r'CREATE\s+(?:TEMP\s+)?TRIGGER',stripped,re.I):
        match=re.match(r'(.*?)(?:\bWHEN\s+(.*?))?\bBEGIN\s+(.*)\bEND\s*;?\s*$',statement,re.I|re.S)
        if not match:raise ValueError('지원하지 않는 금액 트리거 구문입니다.')
        prefix,condition,body=match.groups()
        if condition:
            condition=transform(sqlglot.parse_one('SELECT '+condition,read='sqlite')).expressions[0].sql(dialect='sqlite')
        chunks=sqlglot.tokens.Tokenizer(dialect='sqlite').tokenize(body)
        parts=[];begin=0
        for token in chunks:
            if token.token_type==sqlglot.tokens.TokenType.SEMICOLON:
                parts.append(body[begin:token.start]);begin=token.end+1
        parts.append(body[begin:])
        return prefix+('WHEN '+condition+' ' if condition else '')+'BEGIN '+'; '.join(compile_sql(p.strip()) for p in parts if p.strip())+'; END'
    if re.match(r'(?:PRAGMA|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE|VACUUM|ATTACH|DETACH)',stripped,re.I):return statement
    real_columns=set(re.findall(r'([A-Za-z_][A-Za-z0-9_]*)\s+MONEYREAL\s+TEXT',statement,re.I))
    statement=re.sub(r'\bMONEY(?:INT|UNITS|REAL)?\s+TEXT\b','TEXT',statement,flags=re.I)
    tree=sqlglot.parse_one(statement,read='sqlite')
    if isinstance(tree,exp.Command):raise ValueError('정확한 금액 연산으로 해석하지 못한 SQL입니다.')
    if isinstance(tree,(exp.Create,exp.Alter)):
        table=next(tree.find_all(exp.Table),None)
        if table:
            for column in tree.find_all(exp.ColumnDef):
                kind=money_kind(table.name,column.name)
                if kind and column.args.get('kind'):
                    old=column.args['kind'].sql(dialect='sqlite').upper()
                    if old not in {'TEXT','INTEGER','INT','REAL','FLOAT','DOUBLE','NUMERIC','DECIMAL'}:continue
                    if kind=='MONEY' and (old in {'REAL','FLOAT','DOUBLE'} or column.name in real_columns):kind='MONEYREAL'
                    column.set('kind',exp.Var(this=('MONEYUNITS' if kind=='MONEYINT' else kind)+' TEXT'))
                    column.append('constraints',exp.ColumnConstraint(kind=exp.CollateColumnConstraint(this=exp.Column(this=exp.Identifier(this='wide_numeric')))))
                    from functions.amount_limits import MAX_NATIVE,MAX_MINOR,MAX_SUBMINOR
                    limit=10**90 if column.name=='baseline_supply_units' else MAX_SUBMINOR if 'subminor' in column.name else MAX_MINOR if kind=='MONEYINT' else MAX_NATIVE
                    column.append('constraints',exp.ColumnConstraint(kind=exp.CheckColumnConstraint(this=call('wide_valid_money',exp.Column(this=column.this.copy()),exp.Literal.string(str(limit)),exp.Literal.number(1 if kind=='MONEYINT' else 0)))))
    aliases=set()
    for _ in range(3):
        token=_ALIASES.set(frozenset(aliases))
        for item in tree.find_all(exp.Alias):
            if monetary(item.this):aliases.add(item.alias.lower())
        _ALIASES.reset(token)
    token=_ALIASES.set(frozenset(aliases))
    try:return transform(tree).sql(dialect='sqlite')
    finally:_ALIASES.reset(token)
