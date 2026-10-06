"""Select a numeric backend once on startup; no compiler is needed at runtime."""
import os
from . import _price_math as kernels

BACKEND = 'python'
requested = os.environ.get('INAZINA_PRICE_BACKEND', 'auto').lower()
if requested not in {'auto', 'python', 'cython'}:
    raise ValueError('INAZINA_PRICE_BACKEND는 auto, python, cython 중 하나여야 합니다.')
if requested != 'python':
    try:
        from . import _price_math_native as kernels
    except ImportError:
        if requested == 'cython':
            raise ImportError('Cython 가격 모듈을 먼저 빌드해주세요: python build_price_native.py build_ext --inplace') from None
    else:
        BACKEND = 'cython'
