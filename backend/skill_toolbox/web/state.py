from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import time

ACTIVE = ('queued', 'running', 'waiting')


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'website.sqlite3'
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS invites (
                    id TEXT PRIMARY KEY, quota INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, created REAL NOT NULL,
                    status TEXT NOT NULL, payload TEXT NOT NULL, snapshot TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS feedback (
                    job TEXT PRIMARY KEY, rating INTEGER NOT NULL, text TEXT NOT NULL);
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def invite(self, quota: int) -> str:
        code = secrets.token_urlsafe(18)
        with self.db() as db:
            db.execute('INSERT INTO invites(id,quota) VALUES (?,?)', (digest(code), quota))
        return code

    def login(self, code: str) -> str | None:
        with self.db() as db:
            owner = digest(code.strip())
            if not db.execute('SELECT id FROM invites WHERE id=?', (owner,)).fetchone():
                return None
            token = secrets.token_urlsafe(32)
            db.execute('INSERT INTO sessions VALUES (?,?,?)', (digest(token), owner, time.time() + 7*86400))
            return token

    def owner(self, token: str) -> str | None:
        with self.db() as db:
            row = db.execute('SELECT owner FROM sessions WHERE token=? AND expires>?',
                             (digest(token), time.time())).fetchone()
            return row['owner'] if row else None

    def quota(self, owner: str) -> dict:
        with self.db() as db:
            return dict(db.execute('SELECT quota,used FROM invites WHERE id=?', (owner,)).fetchone())

    def jobs(self, owner: str | None = None) -> list[dict]:
        with self.db() as db:
            rows = db.execute('SELECT * FROM jobs' + (' WHERE owner=?' if owner else '') + ' ORDER BY created DESC',
                              (owner,) if owner else ()).fetchall()
            return [self.decode(row) for row in rows]

    @staticmethod
    def decode(row) -> dict:
        result = dict(row)
        for key in ('payload', 'snapshot'):
            result[key] = json.loads(result[key])
        return result

    def get(self, job_id: str, owner: str | None = None) -> dict | None:
        with self.db() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?' + (' AND owner=?' if owner else ''),
                             (job_id, owner) if owner else (job_id,)).fetchone()
            return self.decode(row) if row else None

    def create(self, owner: str, job_id: str, payload: dict, queue_limit: int, *, concurrency: int = 1, unlimited: bool = False):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            invite = db.execute('SELECT * FROM invites WHERE id=?', (owner,)).fetchone()
            if not unlimited and invite['used'] >= invite['quota']:
                raise ValueError('邀请码次数已用完，请联系网站管理员。')
            placeholders = ','.join('?' for _ in ACTIVE)
            if not unlimited and db.execute(f'SELECT 1 FROM jobs WHERE owner=? AND status IN ({placeholders})', (owner, *ACTIVE)).fetchone():
                raise ValueError('你已有一个未结束的任务，请先完成或取消。')
            count = db.execute(f'SELECT count(*) FROM jobs WHERE status IN ({placeholders})', ACTIVE).fetchone()[0]
            if count >= queue_limit + concurrency:
                raise ValueError('当前队列已满，请稍后再试。')
            snapshot = {'message': '等待生成', 'questions': [], 'artifacts': [], 'events': [], 'error': None}
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?)',
                       (job_id, owner, time.time(), 'queued', json.dumps(payload), json.dumps(snapshot)))
            db.execute('UPDATE invites SET used=used+1 WHERE id=?', (owner,))

    def update(self, job_id: str, status: str | None = None, **changes):
        with self.db() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not row:
                return
            snapshot = json.loads(row['snapshot'])
            snapshot.update(changes)
            new_status = status or row['status']
            if row['status'] == 'queued' and new_status == 'running':
                snapshot.setdefault('started_at', time.time())
            if new_status in ('completed', 'failed', 'cancelled') and row['status'] in ACTIVE:
                snapshot['ended_at'] = time.time()
            if row['status'] in ACTIVE and new_status in ('failed', 'cancelled'):
                db.execute('UPDATE invites SET used=max(0,used-1) WHERE id=?', (row['owner'],))
            db.execute('UPDATE jobs SET status=?,snapshot=? WHERE id=?',
                       (new_status, json.dumps(snapshot, ensure_ascii=False), job_id))

    def feedback(self, job_id: str, rating: int, text: str):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO feedback VALUES (?,?,?)', (job_id, rating, text))
