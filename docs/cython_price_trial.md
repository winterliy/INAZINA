# Cython 가격 계산 시범 적용

GARCH 분산·가격과 자동 목표가의 수치 계산만 Cython으로 컴파일한다.
Python 검증, 난수 생성 순서, 금액 반올림, 시각과 DB 처리는 기존 경로를 사용한다.
두 실행 방식은 `managers/_price_math.py` 하나를 공유한다.

## 빌드와 선택

프로젝트 루트에서 다음을 실행한다. 컴파일러가 필요하며 Cython과 setuptools는
빌드할 때만 필요하다. OS·Python 실행 환경이 바뀌면 다시 빌드한다.

```sh
.venv/bin/python -m pip install -r requirements-native.txt
.venv/bin/python build_price_native.py build_ext --inplace
```

`INAZINA_PRICE_BACKEND` 환경변수로 선택한다.

| 값 | 동작 |
|---|---|
| `auto` | 현재 소스와 일치하는 컴파일 파일이 있으면 Cython, 없으면 Python |
| `python` (기본) | 컴파일 파일이 있어도 Python 사용 |
| `cython` | Cython 필수. 파일이 없거나 오래됐으면 시작 시 빌드 안내 오류 |

소스 SHA-256을 빌드에 포함하고 시작 시 비교하므로 오래된 파일은 자동 사용하지 않는다.
변경한 소스를 적용하려면 빌드 후 봇을 재시작한다. 생성된 바이너리와 빌드 디렉터리는
Git 추적 대상에서 제외한다.

현재 선택 확인:

```sh
.venv/bin/python -c "from managers.price_backend import BACKEND; print(BACKEND)"
```

## 검증과 측정

```sh
.venv/bin/python -m unittest tests.test_price_native
.venv/bin/python tools/benchmark_price_native.py --database database/inazina.db
```

벤치마크는 운영 DB를 읽기 전용으로 백업한 뒤 임시 복사본 두 개에서 실행한다.
같은 난수와 시각으로 32회 갱신하고 초기 2회를 제외한 결과를 비교한다.
시뮬레이션 시각이 진행되지 않으면 유효한 속도 비교로 인정하지 않는다.
가격·변동성·목표가·공통 요인·사건·시세 기록·지수·ETF 상태도 비교한다.
실행 중인 봇의 Discord 응답 시간을 측정하는 것은 아니다.

시범 적용 중 GARCH·자동 목표가·호환성 검사 46개가 통과했다.
마무리 시 로더·수치 동등성 4개와 Python 모드의 수학 모델 검사 11개도 통과했다.
최대 가격, 오버플로, 변동성 0, 난수 상태, 반올림 및 컴파일 파일이 없는 환경을 포함한다.

2026-10-08 로컬 CPython 3.14.7 / macOS ARM64에서 측정했다.

| 항목 | Python | Cython |
|---|---:|---:|
| 가격·목표가 계산 10,000회 중앙값 | 42.30ms | 40.24ms |
| 전체 갱신 중앙값 | 18.71ms | 22.74ms |
| 전체 갱신 95백분위 | 39.25ms | 43.34ms |
| 갱신된 시뮬레이션 상태 수 | 130 | 130 |

각 가격 계산 묶음은 7회 측정 중 첫 회를 제외했다. DB 결과 비교는 모두 일치했다.
수치 계산은 약 5% 개선됐지만 전체 갱신의 개선은 확인되지 않아 기본값은 Python이다.
전체 시간에는 DB·파일 I/O와 시스템 부하가 포함되어 이 수치로 모든 환경의 성능을
일반화하지 않는다. 추가 시험 시 `INAZINA_PRICE_BACKEND=cython`으로 켤 수 있다.
