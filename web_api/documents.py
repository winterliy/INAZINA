"""Public text documents edited from the dashboard: terms, privacy policy,
rules, help and the Discord registration notice.

Legal documents (privacy, terms, operations, internal) change by notice:
saving publishes the revision at once as an announcement (``<name>.next.txt``
plus ``pending.json`` beside the site's documents), and the revision takes
effect at 0:00 KST once the notice period has passed: 7 days, or 30 for a
change that is unfavourable or important to users (privacy policy 9, terms 3).
Nobody enters a date; the revision's "시행일:" line is filled in from it.
Until then the current version stays in force; ``apply_due`` swaps it in.
Help and the registration notice change at once.

Saving is two steps: ``preview`` shows line changes and what they affect,
then ``save`` writes only when the caller confirms and the file is still the
version the editor started from. Replaced versions are kept under
database/document_history/<id>/.
"""
import datetime
import difflib
import hashlib
import json
import os
import re
import time
import zoneinfo
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINKS = ROOT / "apps" / "site" / "public" / "links"
DOCUMENTS = {  # id: (title, path, where it is shown)
    "privacy": ("개인정보 처리방침", LINKS / "privacy.txt", "공식 사이트"),
    "terms": ("서비스 이용약관", LINKS / "terms.txt", "공식 사이트"),
    "operations": ("서비스 운영규칙", LINKS / "operations.txt", "공식 사이트"),
    "internal": ("서비스 내부규칙", LINKS / "internal.txt", "공식 사이트"),
    "help": ("명령어 도움말", LINKS / "help.txt", "공식 사이트"),
    "registration": ("Discord 등록 안내문", ROOT / "database" / "private.text", "Discord /등록 모달"),
}
LEGAL = {"privacy", "terms", "operations", "internal"}
NOTICE_DAYS, IMPORTANT_NOTICE_DAYS = 7, 30
HISTORY = Path(os.environ.get("INAZINA_DOCUMENT_HISTORY", ROOT / "database" / "document_history"))
MAX_CHARS = 100_000
# /등록 shows the notice plus a link and a confirmation code in one 4,000-character modal field.
REGISTRATION_MAX_CHARS = 3_800
KST = zoneinfo.ZoneInfo("Asia/Seoul")
EFFECTIVE_LINE = re.compile(r"^시행일\s*[:：].*$", re.M)


def _document(document_id):
    if document_id not in DOCUMENTS:
        raise ValueError("문서를 찾을 수 없습니다.")
    return DOCUMENTS[document_id]


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read(path):
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _write(path, text):
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)  # readers never see a half-written file


def _kst(epoch):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(epoch + 9 * 3600)) if epoch else None


def _next_path(document_id):
    path = DOCUMENTS[document_id][1]
    return path.with_name(f"{path.stem}.next{path.suffix}")


def _manifest_path():
    return LINKS / "pending.json"


def _manifest():
    try:
        return json.loads(_manifest_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_manifest(manifest):
    _write(_manifest_path(), json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


def _pending(document_id):
    entry = _manifest().get(document_id)
    path = _next_path(document_id)
    if not entry or not path.exists():
        return None
    return {**entry, "content": _read(path)}


def effective_epoch(important, now=None):
    """0:00 KST of the day the notice period ends (counted from today, KST)."""
    today = datetime.datetime.fromtimestamp(now if now is not None else time.time(), KST).date()
    day = today + datetime.timedelta(days=IMPORTANT_NOTICE_DAYS if important else NOTICE_DAYS)
    return int(datetime.datetime(day.year, day.month, day.day, tzinfo=KST).timestamp())


def _stamp(text, epoch):
    """Write the effective date into the revision's 시행일 line (added under the title when missing)."""
    day = datetime.datetime.fromtimestamp(epoch, KST)
    line = f"시행일: {day.year}년 {day.month}월 {day.day}일"
    if EFFECTIVE_LINE.search(text):
        return EFFECTIVE_LINE.sub(line, text, count=1)
    first, _, rest = text.partition("\n")
    return f"{first}\n{line}\n{rest}"


def list_documents():
    manifest = _manifest()
    result = []
    for document_id, (title, path, shown) in DOCUMENTS.items():
        stat = path.stat() if path.exists() else None
        pending = manifest.get(document_id) if _next_path(document_id).exists() else None
        result.append({"id": document_id, "title": title, "shown": shown, "legal": document_id in LEGAL,
                       "path": str(path.relative_to(ROOT)), "size": stat.st_size if stat else 0,
                       "updatedEpoch": int(stat.st_mtime) if stat else None,
                       "pendingEffectiveEpoch": pending["effectiveEpoch"] if pending else None})
    return result


def read_document(document_id):
    """The version in force, the announced revision (if any) and the editing base."""
    title, path, shown = _document(document_id)
    text = _read(path)
    pending = _pending(document_id)
    base = pending["content"] if pending else text
    return {"id": document_id, "title": title, "shown": shown, "legal": document_id in LEGAL,
            "path": str(path.relative_to(ROOT)), "content": text, "pending": pending,
            "base": base, "sha256": _sha(base), "history": _history(document_id),
            "noticeDays": NOTICE_DAYS, "importantNoticeDays": IMPORTANT_NOTICE_DAYS}


def _clean(content):
    if not isinstance(content, str):
        raise ValueError("문서 내용을 확인해주세요.")
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ValueError("빈 문서는 저장할 수 없습니다.")
    if len(text) > MAX_CHARS:
        raise ValueError(f"문서는 {MAX_CHARS:,}자 이하로 입력해주세요.")
    return text if text.endswith("\n") else text + "\n"


def preview(document_id, content, important=False, now=None):
    """Line changes against the editing base and when they would apply, without writing anything."""
    title, path, _ = _document(document_id)
    current = read_document(document_id)
    new = _clean(content)
    notes, warnings = [], []
    if document_id in LEGAL:
        epoch = effective_epoch(important is True, now)
        new = _stamp(new, epoch)
        period = IMPORTANT_NOTICE_DAYS if important is True else NOTICE_DAYS
        notes.append(f"저장하면 개정안을 바로 공지하고, 공지 기간 {period}일이 지난 {_kst(epoch)}(한국시간)에 적용합니다. 그때까지는 지금 문서가 적용됩니다.")
        notes.append("개정안의 '시행일:' 줄은 이 날짜로 자동으로 채워집니다.")
        if current["pending"]:
            warnings.append(f"이미 공지한 개정안({_kst(current['pending']['effectiveEpoch'])} 시행 예정)을 이 내용으로 바꾸고, 시행일도 새로 계산합니다.")
    else:
        notes.append("저장하면 바로 적용됩니다.")
    blocking = document_id == "registration" and len(new.strip()) > REGISTRATION_MAX_CHARS
    if blocking:
        warnings.append(f"Discord 등록 모달에 들어가지 않습니다: {len(new.strip()):,}자 / 최대 {REGISTRATION_MAX_CHARS:,}자.")
    old = current["base"]
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), "현재", "수정본", n=2, lineterm=""))
    return {"title": title, "diff": diff[:2000], "changed": old != new, "notes": notes, "warnings": warnings, "blocking": blocking,
            "added": sum(1 for line in diff if line.startswith("+") and not line.startswith("+++")),
            "removed": sum(1 for line in diff if line.startswith("-") and not line.startswith("---")),
            "effectiveEpoch": effective_epoch(important is True, now) if document_id in LEGAL else None}


