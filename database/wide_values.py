"""SQLite-safe exact decimal scalars, arithmetic and aggregate functions."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, getcontext
from fractions import Fraction
import sqlite3
from functions.amount_limits import DECIMAL_PRECISION, SQLITE_MAX_INTEGER, JS_SAFE_INTEGER
from functions.exact_money import ExactDecimal, decimal_value, decimal_text

TAG = '~M~'
REAL_TAG = '~R~'

def tagged(value):
    return isinstance(value,str) and value.startswith((TAG,REAL_TAG))

def real_tagged(value):
    return isinstance(value,str) and value.startswith(REAL_TAG)


def untag(value):
    return value[len(TAG):] if tagged(value) else value


def number(value):
    value = decimal_value(untag(value))
    if not value.is_finite():
        raise ValueError('금액은 유한한 숫자여야 합니다.')
    return value


def packed(value, force=False):
    value = number(value)
    if force or abs(value)>JS_SAFE_INTEGER:
        return (REAL_TAG if force == 'real' else TAG) + decimal_text(value)
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def canonical(value):
    return None if value is None else TAG + decimal_text(number(value))

def canonical_real(value):
    return None if value is None else REAL_TAG + decimal_text(number(value))


def decoded(value, integral=False):
    value = number(value.decode() if isinstance(value,bytes) else value)
    if integral:
        if value != value.to_integral_value():
            raise ValueError('최소단위 금액은 정수여야 합니다.')
        return int(value)
    if value == value.to_integral_value():
        return int(value) if abs(value)<=JS_SAFE_INTEGER else ExactDecimal(value)
    return float(value) if abs(value)<10**9 else ExactDecimal(value)


def adapt(value):
    if isinstance(value,Decimal):
        if not value.is_finite():raise ValueError('금액은 유한한 숫자여야 합니다.')
        return canonical(value) if abs(value)>10**9 else int(value) if value==value.to_integral_value() else float(value)
    if isinstance(value,int) and not isinstance(value,bool) and abs(value)>SQLITE_MAX_INTEGER:
        return canonical(value)
    return value


def parameters(values):
    if isinstance(values,dict):
        return {key:adapt(value) for key,value in values.items()}
    return tuple(adapt(value) for value in values)


def binary(a,b,operation):
    if a is None or b is None:
        return None
    try:
        a1,b1=number(a),number(b)
    except (ValueError,InvalidOperation,TypeError):
        # SQLite arithmetic casts nonnumeric text to zero.
        try: a1=number(a)
        except (ValueError,InvalidOperation,TypeError): a1=Decimal(0)
        try: b1=number(b)
        except (ValueError,InvalidOperation,TypeError): b1=Decimal(0)
    force='real' if any(real_tagged(value) or isinstance(value,float) for value in (a,b)) else any(tagged(value) for value in (a,b))
    return packed(operation(a1,b1),force)


def compare(a,b):
    if a is None or b is None:
        return None
    try:
        x,y=number(a),number(b)
        return (x>y)-(x<y)
    except (ValueError,InvalidOperation,TypeError):
        # Native SQLite storage ordering: numbers precede text, text precedes blobs.
        rank=lambda v: 0 if isinstance(v,(int,float,Decimal)) or (tagged(v)) else 2 if isinstance(v,bytes) else 1
        if rank(a)!=rank(b): return (rank(a)>rank(b))-(rank(a)<rank(b))
        x,y=str(a),str(b)
        return (x>y)-(x<y)


def rounding(value, places=0):
    if value is None:return None
    places=int(untag(places or 0))
    if not -100<=places<=100: raise ValueError('반올림 자릿수를 확인해주세요.')
    return packed(number(value).quantize(Decimal(1).scaleb(-places),rounding=ROUND_HALF_UP),tagged(value))


def integer(value):
    if value is None:return None
    try:return packed(int(number(value)),tagged(value))
    except (InvalidOperation,ValueError,TypeError):return 0


def real(value):
    if value is None:return None
    try:
        x=number(value)
        return canonical_real(x) if abs(x)>JS_SAFE_INTEGER or tagged(value) else float(x)
    except (InvalidOperation,ValueError,TypeError):return 0.0


def typeof(value):
    if value is None:return 'null'
    if isinstance(value,str):
        try:return 'real' if real_tagged(value) else 'integer' if number(value)==number(value).to_integral_value() else 'real'
        except (ValueError,InvalidOperation):return 'text'
    return 'integer' if isinstance(value,int) else 'real' if isinstance(value,float) else 'blob' if isinstance(value,bytes) else 'text'


class Sum:
    def __init__(self):self.value=Decimal(0);self.seen=False;self.force=False
    def step(self,value):
        if value is not None:
            self.value+=number(value);self.seen=True
            self.force='real' if self.force=='real' or real_tagged(value) else self.force or tagged(value)
    def finalize(self):return packed(self.value,self.force) if self.seen else None


class Average(Sum):
    def __init__(self):
        super().__init__();self.count=0
    def step(self,value):
        super().step(value)
        if value is not None:self.count+=1
    def finalize(self):
        return packed(self.value/self.count,'real') if self.count else None


class Total(Sum):
    def finalize(self):return packed(self.value,self.force)


class Min:
    largest=False
    def __init__(self):self.value=None
    def step(self,value):
        if value is not None and (self.value is None or ((compare(value,self.value)>0) if self.largest else (compare(value,self.value)<0))):self.value=value
    def finalize(self):return self.value


class Max(Min):largest=True


def extrema(values,largest):
    if any(v is None for v in values):return None
    result=values[0]
    for value in values[1:]:
        if ((compare(value,result)>0) if largest else (compare(value,result)<0)):result=value
    return result


def valid_money(value,limit,integral):
    if value is None:return 1
    try:
        parsed=number(value)
        return int(abs(parsed)<=number(limit) and (not integral or parsed==parsed.to_integral_value()))
    except (ValueError,InvalidOperation,TypeError):return 0


def register(connection):
    getcontext().prec=DECIMAL_PRECISION
    sqlite3.register_converter('MONEY',lambda value:decoded(value))
    sqlite3.register_converter('MONEYREAL',lambda value:decoded(value))
    sqlite3.register_converter('MONEYINT',lambda value:decoded(value,True))
    sqlite3.register_converter('MONEYUNITS',lambda value:decoded(value,True))
    for name,fn in {'wide_add':lambda a,b:binary(a,b,lambda x,y:x+y),'wide_sub':lambda a,b:binary(a,b,lambda x,y:x-y),'wide_mul':lambda a,b:binary(a,b,lambda x,y:x*y),'wide_div':lambda a,b:None if b is None or number(b)==0 else binary(a,b,lambda x,y:(x//y if x==x.to_integral_value() and y==y.to_integral_value() and not any(isinstance(v,float) or real_tagged(v) for v in (a,b)) else x/y)),'wide_div_real':lambda a,b:None if a is None or b is None or number(b)==0 else packed(number(a)/number(b),'real'),'wide_mod':lambda a,b:None if b is None or number(b)==0 else binary(a,b,lambda x,y:x%y),'wide_is':lambda a,b:int(a is b) if a is None or b is None else int(compare(a,b)==0),'wide_cmp':compare}.items():
        connection.create_function(name,2,fn,deterministic=True)
    for name,fn in {'wide_int':integer,'wide_real':real,'wide_typeof':typeof,'wide_text':lambda v:None if v is None else str(untag(v)),'wide_canonical':canonical,'wide_canonical_real':canonical_real,'wide_abs':lambda v:None if v is None else packed(abs(number(v)),tagged(v))}.items():
        connection.create_function(name,1,fn,deterministic=True)
    connection.create_function('wide_valid_money',3,valid_money,deterministic=True)
    connection.create_function('wide_round',-1,rounding,deterministic=True)
    connection.create_function('wide_min',-1,lambda *args:extrema(args,False),deterministic=True)
    connection.create_function('wide_max',-1,lambda *args:extrema(args,True),deterministic=True)
    for name,cls in {'wide_avg':Average,'wide_sum':Sum,'wide_total':Total,'wide_min_agg':Min,'wide_max_agg':Max}.items():connection.create_aggregate(name,1,cls)
    connection.create_collation('wide_numeric',lambda a,b:compare(a,b) or 0)
