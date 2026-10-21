"""불변 Chroma 원문의 제목·행 위치만 보존하는 작은 파생 색인."""
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from backend.common import extract_issue_id_from_text, extract_title_from_chroma_text
from backend.execution import checkpoint


def source_signature(path: Path) -> tuple:
    def stat(item):
        try:
            value=item.stat()
            return value.st_ino,value.st_size,value.st_mtime_ns
        except FileNotFoundError:
            return None
    return str(path.resolve()),stat(path),stat(Path(str(path)+'-wal'))


@contextmanager
def open_title_projection(source: Path,target: Path):
    db=None;valid=None
    try:
        if target.is_file():
            db=sqlite3.connect(f'file:{target}?mode=ro',uri=True)
            meta=json.loads(db.execute('SELECT value FROM metadata').fetchone()[0])
            if (meta['version']==1 and meta['signature']==json.dumps(source_signature(source))
                    and db.execute('SELECT count(*) FROM titles').fetchone()[0]==meta['count']):
                valid=db
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError):
        pass
    try:
        yield valid
    finally:
        if db is not None:
            db.close()


def build_title_projection(source: Path,target: Path) -> dict:
    with open_title_projection(source,target) as ready:
        if ready is not None:
            return {'status':'current','count':ready.execute('SELECT count(*) FROM titles').fetchone()[0]}
    target.parent.mkdir(parents=True,exist_ok=True)
    signature=source_signature(source)
    descriptor,name=tempfile.mkstemp(prefix=target.name+'.',suffix='.tmp',dir=target.parent)
    os.close(descriptor);temporary=Path(name)
    try:
        with closing(sqlite3.connect(f'file:{source}?mode=ro',uri=True)) as raw, \
             closing(sqlite3.connect(temporary)) as db,db:
            db.execute('CREATE TABLE titles(position INTEGER PRIMARY KEY,source_rowid INTEGER,issue_id INTEGER,title TEXT)')
            db.execute('CREATE TABLE metadata(value TEXT)')
            count=0
            for position,(rowid,content) in enumerate(raw.execute(
                    "SELECT rowid,string_value FROM embedding_metadata WHERE key='chroma:document'")):
                checkpoint()
                db.execute('INSERT INTO titles VALUES (?,?,?,?)',
                           (position,rowid,extract_issue_id_from_text(content),extract_title_from_chroma_text(content)))
                count+=1
            if signature!=source_signature(source):
                return {'status':'source_advanced'}
            db.execute('INSERT INTO metadata VALUES (?)',[json.dumps(
                {'version':1,'signature':json.dumps(signature),'count':count})])
        os.replace(temporary,target)
        return {'status':'built','count':count,'bytes':target.stat().st_size}
    finally:
        temporary.unlink(missing_ok=True)
