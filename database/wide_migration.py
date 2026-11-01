"""Offline copy migration: preserve originals and recreate triggers only after copying."""
import sqlite3
from pathlib import Path
from .unified import UnifiedConnection
from .money_columns import money_kind
from .wide_values import canonical, canonical_real


def migrate_copy(source, destination):
    source,destination=Path(source).resolve(),Path(destination).resolve()
    if source==destination or destination.exists():
        raise ValueError('원본과 다른 새 DB 경로를 사용해주세요.')
    src=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
    dst=sqlite3.connect(destination,factory=UnifiedConnection)
    dst.wide_money=True
    dst.row_factory=sqlite3.Row
    counts={}
    try:
        sqlite3.Connection.execute(dst,'PRAGMA foreign_keys=OFF')
        sqlite3.Connection.execute(dst,'BEGIN IMMEDIATE')
        schema=src.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'").fetchall()
        for kind,name,ddl in schema:
            if kind!='table':continue
            dst.execute(ddl)
            escaped=name.replace('"','""')
            rows=src.execute(f'SELECT rowid,* FROM "{escaped}"')
            columns=[item[0] for item in rows.description][1:]
            declared={item[1]:item[2].upper() for item in src.execute(f'PRAGMA table_info("{escaped}")')}
            real_columns={column for column,kind in declared.items() if kind in {'REAL','FLOAT','DOUBLE'} or kind.startswith('MONEYREAL')}
            # Explicit rowid preserves snapshot lineage for composite-key tables.
            names=['rowid',*columns]
            insert=f'INSERT INTO "{escaped}" ('+','.join('"'+c.replace('"','""')+'"' for c in names)+') VALUES ('+','.join('?' for _ in names)+')'
            count=0
            for row in rows:
                converted=[row[0]]
                for column,value in zip(columns,row[1:]):
                    converted.append((canonical_real(value) if column in real_columns else canonical(value)) if value is not None and money_kind(name,column) else value)
                dst.execute(insert,converted)
                count+=1
            counts[name]=count
        if src.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
            for name,seq in src.execute('SELECT name,seq FROM sqlite_sequence'):
                sqlite3.Connection.execute(dst,'UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name=?',(seq,name))
        for kind,name,ddl in schema:
            if kind in {'index','view','trigger'}:dst.execute(ddl)
        sqlite3.Connection.execute(dst,'CREATE TABLE IF NOT EXISTS wide_money_schema(version INTEGER NOT NULL)')
        sqlite3.Connection.execute(dst,'DELETE FROM wide_money_schema')
        sqlite3.Connection.execute(dst,'INSERT INTO wide_money_schema VALUES (1)')
        for name,expected in counts.items():
            escaped=name.replace('"','""')
            actual=dst.execute(f'SELECT COUNT(*) FROM "{escaped}"').fetchone()[0]
            if actual!=expected:raise ValueError(f'{name} 행 수가 일치하지 않습니다.')
        problems=sqlite3.Connection.execute(dst,'PRAGMA foreign_key_check').fetchall()
        if problems:raise ValueError(f'외래키 검사 실패: {len(problems)}건')
        if sqlite3.Connection.execute(dst,'PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('DB 무결성 검사 실패')
        dst.commit()
        return counts
    except BaseException:
        dst.rollback()
        raise
    finally:
        src.close();dst.close()