def save(document_id, content, base_sha256, confirmed, important=False, now=None):
    """Publish a confirmed edit: announce it (legal documents) or apply it at once.

    Returns the audit details.
    """
    _, path, _ = _document(document_id)
    if confirmed is not True:
        raise ValueError("변경 내용을 확인한 뒤 확정해 주세요.")
    result = preview(document_id, content, important, now)
    if result["blocking"]:
        raise ValueError(result["warnings"][-1])
    current = read_document(document_id)
    if current["sha256"] != base_sha256:
        raise ValueError("편집하는 동안 다른 곳에서 문서가 바뀌었습니다. 다시 불러온 뒤 수정해 주세요.")
    if not result["changed"]:
        raise ValueError("바뀐 내용이 없습니다.")
    new = _clean(content)
    change = {"document": document_id, "before": current["sha256"], "added": result["added"], "removed": result["removed"]}
    if document_id in LEGAL:
        epoch = result["effectiveEpoch"]
        new = _stamp(new, epoch)
        _write(_next_path(document_id), new)
        manifest = _manifest()
        manifest[document_id] = {"effectiveEpoch": epoch, "announcedEpoch": int(now if now is not None else time.time()),
                                 "important": important is True}
        _save_manifest(manifest)
        return {**change, "after": _sha(new), "scheduled": epoch}
    _backup(document_id, current["content"])
    _write(path, new)
    return {**change, "after": _sha(new), "scheduled": None}


def cancel(document_id):
    """Withdraw an announced revision; the version in force stays."""
    _document(document_id)
    manifest = _manifest()
    if document_id not in manifest and not _next_path(document_id).exists():
        raise ValueError("공지한 개정안이 없습니다.")
    manifest.pop(document_id, None)
    _save_manifest(manifest)
    _next_path(document_id).unlink(missing_ok=True)
    return {"document": document_id}


def apply_due(now=None):
    """Put every announced revision whose effective time has come into force; returns what changed."""
    now = now if now is not None else time.time()
    manifest = _manifest()
    applied = []
    for document_id, entry in list(manifest.items()):
        if document_id not in DOCUMENTS or entry.get("effectiveEpoch", 0) > now:
            continue
        revision = _next_path(document_id)
        if revision.exists():
            path = DOCUMENTS[document_id][1]
            _backup(document_id, _read(path))
            _write(path, _read(revision))
            revision.unlink()
            applied.append({"document": document_id, "effectiveEpoch": entry["effectiveEpoch"], "after": _sha(_read(path))})
        manifest.pop(document_id)
    if applied or len(manifest) != len(_manifest()):
        _save_manifest(manifest)
    return applied


def _backup(document_id, text):
    if not text:
        return
    folder = HISTORY / document_id
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(time.time() + 9 * 3600))
    (folder / f"{stamp}-{_sha(text)[:8]}.txt").write_text(text, encoding="utf-8")


def _history(document_id):
    folder = HISTORY / document_id
    if not folder.is_dir():
        return []
    return [{"name": item.name, "size": item.stat().st_size} for item in sorted(folder.glob("*.txt"), reverse=True)[:20]]
