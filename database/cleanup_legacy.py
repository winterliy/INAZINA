"""Remove only migrated legacy databases after making a verified recovery archive."""
import hashlib
import json
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path
from .unified import ALIASES,_legacy_snapshot


def cleanup_legacy(root,archive):
    root,archive = Path(root).resolve(),Path(archive).resolve()
    paths = [root/f'{alias}.db' for alias in ALIASES if (root/f'{alias}.db').exists()]
    if not paths:
        return []
    target = root/'inazina.db'
    with closing(sqlite3.connect(f'{target.as_uri()}?mode=ro',uri=True)) as conn:
        if conn.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise RuntimeError('통합 DB 무결성 검증에 실패하여 기존 DB를 유지합니다.')
        imported = {row[0] for row in conn.execute('SELECT source FROM migration_manifest')}
        if any(path.stem not in imported for path in paths):
            raise RuntimeError('이전 기록이 없는 DB는 삭제하지 않습니다.')
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if archive.exists():
        raise FileExistsError('기존 복구 백업을 덮어쓰지 않습니다.')
    archive.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as folder,_legacy_snapshot(root) as (source,attached):
        manifest = {}
        for alias in attached:
            names = [r[0] for r in source.execute(f"SELECT name FROM {alias}.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            if set(names)-tables:
                raise RuntimeError('통합되지 않은 테이블이 있어 기존 DB를 유지합니다.')
            backup = Path(folder)/f'{alias}.db'
            with closing(sqlite3.connect(f'{(root/f"{alias}.db").as_uri()}?mode=ro',uri=True)) as original,closing(sqlite3.connect(backup)) as dest:
                original.backup(dest)
                if dest.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                    raise RuntimeError('복구 백업 검증에 실패했습니다.')
            manifest[backup.name] = {'sha256':hashlib.sha256(backup.read_bytes()).hexdigest(),
                'rows':{name:source.execute(f'SELECT COUNT(*) FROM {alias}."{name.replace(chr(34),chr(34)*2)}"').fetchone()[0] for name in names}}
        staging = archive.with_suffix('.zip.tmp')
        try:
            with zipfile.ZipFile(staging,'x',compression=zipfile.ZIP_DEFLATED) as output:
                for filename in manifest:
                    output.write(Path(folder)/filename,filename)
                output.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
            with zipfile.ZipFile(staging) as check:
                if check.testzip() is not None:
                    raise RuntimeError('압축 백업 무결성 검증에 실패했습니다.')
                for filename,metadata in manifest.items():
                    if hashlib.sha256(check.read(filename)).hexdigest()!=metadata['sha256']:
                        raise RuntimeError('압축 백업 체크섬 검증에 실패했습니다.')
            staging.replace(archive)
        finally:
            staging.unlink(missing_ok=True)
    # The archive preserves old historical rows even if retention removed them
    # from the live DB. Unrecognized/history-only database files are untouched.
    for path in paths:
        path.unlink()
        for suffix in ('-wal','-shm'):
            Path(str(path)+suffix).unlink(missing_ok=True)
    return [path.name for path in paths]
