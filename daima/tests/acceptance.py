"""真实本地模型验收。直接运行；正式数据不参与，结果保存在 noim。"""
import os
from pathlib import Path
import sys
import json
import time
import asyncio

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT.parent/'noim'/'real-acceptance'
TARGET.mkdir(parents=True, exist_ok=True)
os.environ['XUEJI_DATA_DIR'] = str(TARGET)
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
sys.path.insert(0,str(ROOT))
sys.stdout.reconfigure(encoding='utf-8',errors='replace')

async def main():
    import httpx
    from api.app import app
    from rag.kb_manager import get_kb_manager
    report=[]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',timeout=300) as client:
        sid='real-'+str(int(time.time()))
        await client.post('/sessions',json={'session_id':sid,'title':'隔离真实模型验收'})
        async def check(name, path, body):
            start=time.perf_counter()
            res=await client.post(path,json=body)
            report.append({'name':name,'status':res.status_code,'seconds':round(time.perf_counter()-start,2),'result':res.json()})
            (TARGET/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(name,res.status_code,report[-1]['seconds'],flush=True)
            return res.json()
        await check('空库不调用模型','/chat/sync',{'session_id':sid,'message':'采购审批需要几级签字？'})
        text='# 采购管理\n### 审批职责\n采购审批需要两级签字。第一级由采购主管审核需求，第二级由财务负责人复核预算。未完成两级签字，不得下达采购订单。\n### 归档\n采购合同由档案管理员保存，保存期限为五年。合同归档后应登记编号、签订日期和保管人。'
        upload=await client.post('/documents',params={'session_id':sid},files={'file':('采购讲义.md',text.encode(),'text/markdown')})
        doc=upload.json()
        for _ in range(180):
            data=(await client.get('/documents',params={'session_id':sid})).json()['docs']
            if data and data[0]['status']!='processing':break
            await asyncio.sleep(1)
        report.append({'name':'真实嵌入索引','result':data})
        if not data or data[0]['status']!='ready':raise RuntimeError('索引失败或超时')
        answer=await check('资料内回答','/chat/sync',{'session_id':sid,'message':'采购审批需要几级签字，各由谁负责？'})
        await check('资料外拦截','/chat/sync',{'session_id':sid,'message':'光合作用的光反应发生在哪里？'})
        await check('明确同意通用回答','/chat/sync',{'session_id':sid,'message':'光合作用的光反应发生在哪里？','allow_general':True})
        if answer.get('answer'):
            card=await check('手动收录','/mistakes',{'session_id':sid,'question':'采购审批需要几级签字，各由谁负责？','answer':answer['answer']})
            if card.get('id'):
                await check('按错题出题','/exam/from-mistakes',{'session_id':sid,'mistake_ids':[card['id']],'count_per':1})
        await check('按主题出题','/exam/from-topic',{'session_id':sid,'topic':'采购审批','count':2})
        deletion=await client.delete('/sessions/'+sid)
        report.append({'name':'删除验收对话及资料','status':deletion.status_code,'remaining_vectors':get_kb_manager().collection.get(where={'doc_id':doc['doc_id']},include=[])['ids']})
        (TARGET/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__':asyncio.run(main())
