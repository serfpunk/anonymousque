"""PostgreSQL repository. Never logs question text, Telegram IDs or credentials."""
import secrets
import logging
import ssl
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import asyncpg

SCHEMA = '''
CREATE TABLE IF NOT EXISTS bot_users (
    user_id BIGINT PRIMARY KEY,
    first_seen TIMESTAMPTZ,
    last_seen TIMESTAMPTZ,
    service_opt_in BOOLEAN NOT NULL DEFAULT true,
    advertising_opt_in BOOLEAN NOT NULL DEFAULT false,
    blocked BOOLEAN NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS broadcasts (
    id TEXT PRIMARY KEY,
    admin_id BIGINT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('service', 'ads')),
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','completed','cancelled')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS broadcast_recipients (
    broadcast_id TEXT NOT NULL REFERENCES broadcasts(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES bot_users(user_id),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sending','sent','failed','skipped','unknown')),
    PRIMARY KEY (broadcast_id,user_id)
);
CREATE INDEX IF NOT EXISTS broadcast_recipients_pending_idx ON broadcast_recipients(broadcast_id,status,user_id);
CREATE TABLE IF NOT EXISTS creators (
    user_id BIGINT PRIMARY KEY,
    public_code TEXT NOT NULL UNIQUE,
    channel_id BIGINT,
    channel_title TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS questions (
    id BIGSERIAL PRIMARY KEY,
    asker_id BIGINT NOT NULL,
    creator_id BIGINT NOT NULL REFERENCES creators(user_id),
    kind TEXT NOT NULL CHECK (kind IN ('text', 'photo', 'voice', 'video')),
    file_id TEXT,
    body TEXT NOT NULL DEFAULT '',
    answer_kind TEXT CHECK (answer_kind IN ('text', 'photo', 'voice', 'video')),
    answer_file_id TEXT,
    answer_body TEXT,
    answered_at TIMESTAMPTZ,
    legacy_answered BOOLEAN NOT NULL DEFAULT false,
    delivery_status TEXT NOT NULL DEFAULT 'pending',
    answer_delivery_status TEXT NOT NULL DEFAULT 'pending',
    answer_claimed_at TIMESTAMPTZ,
    published_channel_id BIGINT,
    published_question_message_id BIGINT,
    published_answer_message_id BIGINT,
    publish_claimed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE creators ADD COLUMN IF NOT EXISTS accepting_questions BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE creators ADD COLUMN IF NOT EXISTS allow_text BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE creators ADD COLUMN IF NOT EXISTS allow_photo BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE creators ADD COLUMN IF NOT EXISTS allow_voice BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE creators ADD COLUMN IF NOT EXISTS allow_video BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE creators ADD COLUMN IF NOT EXISTS greeting TEXT NOT NULL DEFAULT '';
ALTER TABLE questions ADD COLUMN IF NOT EXISTS skipped BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE questions ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;
DO $migration$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='questions'::regclass AND conname='questions_kind_check' AND pg_get_constraintdef(oid) LIKE '%video%') THEN
        ALTER TABLE questions DROP CONSTRAINT IF EXISTS questions_kind_check;
        ALTER TABLE questions ADD CONSTRAINT questions_kind_check CHECK (kind IN ('text','photo','voice','video'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='questions'::regclass AND conname='questions_answer_kind_check' AND pg_get_constraintdef(oid) LIKE '%video%') THEN
        ALTER TABLE questions DROP CONSTRAINT IF EXISTS questions_answer_kind_check;
        ALTER TABLE questions ADD CONSTRAINT questions_answer_kind_check CHECK (answer_kind IN ('text','photo','voice','video'));
    END IF;
END $migration$;
CREATE INDEX IF NOT EXISTS questions_creator_id_idx ON questions(creator_id, created_at DESC);
CREATE TABLE IF NOT EXISTS bans (
    creator_id BIGINT NOT NULL REFERENCES creators(user_id),
    asker_id BIGINT NOT NULL,
    PRIMARY KEY(creator_id, asker_id)
);
CREATE TABLE IF NOT EXISTS rate_limits (
    creator_id BIGINT NOT NULL,
    asker_id BIGINT NOT NULL,
    last_sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(creator_id, asker_id)
);
'''

def connection_parameters(url):
    """Neon requires TLS. asyncpg does not implement SCRAM channel binding."""
    parsed = urlsplit(url)
    opts = {}
    if parsed.hostname and parsed.hostname.endswith('.neon.tech'):
        parts = parse_qsl(parsed.query, keep_blank_values=True)
        if any(k == 'sslmode' and v not in ('require', 'verify-ca', 'verify-full') for k, v in parts):
            raise ValueError('Neon connections require TLS')
        if any(k == 'channel_binding' for k, _ in parts):
            logging.getLogger('database').warning('asyncpg does not support channel binding; using verified TLS instead')
            parts = [(k,v) for k,v in parts if k != 'channel_binding']
        url = urlunsplit((parsed.scheme,parsed.netloc,parsed.path,urlencode(parts),parsed.fragment))
        opts['ssl'] = ssl.create_default_context()
    return url, opts

