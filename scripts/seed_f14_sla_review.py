import asyncio, json, os, secrets, uuid
from sqlalchemy.ext.asyncio import create_async_engine
from app.core.security import hash_password
from app.db.models import User, AccessGroup, AccessGroupMember
from sqlalchemy import insert
async def main():
    url=os.environ['DATABASE_URL']
    assert url=='postgresql+asyncpg://postgres@127.0.0.1:55888/infraspine_f14browser_27b1_test'
    a={'email':'f14-admin@example.test','password':secrets.token_urlsafe(24)}
    e={'email':'f14-employee@example.test','password':secrets.token_urlsafe(24)}
    ai,ei,gi=uuid.uuid4(),uuid.uuid4(),uuid.uuid4()
    engine=create_async_engine(url)
    async with engine.begin() as c:
        await c.execute(insert(User),[{'id':ai,'email':a['email'],'password_hash':hash_password(a['password']),'role':'admin'}, {'id':ei,'email':e['email'],'password_hash':hash_password(e['password']),'role':'employee'}])
        await c.execute(insert(AccessGroup).values(id=gi,name='Synthetic F14 Operations',kind='department',source='local',created_by=ai))
        await c.execute(insert(AccessGroupMember).values(group_id=gi,user_id=ei,added_by=ai))
    await engine.dispose()
    with open(os.environ['F14_FIXTURE'],'x',encoding='utf-8') as f: json.dump({'admin':a,'employees':[e]},f)
    print('F14_SEED=OK')
asyncio.run(main())
