"""One-time import of the original project's SQLite into an EMPTY PostgreSQL database."""
import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timezone

import db


def dt(value):
    if not value:
        return datetime.now(timezone.utc)
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)

async def migrate(path, url):
    # mode=ro prevents a mistyped filename from creating an empty SQLite file.
    from pathlib import Path
    src = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    src.row_factory = sqlite3.Row
    users = list(src.execute('SELECT * FROM users'))
    questions = list(src.execute('SELECT * FROM questions'))
    bans = list(src.execute('SELECT * FROM bans'))
    pool = await db.create_pool(url)
    try:
        async with pool.acquire() as con, con.transaction():
            counts = await con.fetchrow('SELECT (SELECT count(*) FROM creators) AS creators, (SELECT count(*) FROM questions) AS questions, (SELECT count(*) FROM bans) AS bans')
            if any(counts.values()):
                raise RuntimeError('Target must be empty; import stopped without overwriting data.')
            for u in users:
                await con.execute('INSERT INTO creators(user_id,public_code,channel_id,channel_title,created_at) VALUES($1,$2,$3,$4,$5)',
                                  u['user_id'],u['public_code'],u['channel_id'],u['channel_title'],dt(u['created_at']))
            for q in questions:
                if q['media_type'] not in ('text','photo','voice'):
                    raise ValueError(f'Unsupported legacy media type for question {q["id"]}')
                # Old DB has only an answered flag, not the actual answer: cannot migrate answer content.
                # Mark legacy answers explicitly so they are not advertised as pending.
                await con.execute('''INSERT INTO questions(id,asker_id,creator_id,kind,file_id,body,delivery_status,created_at,legacy_answered)
                    VALUES($1,$2,$3,$4,$5,$6,'sent',$7,$8)''', q['id'], q['asker_id'], q['recipient_id'], q['media_type'], q['file_id'], q['text'] or '', dt(q['created_at']), bool(q['answered']))
            for b in bans:
                await con.execute('INSERT INTO bans(creator_id,asker_id) VALUES($1,$2)', b['recipient_id'],b['banned_user_id'])
            await con.execute("SELECT setval(pg_get_serial_sequence('questions','id'), COALESCE((SELECT MAX(id) FROM questions), 1), EXISTS(SELECT 1 FROM questions))")
        print(f'Imported {len(users)} creators, {len(questions)} questions, {len(bans)} bans.')
        print('Legacy answer content was not stored in SQLite; it cannot be recovered.')
    finally:
        src.close()
        await pool.close()

if __name__ == '__main__':
    if len(sys.argv) != 2 or not os.getenv('DATABASE_URL'):
        raise SystemExit('Usage: set DATABASE_URL, then python migrate_sqlite.py /path/to/backup.db')
    asyncio.run(migrate(sys.argv[1], os.environ['DATABASE_URL']))
