import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import iconv from 'iconv-lite';
import JSZip from 'jszip';

const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'xueji-acceptance-'));
process.env.XUEJI_DATA_ROOT = tmpRoot;
process.env.EVIDENCE_MIN_SCORE = '0.12';

let server: http.Server;
let baseUrl = '';

async function startTestServer(createApp: () => any) {
  const app = createApp();
  await new Promise<void>((resolve) => {
    server = app.listen(0, '127.0.0.1', () => {
      const addr = server.address() as { port: number };
      baseUrl = `http://127.0.0.1:${addr.port}`;
      resolve();
    });
  });
}

async function stopTestServer() {
  if (server) {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
}

async function apiJson(urlPath: string, options: RequestInit = {}) {
  const resp = await fetch(`${baseUrl}${urlPath}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
  });
  const text = await resp.text();
  let json: any = null;
  try {
    json = JSON.parse(text);
  } catch {
    json = text;
  }
  return { status: resp.status, data: json, headers: resp.headers };
}

async function uploadFile(sessionId: string, filename: string, content: Buffer) {
  const form = new FormData();
  form.append('file', new Blob([new Uint8Array(content)]), filename);
  const resp = await fetch(`${baseUrl}/documents?session_id=${encodeURIComponent(sessionId)}`, {
    method: 'POST',
    body: form,
  });
  return { status: resp.status, data: await resp.json() };
}

async function makeDocxBuffer(paragraphText: string, tableRow: string[]): Promise<Buffer> {
  const zip = new JSZip();
  const xml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>${paragraphText}</w:t></w:r></w:p>
    <w:tbl>
      <w:tr>
        ${tableRow.map((c) => `<w:tc><w:p><w:r><w:t>${c}</w:t></w:r></w:p></w:tc>`).join('')}
      </w:tr>
    </w:tbl>
  </w:body>
</w:document>`;
  zip.file('word/document.xml', xml);
  return zip.generateAsync({ type: 'nodebuffer' });
}

async function runTests() {
  console.log(`Running A1-A11 with XUEJI_DATA_ROOT=${tmpRoot}`);
  const { createApp } = await import('../server.js');
  const { setLlmCallerForTest } = await import('../src/llm.js');
  const rag = await import('../src/rag.js');
  const store = await import('../src/store.js');
  const { chunkDocument, reloadKbManager, retrieveEvidenceResult } = rag;
  const { reloadStores, sessionKbId } = store;

  reloadStores(tmpRoot);
  reloadKbManager(tmpRoot);
  await startTestServer(createApp);

  try {
    const health = await apiJson('/health');
    assert.equal(health.status, 200);
    assert.equal(health.data.data_dir, tmpRoot);
    assert.equal(health.data.session_storage, 'json-file');

    console.log('[A1] Document upload & chunking');
    await apiJson('/sessions', {
      method: 'POST',
      body: JSON.stringify({ session_id: 's_a1', title: 'A1 测试' }),
    });

    const mdContent = [
      '# 第一章 内部控制概述',
      '## 1.1 控制目标',
      '内部控制与审计部执行独立的内控鉴定程序，参照内控手册要求开展内控评审工作，对董事会审计委员会负责并向其报告工作。',
      '### 1.1.1 人员要求',
      '内部审计人员应当具备从事审计工作所需要的专业能力，具有经营管理知识，掌握会计、审计理论和技能。' +
        '此外，负责人应当具备审计、会计、经济、法律或者管理等工作背景。'.repeat(12),
    ].join('\n');

    const mdChunks = chunkDocument(mdContent);
    assert.ok(mdChunks.length >= 2, 'Markdown headings should produce structured chunks');
    for (const c of mdChunks) {
      assert.ok(c.length <= 800, `Chunk exceeded 800 chars: ${c.length}`);
      assert.ok(c.length >= 20, `Chunk too small: ${c.length}`);
    }

    const mdUpload = await uploadFile('s_a1', 'notes.md', Buffer.from(mdContent, 'utf8'));
    assert.equal(mdUpload.status, 200);
    await rag.kbManager.waitForIndexing(mdUpload.data.doc_id);

    const txtUpload = await uploadFile(
      's_a1',
      'utf8.txt',
      Buffer.from('审计抽样是指审计人员从总体中选取部分项目实施审计程序。', 'utf8')
    );
    assert.equal(txtUpload.status, 200);
    await rag.kbManager.waitForIndexing(txtUpload.data.doc_id);

    const docxBuf = await makeDocxBuffer('Word 正文：党委负责内控管理工作的领导。', ['项目', '说明']);
    const docxUpload = await uploadFile('s_a1', 'guide.docx', docxBuf);
    assert.equal(docxUpload.status, 200);
    await rag.kbManager.waitForIndexing(docxUpload.data.doc_id);

    const docsRes = await apiJson('/documents?session_id=s_a1');
    assert.equal(docsRes.data.docs.length, 3);
    for (const d of docsRes.data.docs) {
      assert.equal(d.status, 'ready', `Doc ${d.name} should be ready, got error: ${d.error}`);
      assert.ok(d.chunk_count > 0);
    }

    console.log('[A2] GBK, empty file, bad ext, duplicate');
    const gbkBuf = iconv.encode('这是GBK编码的学习资料：软件工程生命周期包含需求分析阶段。', 'gb18030');
    const gbkUp = await uploadFile('s_a1', 'gbk_notes.txt', gbkBuf);
    assert.equal(gbkUp.status, 200);
    await rag.kbManager.waitForIndexing(gbkUp.data.doc_id);
    const gbkDoc = (await apiJson('/documents?session_id=s_a1')).data.docs.find(
      (d: any) => d.doc_id === gbkUp.data.doc_id
    );
    assert.equal(gbkDoc.status, 'ready');

    const emptyUp = await uploadFile('s_a1', 'empty.txt', Buffer.alloc(0));
    assert.equal(emptyUp.status, 400);

    const exeUp = await uploadFile('s_a1', 'bad.exe', Buffer.from('MZ123'));
    assert.equal(exeUp.status, 400);
    assert.match(exeUp.data.detail, /不支持的格式/);

    const dupUp = await uploadFile('s_a1', 'notes_copy.md', Buffer.from(mdContent, 'utf8'));
    assert.equal(dupUp.status, 200);
    assert.equal(dupUp.data.duplicate, true);

    console.log('[A3] Session KB isolation');
    await apiJson('/sessions', {
      method: 'POST',
      body: JSON.stringify({ session_id: 's_a3_other', title: '隔离测试' }),
    });
    const otherUpload = await uploadFile(
      's_a3_other',
      'secret.txt',
      Buffer.from('绝密代号 XUEJI_9981 只属于隔离会话。', 'utf8')
    );
    await rag.kbManager.waitForIndexing(otherUpload.data.doc_id);

    const crossRet = retrieveEvidenceResult('s_a1', 'XUEJI_9981');
    assert.equal(crossRet.evidence.length, 0, 's_a1 must not see s_a3_other chunks');
    const ownRet = retrieveEvidenceResult('s_a3_other', 'XUEJI_9981');
    assert.ok(ownRet.evidence.length > 0, 's_a3_other must see its own chunk');

    console.log('[A4] Retrieval threshold gating');
    const mockChatLlm = (
      handler: Parameters<typeof setLlmCallerForTest>[0]
    ) => {
      setLlmCallerForTest(async (params) => {
        if (params.system?.includes('对话问题改写器')) {
          const content = String(params.messages[params.messages.length - 1]?.content || '');
          const match = /当前问题：(.+)/.exec(content);
          return { text: match ? match[1].trim() : content, stop_reason: 'end_turn' };
        }
        return handler!(params);
      });
    };

    let llmCallCount = 0;
    mockChatLlm(async () => {
      llmCallCount += 1;
      return { text: '内部控制与审计部对董事会审计委员会负责 [1]。', stop_reason: 'end_turn' };
    });

    const lowRes = await apiJson('/chat/sync', {
      method: 'POST',
      body: JSON.stringify({ session_id: 's_a1', message: '2024年巴黎奥运会举办城市是哪里' }),
    });
    assert.equal(lowRes.data.needs_general, true);
    assert.equal(llmCallCount, 0, 'Below threshold should NOT call LLM');

    const highRes = await apiJson('/chat/sync', {
      method: 'POST',
      body: JSON.stringify({ session_id: 's_a1', message: '内部控制与审计部向哪个机构负责并报告工作' }),
    });
    assert.equal(highRes.status, 200);
    assert.match(highRes.data.answer, /【依据当前对话资料】/);
    assert.equal(llmCallCount, 1);

    console.log('[A5] Citation, refusal & rollback');
    mockChatLlm(async () => ({ text: '没有标注编号的回答内容。', stop_reason: 'end_turn' }));
    const noRefRes = await apiJson('/chat/sync', {
      method: 'POST',
      body: JSON.stringify({ session_id: 's_a1', message: '内部控制与审计部的职责是什么' }),
    });
    assert.match(noRefRes.data.answer, /【出处未验证，请核对原文】/);
    assert.equal(noRefRes.data.meta.mode, 'unverified');
    assert.ok(!noRefRes.data.answer.includes('参考资料：'));

    mockChatLlm(async () => ({ text: '依据内容 [1][99]。', stop_reason: 'end_turn' }));
    const badRefRes = await apiJson('/chat/sync', {
      method: 'POST',
      body: JSON.stringify({ session_id: 's_a1', message: '内部审计人员有什么要求' }),
    });
    assert.ok(!badRefRes.data.answer.includes('[99]'));

    mockChatLlm(async () => ({ text: 'NO_DOCUMENT_EVIDENCE', stop_reason: 'end_turn' }));
    const refusalRes = await apiJson('/chat/sync', {
      method: 'POST',
      body: JSON.stringify({ session_id: 's_a1', message: '内部审计人员年龄上限是多少' }),
    });
    assert.equal(refusalRes.data.needs_general, true);

    mockChatLlm(async () => ({
      text: 'NO_DOCUMENT_EVIDENCE\n部分解答：内审人员需具备专业能力 [1]。',
      stop_reason: 'end_turn',
    }));
    const partialRes = await apiJson('/chat/sync', {
      method: 'POST',
      body: JSON.stringify({ session_id: 's_a1', message: '内审人员能力要求和薪资标准' }),
    });
    assert.match(partialRes.data.answer, /部分解答：内审人员需具备专业能力/);
    assert.ok(!partialRes.data.answer.includes('NO_DOCUMENT_EVIDENCE'));

    const beforeMsgs = (await apiJson('/sessions/s_a1/messages')).data.messages.length;
    mockChatLlm(async () => ({ text: '半截输出', stop_reason: 'max_tokens' }));
    const truncRes = await apiJson('/chat/sync', {
      method: 'POST',
      body: JSON.stringify({ session_id: 's_a1', message: '内部审计人员背景要求' }),
    });
    assert.equal(truncRes.status, 502);
    const afterMsgs = (await apiJson('/sessions/s_a1/messages')).data.messages.length;
    assert.equal(afterMsgs, beforeMsgs, 'Truncated response must roll back session messages');

    console.log('[A6] No-evidence & general confirmation');
    await apiJson('/sessions', {
      method: 'POST',
      body: JSON.stringify({ session_id: 's_a6', title: 'A6 测试' }),
    });
    const noDocChat = await apiJson('/chat/sync', {
      method: 'POST',
      body: JSON.stringify({ session_id: 's_a6', message: '什么是量子纠缠' }),
    });
    assert.equal(noDocChat.data.needs_general, true);

    setLlmCallerForTest(async () => ({
      text: '量子纠缠是量子力学中的关联现象。',
      stop_reason: 'end_turn',
    }));
    const genConfirm = await apiJson('/chat/sync', {
      method: 'POST',
      body: JSON.stringify({ session_id: 's_a6', message: '什么是量子纠缠', allow_general: true }),
    });
    assert.match(genConfirm.data.answer, /【模型通用知识回答，未经上传资料验证】/);

    const a6Msgs = (await apiJson('/sessions/s_a6/messages')).data.messages;
    const userCount = a6Msgs.filter((m: any) => m.role === 'user').length;
    assert.equal(userCount, 1, 'User question must not be duplicated on general confirm');
    assert.equal(a6Msgs[1].meta.mode, 'no_evidence');
    assert.equal(a6Msgs[2].meta.mode, 'general');

    console.log('[A7] Mistake book CRUD & export');
    setLlmCallerForTest(async () => ({
      text: '收录\n主题：量子纠缠\n混淆点：容易与经典物理中的相关概念混淆',
      stop_reason: 'end_turn',
    }));
    const mk1 = await apiJson('/mistakes', {
      method: 'POST',
      body: JSON.stringify({ session_id: 's_a6' }),
    });
    assert.equal(mk1.status, 200);
    assert.equal(mk1.data.topic, '量子纠缠');

    const mkDup = await apiJson('/mistakes', {
      method: 'POST',
      body: JSON.stringify({ session_id: 's_a6' }),
    });
    assert.equal(mkDup.data.id, mk1.data.id, 'Duplicate capture returns existing card');

    await apiJson(`/mistakes/${mk1.data.id}/status`, {
      method: 'PUT',
      body: JSON.stringify({ status: '已掌握' }),
    });
    const learnedList = await apiJson('/mistakes?status=' + encodeURIComponent('已掌握'));
    assert.equal(learnedList.data.items.length, 1);
    assert.equal(learnedList.data.stats.learned, 1);

    const exportResp = await fetch(`${baseUrl}/mistakes/export`);
    const exportMd = await exportResp.text();
    assert.match(exportMd, /我的错题本/);
    assert.match(exportMd, /量子纠缠/);

    console.log('[A8] Exam generation & validation');
    let examCall = 0;
    setLlmCallerForTest(async (params) => {
      examCall += 1;
      if (params.system?.includes('审题助手')) {
        throw new Error('Reviewer unavailable');
      }
      return {
        text: JSON.stringify([
          {
            type: 'choice',
            question: '内部控制与审计部向谁负责？',
            options: ['A. 管理层', 'B. 董事会审计委员会', 'C. 监事会', 'D. 财务部'],
            answer: 'B',
            explanation: '原文明确说明对董事会审计委员会负责。',
            evidence_ref: 1,
            quote: '对董事会审计委员会负责并向其报告工作',
          },
          {
            type: 'fill',
            question: '伪造引文测试',
            answer: '答案',
            explanation: '解析',
            evidence_ref: 1,
            quote: '这段引文根本不在原文里',
          },
        ]),
        stop_reason: 'end_turn',
      };
    });

    const examRes = await apiJson('/exam/from-topic', {
      method: 'POST',
      body: JSON.stringify({ session_id: 's_a1', topic: '内部控制', count: 2 }),
    });
    assert.equal(examRes.status, 200);
    assert.equal(examRes.data.count, 1, 'Bad quote question must be rejected');
    assert.equal(examRes.data.rejected, 1);
    assert.equal(examRes.data.review_status, 'unreviewed', 'Reviewer exception must mark unreviewed');
    assert.match(examRes.data.question_markdown, /尚未审核/);

    console.log('[A9] Cascade delete & 409 guard');
    const delDocRes = await apiJson(
      `/documents/${txtUpload.data.doc_id}?session_id=s_a1`,
      { method: 'DELETE' }
    );
    assert.equal(delDocRes.status, 200);

    rag.setIndexingDelayForTest(300);
    const procUp = await uploadFile('s_a6', 'proc.txt', Buffer.from('正在索引测试文本', 'utf8'));
    assert.equal(procUp.status, 200);
    assert.equal(procUp.data.status, 'processing');

    const guard409 = await apiJson(`/documents/${procUp.data.doc_id}?session_id=s_a6`, {
      method: 'DELETE',
    });
    assert.equal(guard409.status, 409);

    const sessionGuard409 = await apiJson('/sessions/s_a6', { method: 'DELETE' });
    assert.equal(sessionGuard409.status, 409);

    rag.setIndexingDelayForTest(0);
    await rag.kbManager.waitForIndexing(procUp.data.doc_id);
    const delProcAfterReady = await apiJson(`/documents/${procUp.data.doc_id}?session_id=s_a6`, {
      method: 'DELETE',
    });
    assert.equal(delProcAfterReady.status, 200);

    console.log('[A10] Restart persistence & stale recovery');
    const regPath = path.join(tmpRoot, 'kb_registry.json');
    const regRaw = JSON.parse(fs.readFileSync(regPath, 'utf8'));
    regRaw.docs.push({
      doc_id: 'fake_proc_doc',
      kb_id: sessionKbId('s_a6'),
      name: 'stale_proc.txt',
      ext: '.txt',
      status: 'processing',
      stage: '向量化中',
      progress: 50,
      chunk_count: 0,
      size: 10,
      uploaded_at: '2026-11-17T00:00:00',
      error: null,
    });
    fs.writeFileSync(regPath, JSON.stringify(regRaw), 'utf8');

    reloadStores(tmpRoot);
    reloadKbManager(tmpRoot);

    assert.ok(store.sessionStore.hasSession('s_a1'), 'Session s_a1 should persist across reload');
    assert.ok(store.sessionStore.loadMessages('s_a6').length === 3, 'Messages should persist across reload');
    assert.equal(store.mistakeBook.listAll().length, 1, 'Mistake cards should persist across reload');

    const recoveredDoc = rag.kbManager
      .listDocuments(sessionKbId('s_a6'))
      .find((d) => d.doc_id === 'fake_proc_doc');
    assert.equal(recoveredDoc?.status, 'error', 'Stale processing doc must be marked error on startup');

    const delSess = await apiJson('/sessions/s_a6', { method: 'DELETE' });
    assert.equal(delSess.status, 200);
    assert.equal(store.mistakeBook.listAll().length, 0, 'Cascade session delete must remove session mistakes');

    console.log('[A11] Non-blocking /sessions under slow exam call');
    setLlmCallerForTest(async (params) => {
      if (params.system?.includes('审题助手')) {
        return { text: JSON.stringify([{ valid: true, reason: 'ok' }]), stop_reason: 'end_turn' };
      }
      await new Promise((r) => setTimeout(r, 400));
      return {
        text: JSON.stringify([
          {
            type: 'short',
            question: '内部控制与审计部向谁负责？',
            answer: '董事会审计委员会',
            explanation: '原文依据。',
            evidence_ref: 1,
            quote: '对董事会审计委员会负责并向其报告工作',
          },
        ]),
        stop_reason: 'end_turn',
      };
    });

    const examPromise = apiJson('/exam/from-topic', {
      method: 'POST',
      body: JSON.stringify({ session_id: 's_a1', topic: '内部控制', count: 1 }),
    });
    const tStart = Date.now();
    const sessWhileBusy = await apiJson('/sessions');
    const elapsedMs = Date.now() - tStart;
    assert.equal(sessWhileBusy.status, 200);
    assert.ok(elapsedMs < 300, `/sessions took ${elapsedMs}ms while exam was running`);
    await examPromise;

    console.log('All A1-A11 checks passed');
  } finally {
    setLlmCallerForTest(null);
    await stopTestServer();
    fs.rmSync(tmpRoot, { recursive: true, force: true });
  }
}

runTests().catch((err) => {
  console.error('Acceptance suite failed:', err);
  process.exit(1);
});
