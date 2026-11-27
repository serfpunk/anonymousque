import asyncio, uuid, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from unittest.mock import AsyncMock
import asyncpg
from aiogram import Bot, Dispatcher
from aiogram.types import Update, Message, Chat, User, CallbackQuery
import main, admin, db

async def run():
    uri=os.environ.get('TEST_DATABASE_URL')
    if not uri:
        raise SystemExit('Set TEST_DATABASE_URL to a disposable PostgreSQL database')
    conn=await asyncpg.connect(uri); schema='test_'+uuid.uuid4().hex
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    pool=await asyncpg.create_pool(uri,server_settings={'search_path':schema})
    await pool.execute(db.SCHEMA)
    bot=Bot('123456:abcdefghijklmnopqrstuvwxyzABCDE')
    sent=[]
    async def fake(bot_,method,timeout=None):
        sent.append(method)
        if method.__api_method__ == 'getMe': return User(id=123456,is_bot=True,first_name='bot',username='test_bot')
        return True
    bot.session.make_request=fake
    dp=Dispatcher(); dp.message.outer_middleware(admin.TrackUsers()); dp.callback_query.outer_middleware(admin.TrackUsers()); dp.include_router(admin.router); dp.include_router(main.r)
    wakeup=asyncio.Event()
    opts=dict(pool=pool,rate_limit=60,admin_ids=frozenset({1}),broadcast_wakeup=wakeup)
    def msg(uid,text,num):
        return Update(update_id=num,message=Message(message_id=num,date=1700000000,chat=Chat(id=uid,type='private'),from_user=User(id=uid,is_bot=False,first_name='U'),text=text))
    def cb(uid,data,num):
        m=Message(message_id=1,date=1700000000,chat=Chat(id=uid,type='private'),from_user=User(id=123456,is_bot=True,first_name='bot'),text='menu')
        return Update(update_id=num,callback_query=CallbackQuery(id=str(num),from_user=User(id=uid,is_bot=False,first_name='U'),chat_instance='x',data=data,message=m))
    try:
        await dp.feed_update(bot,msg(2,'/start',1),**opts)
        assert not await pool.fetchval('SELECT advertising_opt_in FROM bot_users WHERE user_id=2')
        await dp.feed_update(bot,msg(1,'/admin',4),**opts)
        await dp.feed_update(bot,cb(1,'adm:ads',5),**opts)
        await dp.feed_update(bot,msg(1,'Рекламный текст',6),**opts)
        preview=sent[-1]; callback=preview.reply_markup.inline_keyboard[0][0].callback_data
        await dp.feed_update(bot,cb(1,callback,7),**opts)
        job=await pool.fetchrow('SELECT * FROM broadcasts')
        recipients=await pool.fetch('SELECT user_id FROM broadcast_recipients')
        assert job['category']=='ads' and sorted(x['user_id'] for x in recipients)==[1,2]
        task=asyncio.create_task(admin.broadcast_worker(bot,pool,wakeup))
        for _ in range(30):
            if await pool.fetchval("SELECT status FROM broadcasts WHERE id=$1",job['id'])=='completed': break
            await asyncio.sleep(.1)
        assert await pool.fetchval("SELECT COUNT(*) FROM broadcast_recipients WHERE broadcast_id=$1 AND status='sent'",job['id'])==2
        assert any(getattr(x,'chat_id',None)==2 and 'Рекламный текст' in (getattr(x,'text','') or '') for x in sent)
        task.cancel(); await asyncio.gather(task,return_exceptions=True)
        print('Dispatch end-to-end: compose, confirm, queue, delivery OK')
    finally:
        await bot.session.close(); await pool.close(); await conn.execute(f'DROP SCHEMA "{schema}" CASCADE'); await conn.close()
asyncio.run(run())
