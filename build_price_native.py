"""Optional build: .venv/bin/python build_price_native.py build_ext --inplace."""
from pathlib import Path
import hashlib
import os
from setuptools import Extension, setup
from Cython.Build import cythonize

os.chdir(Path(__file__).resolve().parent)
source = Path('managers/_price_math.py').read_bytes()
generated = Path('build/native_source/_price_math.py')
generated.parent.mkdir(parents=True, exist_ok=True)
generated.write_bytes(source + f'\nSOURCE_HASH = {hashlib.sha256(source).hexdigest()!r}\n'.encode())
setup(
    name='inazina-price-native',
    packages=[],
    ext_modules=cythonize(
        [Extension('managers._price_math_native', [str(generated)],
                   extra_compile_args=['-ffp-contract=off'] if os.name != 'nt' else ['/fp:strict'])],
        compiler_directives={'language_level': 3, 'annotation_typing': True},
        build_dir='build/cython',
    ),
)
