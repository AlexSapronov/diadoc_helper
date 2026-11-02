import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from .config import DATABASE_PATH, DATA_DIR


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def conn():
    c = sqlite3.connect(DATABASE_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA busy_timeout=20000')
    return c


@contextmanager
def database():
    c = conn()
    try:
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with database() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS secrets (
            key TEXT PRIMARY KEY,
            encrypted_value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS organizations (
            box_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            inn TEXT NOT NULL DEFAULT '',
            kpp TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 0,
            mail_recipients TEXT NOT NULL DEFAULT '',
            last_sync TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS groups (
            box_id TEXT NOT NULL,
            group_id TEXT NOT NULL,
            name TEXT NOT NULL,
            PRIMARY KEY(box_id, group_id),
            FOREIGN KEY(box_id) REFERENCES organizations(box_id)
        );
        CREATE TABLE IF NOT EXISTS counterparties (
            box_id TEXT NOT NULL,
            cp_box_id TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            inn TEXT NOT NULL DEFAULT '',
            kpp TEXT NOT NULL DEFAULT '',
            direction TEXT NOT NULL DEFAULT 'unknown',
            status TEXT NOT NULL,
            current_group_id TEXT NOT NULL DEFAULT '',
            invitation_doc_id TEXT NOT NULL DEFAULT '',
            last_event_ticks TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(box_id, cp_box_id),
            FOREIGN KEY(box_id) REFERENCES organizations(box_id)
        );
        CREATE TABLE IF NOT EXISTS intents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            box_id TEXT NOT NULL,
            cp_box_id TEXT NOT NULL DEFAULT '',
            cp_inn TEXT NOT NULL DEFAULT '',
            cp_name TEXT NOT NULL DEFAULT '',
            direction TEXT NOT NULL CHECK(direction IN ('incoming','outgoing')),
            target_group_id TEXT NOT NULL,
            task_id TEXT NOT NULL DEFAULT '',
            send_state TEXT NOT NULL DEFAULT 'pending',
            assign_state TEXT NOT NULL DEFAULT 'pending',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(box_id) REFERENCES organizations(box_id)
        );
        CREATE INDEX IF NOT EXISTS idx_intents_pending ON intents(box_id, assign_state);
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            box_id TEXT NOT NULL,
            cp_box_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            read_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(box_id) REFERENCES organizations(box_id)
        );
        CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            box_id TEXT NOT NULL,
            cp_box_id TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        ''')


def rows(query, params=()):
    with database() as c:
        return [dict(r) for r in c.execute(query, params).fetchall()]


def one(query, params=()):
    with database() as c:
        r = c.execute(query, params).fetchone()
        return dict(r) if r else None


def audit(c, box_id, cp_box_id, action, detail=''):
    c.execute('INSERT INTO audit(box_id, cp_box_id, action, detail, created_at) VALUES(?,?,?,?,?)',
              (box_id, cp_box_id, action, detail[:300], utcnow()))
