import asyncio, uuid, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asyncpg
from aiogram import Bot, Dispatcher
from aiogram.types import Update, Message, Chat, User, CallbackQuery
import main, admin, db

async def run():
    uri=os.environ.get('TEST_DATABASE_URL')
    if not uri: raise SystemExit('Set TEST_DATABASE_URL to a disposable PostgreSQL database')
    conn=await asyncpg.connect(uri); schema='support_'+uuid.uuid4().hex
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    pool=await asyncpg.create_pool(uri,server_settings={'search_path':schema}); await pool.execute(db.SCHEMA)
    bot=Bot('123456:abcdefghijklmnopqrstuvwxyzABCDE'); sent=[]
    async def fake(bot_,method,timeout=None):
        sent.append(method)
        if method.__api_method__=='getMe': return User(id=123456,is_bot=True,first_name='bot',username='test_bot')
        return True
    bot.session.make_request=fake
    dp=Dispatcher(); dp.message.outer_middleware(admin.TrackUsers()); dp.callback_query.outer_middleware(admin.TrackUsers())
    dp.include_router(admin.router); dp.include_router(main.r)
    opts=dict(pool=pool,rate_limit=60,admin_ids=frozenset({1}),broadcast_wakeup=asyncio.Event())
    def msg(uid,text,num):
        return Update(update_id=num,message=Message(message_id=num,date=1700000000,chat=Chat(id=uid,type='private'),from_user=User(id=uid,is_bot=False,first_name='U'),text=text))
    def cb(uid,data,num):
        m=Message(message_id=1,date=1700000000,chat=Chat(id=uid,type='private'),from_user=User(id=123456,is_bot=True,first_name='bot'),text='menu')
        return Update(update_id=num,callback_query=CallbackQuery(id=str(num),from_user=User(id=uid,is_bot=False,first_name='U'),chat_instance='x',data=data,message=m))
    try:
        await dp.feed_update(bot,msg(2,'/support',1),**opts)
        assert 'Telegram ID' in sent[-1].text
        await dp.feed_update(bot,msg(2,'Нужна помощь',2),**opts)
        assert any(getattr(x,'chat_id',None)==1 and 'Нужна помощь' in (getattr(x,'text',None) or '') for x in sent)
        await dp.feed_update(bot,cb(1,'support:reply:2',3),**opts)
        await dp.feed_update(bot,msg(1,'Подсказываю',4),**opts)
        assert any(getattr(x,'chat_id',None)==2 and 'Подсказываю' in (getattr(x,'text',None) or '') for x in sent)
        await dp.feed_update(bot,msg(2,'/support',5),**opts)
        await dp.feed_update(bot,msg(2,'/start',6),**opts)
        assert 'Кабинет креатора' in sent[-1].text
        print('Support dispatch end-to-end: request, reply and /start OK')
    finally:
        await bot.session.close(); await pool.close(); await conn.execute(f'DROP SCHEMA "{schema}" CASCADE'); await conn.close()
asyncio.run(run())
