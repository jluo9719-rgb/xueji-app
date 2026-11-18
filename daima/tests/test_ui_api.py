import os
assert os.environ.get('XUEJI_DATA_DIR'), '请使用 python -B tests/run.py，避免写入正式数据'
import asyncio, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.config import config
config._data['redis']['enabled']=False
from core.mistakes import MistakeBook
from api.app import app
import httpx

async def main():
    book=MistakeBook(':memory:')
    messages=[{'role':'user','content':'问题一'},{'role':'assistant','content':'回答一'},
              {'role':'user','content':'问题二'},{'role':'assistant','content':'回答二'}]
    engine=SimpleNamespace(mistakes=book,session=SimpleNamespace(load_messages=lambda sid: messages),session_title=lambda sid:'测试')
    judge=AsyncMock(return_value={'topic':'测试','confusion':'测试混淆'})
    with patch('api.app._get_engine',AsyncMock(return_value=engine)),patch('api.app.judge_exchange',judge):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
            body={'session_id':'test','question':'问题一','answer':'回答一'}
            r=await c.post('/mistakes',json=body);assert r.status_code==200,r.text
            mid=r.json()['id'];assert r.json()['question']=='问题一'
            r=await c.post('/mistakes',json=body);assert r.json()['id']==mid
            assert judge.await_count==1
            r=await c.get('/mistakes');assert len(r.json()['items'])==1
            r=await c.delete('/mistakes/'+str(mid));assert r.status_code==200
            assert (await c.get('/mistakes')).json()['stats']['total']==0
            r=await c.post('/mistakes',json={**body,'answer':'伪造回答'});assert r.status_code==409
            r=await c.post('/mistakes',json={'session_id':'test'});assert r.json()['question']=='问题二'
            r=await c.get('/');assert r.status_code==200
            assert '上传文件' in r.text and '已收录 · 点击取消' in r.text and '新對話' not in r.text
            print('PASS: clicked exchange, duplicate prevention, list state, cancel, invalid pair, legacy request, page contents')
    class KB:
        def add_document(self,name,body,kb_id):
            assert name=='test.txt' and body==b'learning';return {'doc_id':'test','name':name,'status':'processing'}
        def list_documents(self,kb_id):return [{'doc_id':'test','name':'test.txt','status':'ready','progress':100}]
    with patch('api.app.get_kb_manager',return_value=KB()):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
            r=await c.post('/documents',files={'file':('test.txt',b'learning','text/plain')});assert r.status_code==200
            r=await c.get('/documents');assert r.json()['docs'][0]['status']=='ready'
            print('PASS: multipart upload and document status API contract (mock indexer)')
    book.close()
asyncio.run(main())
