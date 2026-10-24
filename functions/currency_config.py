"""Editable first-install currency catalog and base-currency settings."""
import json
import re
from pathlib import Path


DEFAULT_CURRENCY_CONFIG = (
    Path(__file__).resolve().parents[1] / "config" / "initial_currency_catalog.json"
)


def load_initial_currency_config(path=None):
    source = Path(path) if path else DEFAULT_CURRENCY_CONFIG
    try:
        config = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"초기 통화 설정을 읽을 수 없습니다: {source}") from exc
    if not isinstance(config, dict) or not isinstance(config.get("currencies"), list):
        raise ValueError("초기 통화 설정은 currencies 배열을 포함해야 합니다.")
    base = config.get("baseCurrency")
    if not isinstance(base, dict):
        raise ValueError("초기 통화 설정에 baseCurrency 객체가 필요합니다.")
    code = str(base.get("code") or "").strip().upper()
    name = str(base.get("name") or "").strip()
    nationality = str(base.get("nationality") or "").strip()
    if not re.fullmatch(r"[A-Z0-9]{2,12}", code) or not name or not nationality:
        raise ValueError("기본 통화 코드·이름·국가를 입력해주세요.")
    return config
