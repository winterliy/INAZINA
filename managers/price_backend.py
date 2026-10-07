"""Select a numeric backend once on startup; no compiler is needed at runtime."""
import os
import hashlib
from pathlib import Path
from . import _price_math as kernels

BACKEND = 'python'
# The trial improved kernels but did not improve whole-tick latency.
requested = os.environ.get('INAZINA_PRICE_BACKEND', 'python').lower()
if requested not in {'auto', 'python', 'cython'}:
    raise ValueError('INAZINA_PRICE_BACKEND는 auto, python, cython 중 하나여야 합니다.')
if requested != 'python':
    try:
        from . import _price_math_native as native
        source_hash = hashlib.sha256(Path(kernels.__file__).read_bytes()).hexdigest()
        if getattr(native, 'SOURCE_HASH', None) != source_hash:
            raise ImportError('Cython 가격 모듈을 다시 빌드해주세요.')
    except ImportError:
        if requested == 'cython':
            raise ImportError('Cython 가격 모듈을 먼저 빌드해주세요: python build_price_native.py build_ext --inplace') from None
    else:
        kernels = native
        BACKEND = 'cython'