async def create_pool(url):
    url, opts = connection_parameters(url)
    pool = await asyncpg.create_pool(url, min_size=0, max_size=5, command_timeout=30,
        timeout=60, max_inactive_connection_lifetime=60, **opts)
    async with pool.acquire() as con:
        await con.execute(SCHEMA)
        # Import known Q&A users. Legacy preference columns are retained but not used.
        await con.execute('''INSERT INTO bot_users(user_id)
            SELECT user_id FROM creators UNION SELECT asker_id FROM questions
            ON CONFLICT(user_id) DO NOTHING''')
    return pool

async def register(pool, uid):
    # Collision on the random public code is extremely unlikely, but handle it.
    for _ in range(5):
        try:
            return await pool.fetchrow('''INSERT INTO creators(user_id,public_code) VALUES($1,$2)
                ON CONFLICT(user_id) DO UPDATE SET user_id=EXCLUDED.user_id RETURNING *''',
                uid, secrets.token_urlsafe(9))
        except asyncpg.UniqueViolationError:
            continue
    raise RuntimeError('Could not generate a unique creator code')

async def by_code(pool, code):
    return await pool.fetchrow('SELECT * FROM creators WHERE public_code=$1', code)

async def creator(pool, uid):
    return await pool.fetchrow('SELECT * FROM creators WHERE user_id=$1', uid)

async def set_channel(pool, uid, channel_id, title):
    await pool.execute('UPDATE creators SET channel_id=$2,channel_title=$3 WHERE user_id=$1', uid, channel_id, title)

async def is_banned(pool, creator_id, asker_id):
    return await pool.fetchval('SELECT EXISTS(SELECT 1 FROM bans WHERE creator_id=$1 AND asker_id=$2)', creator_id, asker_id)

async def toggle_ban(pool, creator_id, asker_id):
    async with pool.acquire() as con, con.transaction():
        removed = await con.fetchval('DELETE FROM bans WHERE creator_id=$1 AND asker_id=$2 RETURNING true', creator_id, asker_id)
        if removed:
            return False
        await con.execute('INSERT INTO bans(creator_id,asker_id) VALUES($1,$2) ON CONFLICT DO NOTHING', creator_id, asker_id)
        return True

async def save_question(pool, asker_id, creator_id, kind, file_id, body, interval):
    # Atomic database throttle across restarts; block list checked again at submission.
    async with pool.acquire() as con, con.transaction():
        settings = await con.fetchrow('SELECT * FROM creators WHERE user_id=$1 FOR SHARE', creator_id)
        if not settings or not settings['accepting_questions']:
            return 'closed', None
        if kind not in ('text','photo','voice','video') or not settings['allow_' + kind]:
            return 'type_disabled', None
        if await con.fetchval('SELECT EXISTS(SELECT 1 FROM bans WHERE creator_id=$1 AND asker_id=$2)', creator_id, asker_id):
            return 'banned', None
        allowed = await con.fetchval('''INSERT INTO rate_limits(creator_id,asker_id,last_sent_at) VALUES($1,$2,now())
            ON CONFLICT(creator_id,asker_id) DO UPDATE SET last_sent_at=now()
            WHERE rate_limits.last_sent_at <= now() - ($3 * interval '1 second') RETURNING true''', creator_id, asker_id, interval)
        if not allowed:
            return 'rate_limited', None
        q = await con.fetchrow('''INSERT INTO questions(asker_id,creator_id,kind,file_id,body)
            VALUES($1,$2,$3,$4,$5) RETURNING *''', asker_id, creator_id, kind, file_id, body)
        return 'ok', q

async def question(pool, qid, creator_id):
    return await pool.fetchrow('SELECT * FROM questions WHERE id=$1 AND creator_id=$2', qid, creator_id)

async def save_answer(pool, qid, uid, kind, file_id, body):
    return await pool.fetchrow('''UPDATE questions SET answer_kind=$3, answer_file_id=$4, answer_body=$5,
        answered_at=now(), answer_delivery_status='pending' WHERE id=$1 AND creator_id=$2 AND answer_kind IS NULL AND NOT legacy_answered AND NOT skipped RETURNING *''',
        qid, uid, kind, file_id, body)

async def claim_answer(pool, qid, uid):
    return await pool.fetchrow('''UPDATE questions SET answer_delivery_status='sending',answer_claimed_at=now()
        WHERE id=$1 AND creator_id=$2 AND answer_kind IS NOT NULL AND
        (answer_delivery_status IN ('pending','failed') OR (answer_delivery_status='sending' AND answer_claimed_at < now()-interval '5 minutes'))
        RETURNING *''', qid, uid)

async def claim_publish(pool, qid, uid):
    return await pool.fetchrow('''UPDATE questions SET publish_claimed_at=now() WHERE id=$1 AND creator_id=$2
        AND answer_kind IS NOT NULL AND published_answer_message_id IS NULL
        AND (publish_claimed_at IS NULL OR publish_claimed_at < now()-interval '5 minutes') RETURNING *''', qid, uid)
