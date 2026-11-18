import 'dotenv/config';
import express, { NextFunction, Request, Response } from 'express';
import multer from 'multer';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { NeedsGeneralAnswer, runChatTurn } from './src/chat.js';
import { getDataRoot } from './src/config.js';
import {
  applyReviews,
  examResponse,
  generateQuestions,
  judgeExchange,
  RejectionReason,
  retrieveForMistakes,
  retrieveForTopic,
  reviewQuestions,
} from './src/exam.js';
import { checkLlmConnectivity, getLlmStatus } from './src/llm.js';
import { EvidenceChunk, kbManager } from './src/rag.js';
import {
  DEFAULT_SESSION_ID,
  mistakeBook,
  nowTimestamp,
  sessionKbId,
  sessionStore,
  SYNTHETIC_HINT,
  withSessionLock,
} from './src/store.js';

const PORT = Number(process.env.PORT || 3000);
const HOST = '0.0.0.0';
const PUBLIC_DIR = path.resolve(process.cwd(), 'public');
const UPLOAD_MAX_BYTES = 200 * 1024 * 1024;

function decodeFilename(raw: string): string {
  if (!raw) return 'document';
  try {
    const decoded = Buffer.from(raw, 'latin1').toString('utf8');
    if (decoded !== raw && !decoded.includes('\uFFFD')) {
      return decoded;
    }
  } catch {
  }
  return raw;
}

function autoRenameIfFirstTurn(sessionId: string, message: string): void {
  const sessions = sessionStore.listSessions();
  for (const s of sessions) {
    if (s.id === sessionId && s.user_turns <= 1 && !s.title_manual) {
      sessionStore.renameSession(sessionId, message.slice(0, 20), false);
      break;
    }
  }
}

export function createApp() {
  const app = express();
  app.use(express.json({ limit: '10mb' }));

  const upload = multer({
    storage: multer.memoryStorage(),
    limits: { fileSize: UPLOAD_MAX_BYTES },
  });

  app.get('/', (_req: Request, res: Response) => {
    res.sendFile(path.join(PUBLIC_DIR, 'index.html'));
  });

  app.get('/health', async (req: Request, res: Response) => {
    const llm = getLlmStatus();
    let llmReachable: boolean | 'not_checked' = llm.configured ? 'not_checked' : false;
    let llmError: string | undefined;

    if (req.query.check === '1') {
      const probe = await checkLlmConnectivity();
      llmReachable = probe.reachable;
      llmError = probe.reason;
    }

    res.json({
      status: 'ok',
      llm_configured: llm.configured,
      llm: llm.model,
      provider: llm.provider,
      llm_reachable: llmReachable,
      llm_error: llmError,
      base_url: llm.configured ? llm.baseUrl : null,
      key: llm.keyMasked,
      redis_persistent: false,
      session_storage: 'json-file',
      data_dir: getDataRoot(),
    });
  });

  app.post('/chat', async (req: Request, res: Response) => {
    const message = typeof req.body?.message === 'string' ? req.body.message : '';
    const sessionId =
      typeof req.body?.session_id === 'string' && req.body.session_id
        ? req.body.session_id
        : DEFAULT_SESSION_ID;
    const allowGeneral = Boolean(req.body?.allow_general);

    res.setHeader('Content-Type', 'text/event-stream; charset=utf-8');
    res.setHeader('Cache-Control', 'no-cache');
    res.setHeader('Connection', 'keep-alive');
    res.flushHeaders();

    let closed = false;
    req.on('close', () => {
      closed = true;
    });

    const sendEvent = (event: string, data: unknown) => {
      if (closed || res.writableEnded) return;
      res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
    };

    try {
      const result = await runChatTurn({
        sessionId,
        query: message,
        allowGeneral,
        onStatus: (stage, detail) => sendEvent('status', { stage, detail }),
      });
      sendEvent('answer', { text: result.answer, meta: result.meta });
      autoRenameIfFirstTurn(sessionId, message);
    } catch (err: any) {
      if (err instanceof NeedsGeneralAnswer) {
        sendEvent('needs_general', { message: err.message, question: message });
      } else {
        sendEvent('error', { message: String(err?.message || err).slice(0, 300) });
      }
    } finally {
      sendEvent('done', {});
      if (!res.writableEnded) res.end();
    }
  });

  app.post('/chat/sync', async (req: Request, res: Response) => {
    const message = typeof req.body?.message === 'string' ? req.body.message : '';
    const sessionId =
      typeof req.body?.session_id === 'string' && req.body.session_id
        ? req.body.session_id
        : DEFAULT_SESSION_ID;
    const allowGeneral = Boolean(req.body?.allow_general);
    const t0 = Date.now();

    try {
      const result = await runChatTurn({
        sessionId,
        query: message,
        allowGeneral,
      });
      autoRenameIfFirstTurn(sessionId, message);
      const cost = Number(((Date.now() - t0) / 1000).toFixed(1));
      res.json({
        session_id: sessionId,
        answer: result.answer,
        meta: result.meta,
        cost_seconds: cost,
        message_count: result.messagesCount,
      });
    } catch (err: any) {
      if (err instanceof NeedsGeneralAnswer) {
        res.json({ needs_general: true, message: err.message, question: message });
        return;
      }
      res.status(502).json({ detail: String(err?.message || err).slice(0, 300) });
    }
  });

  app.get('/sessions', (_req: Request, res: Response) => {
    res.json({ sessions: sessionStore.listSessions() });
  });

  app.post('/sessions', (req: Request, res: Response) => {
    const sessionId = typeof req.body?.session_id === 'string' ? req.body.session_id : '';
    if (!sessionId) {
      res.status(400).json({ detail: '缺少 session_id' });
      return;
    }
    if (sessionStore.hasSession(sessionId)) {
      res.status(409).json({ detail: '会话已存在' });
      return;
    }
    const title = typeof req.body?.title === 'string' ? req.body.title : null;
    const meta = sessionStore.createSession(sessionId, title);
    res.json(meta);
  });

  app.patch('/sessions/:sid', (req: Request, res: Response) => {
    const sid = String(req.params.sid);
    if (!sessionStore.hasSession(sid)) {
      res.status(404).json({ detail: '会话不存在' });
      return;
    }
    if (req.body?.title !== undefined && req.body?.title !== null) {
      const trimmed = String(req.body.title).trim();
      if (!trimmed || trimmed.length > 100) {
        res.status(400).json({ detail: '标题请输入 1～100 个字符' });
        return;
      }
      sessionStore.renameSession(sid, trimmed, true);
    }
    if (req.body?.archived !== undefined && req.body?.archived !== null) {
      sessionStore.archiveSession(sid, Boolean(req.body.archived));
    }
    res.json({ updated: sid });
  });

  app.delete('/sessions/:sid', async (req: Request, res: Response) => {
    const sid = String(req.params.sid);
    try {
      await withSessionLock(sid, async () => {
        if (!sessionStore.hasSession(sid)) {
          res.status(404).json({ detail: '会话不存在' });
          return;
        }
        try {
          kbManager.deleteScope(sessionKbId(sid));
        } catch (err: any) {
          res.status(409).json({ detail: String(err?.message || err) });
          return;
        }
        for (const card of mistakeBook.listAll()) {
          if (card.session_id === sid) {
            mistakeBook.delete(card.id);
          }
        }
        sessionStore.deleteSession(sid);
        res.json({ deleted: sid });
      });
    } catch (err: any) {
      res.status(500).json({ detail: String(err?.message || err) });
    }
  });

  app.get('/sessions/:sid/messages', (req: Request, res: Response) => {
    const sid = String(req.params.sid);
    const msgs = sessionStore.loadMessages(sid);
    if (!msgs.length && sid !== DEFAULT_SESSION_ID && !sessionStore.hasSession(sid)) {
      res.status(404).json({ detail: `会话不存在：${sid}` });
      return;
    }
    res.json({ session_id: sid, messages: msgs });
  });

  app.post('/documents', upload.single('file'), (req: Request, res: Response) => {
    const sessionId =
      typeof req.query.session_id === 'string' && req.query.session_id
        ? req.query.session_id
        : DEFAULT_SESSION_ID;
    sessionStore.touchIndex(sessionId);

    if (!req.file) {
      res.status(400).json({ detail: '请选择要上传的文件' });
      return;
    }

    try {
      const filename = decodeFilename(req.file.originalname);
      const rec = kbManager.addDocument(filename, req.file.buffer, sessionKbId(sessionId));
      res.json(rec);
    } catch (err: any) {
      res.status(400).json({ detail: String(err?.message || err) });
    }
  });

  app.get('/documents', (req: Request, res: Response) => {
    const sessionId =
      typeof req.query.session_id === 'string' && req.query.session_id
        ? req.query.session_id
        : DEFAULT_SESSION_ID;
    res.json({ docs: kbManager.listDocuments(sessionKbId(sessionId)) });
  });

  app.post('/documents/:doc_id/rebuild', async (req: Request, res: Response) => {
    const docId = String(req.params.doc_id);
    const sessionId =
      typeof req.query.session_id === 'string' && req.query.session_id
        ? req.query.session_id
        : DEFAULT_SESSION_ID;
    await withSessionLock(sessionId, async () => {
      try {
        const rec = kbManager.rebuildDocument(docId, sessionKbId(sessionId));
        res.json(rec);
      } catch (err: any) {
        res.status(409).json({ detail: String(err?.message || err) });
      }
    });
  });

  app.delete('/documents/:doc_id', async (req: Request, res: Response) => {
    const docId = String(req.params.doc_id);
    const sessionId =
      typeof req.query.session_id === 'string' && req.query.session_id
        ? req.query.session_id
        : DEFAULT_SESSION_ID;
    await withSessionLock(sessionId, async () => {
      try {
        const ok = kbManager.deleteDocument(docId, sessionKbId(sessionId));
        if (!ok) {
          res.status(404).json({ detail: `文档不存在：${docId}` });
          return;
        }
        res.json({ deleted: docId });
      } catch (err: any) {
        res.status(409).json({ detail: String(err?.message || err) });
      }
    });
  });

  app.get('/mistakes', (req: Request, res: Response) => {
    const status = typeof req.query.status === 'string' && req.query.status ? req.query.status : null;
    res.json({
      items: mistakeBook.listAll(status),
      stats: mistakeBook.count(),
    });
  });

  app.post('/mistakes', async (req: Request, res: Response) => {
    const sessionId = typeof req.body?.session_id === 'string' ? req.body.session_id : '';
    if (!sessionId) {
      res.status(400).json({ detail: '缺少 session_id' });
      return;
    }

    const msgs = sessionStore.loadMessages(sessionId);
    const pairs: Array<[string, string]> = [];
    let question: string | null = null;
    for (const m of msgs) {
      if (m.meta?.mode === 'no_evidence') continue;
      if (typeof m.content !== 'string') continue;
      if (m.role === 'user' && !m.content.startsWith(SYNTHETIC_HINT)) {
        question = m.content;
      } else if (m.role === 'assistant' && question !== null) {
        pairs.push([question, m.content]);
        question = null;
      }
    }

    let selected: [string, string];
    if (req.body?.question !== undefined || req.body?.answer !== undefined) {
      const reqQ = String(req.body.question ?? '');
      const reqA = String(req.body.answer ?? '');
      const found = pairs.find(([q, a]) => q === reqQ && a === reqA);
      if (!found) {
        res.status(409).json({ detail: '这条问答已不在会话记录中，请刷新后重试' });
        return;
      }
      selected = found;
    } else if (pairs.length) {
      selected = pairs[pairs.length - 1];
    } else {
      res.status(400).json({ detail: '该会话没有可收录的完整问答' });
      return;
    }

    const [lastUser, lastAnswer] = selected;
    const existing = mistakeBook
      .listAll()
      .find(
        (item) =>
          item.session_id === sessionId &&
          item.question === lastUser &&
          item.answer === lastAnswer.slice(0, 3000)
      );
    if (existing) {
      res.json(existing);
      return;
    }

    const card = await judgeExchange(lastUser, lastAnswer, true);
    if (!card) {
      res.status(502).json({ detail: '收录失败：大模型未配置或调用异常，请检查设置后重试' });
      return;
    }

    const mid = mistakeBook.add({
      question: lastUser,
      answer: lastAnswer,
      confusion: card.confusion,
      topic: card.topic,
      session_id: sessionId,
      session_title: sessionStore.getSessionTitle(sessionId),
    });
    res.json(mistakeBook.get(mid));
  });

  app.put('/mistakes/:mid/status', (req: Request, res: Response) => {
    const mid = Number(req.params.mid);
    const status = typeof req.body?.status === 'string' ? req.body.status : '';
    try {
      const ok = mistakeBook.setStatus(mid, status);
      if (!ok) {
        res.status(404).json({ detail: `错题不存在：#${mid}` });
        return;
      }
      res.json({ id: mid, status });
    } catch (err: any) {
      res.status(400).json({ detail: String(err?.message || err) });
    }
  });

  app.delete('/mistakes/:mid', (req: Request, res: Response) => {
    const mid = Number(req.params.mid);
    if (!mistakeBook.delete(mid)) {
      res.status(404).json({ detail: `错题不存在：#${mid}` });
      return;
    }
    res.json({ deleted: mid });
  });

  app.get('/mistakes/export', (_req: Request, res: Response) => {
    const md = mistakeBook.exportMarkdown();
    const datePart = nowTimestamp().slice(0, 10);
    const fname = `错题本_${datePart}.md`;
    res.setHeader('Content-Type', 'text/markdown; charset=utf-8');
    res.setHeader(
      'Content-Disposition',
      `attachment; filename*=UTF-8''${encodeURIComponent(fname)}`
    );
    res.send(md);
  });

  async function generateReviewed(chunks: EvidenceChunk[], count: number, context: string) {
    const generated = await generateQuestions(chunks, count, context);
    const reviews = await reviewQuestions(generated.items);
    return applyReviews(generated, reviews);
  }

  app.post('/exam/from-mistakes', async (req: Request, res: Response) => {
    const rawIds = Array.isArray(req.body?.mistake_ids) ? req.body.mistake_ids.map(Number) : [];
    const sessionId =
      typeof req.body?.session_id === 'string' && req.body.session_id
        ? req.body.session_id
        : DEFAULT_SESSION_ID;
    const countPer = Math.min(5, Math.max(1, Number(req.body?.count_per || 2)));

    if (!rawIds.length || rawIds.length > 10) {
      res.status(400).json({ detail: '请选择 1～10 道错题' });
      return;
    }

    await withSessionLock(sessionId, async () => {
      const ids = Array.from(new Set(rawIds)) as number[];
      const allowed = new Set(
        mistakeBook
          .listAll()
          .filter((m) => m.session_id === sessionId)
          .map((m) => m.id)
      );
      if (!ids.every((id) => allowed.has(id))) {
        res.status(400).json({ detail: '请选择当前对话的错题' });
        return;
      }

      const retrieved = retrieveForMistakes(ids, 3, sessionKbId(sessionId));
      const allQuestions = [];
      const rejected: RejectionReason[] = [];

      for (const item of retrieved) {
        if (!item.chunks.length) {
          rejected.push({ mistake_id: item.mistake_id, reason: '未找到相关资料，未生成题目' });
          continue;
        }
        try {
          const { kept, rejected: itemRej } = await generateReviewed(
            item.chunks,
            countPer,
            `${item.topic} - ${item.confusion}`
          );
          allQuestions.push(...kept);
          for (const r of itemRej) {
            rejected.push({ ...r, mistake_id: item.mistake_id });
          }
        } catch (err: any) {
          res.status(502).json({ detail: String(err?.message || err) });
          return;
        }
      }

      if (!allQuestions.length) {
        res.status(422).json({
          detail: {
            message: '没有可用题目：资料不足或题目校验未通过',
            rejection_reasons: rejected,
          },
        });
        return;
      }

      res.json(examResponse(allQuestions, '错题本薄弱点检测卷', ids.length * countPer, rejected));
    });
  });

  app.post('/exam/from-topic', async (req: Request, res: Response) => {
    const topic = typeof req.body?.topic === 'string' ? req.body.topic.trim() : '';
    const sessionId =
      typeof req.body?.session_id === 'string' && req.body.session_id
        ? req.body.session_id
        : DEFAULT_SESSION_ID;
    const count = Math.min(10, Math.max(1, Number(req.body?.count || 5)));

    if (!topic) {
      res.status(400).json({ detail: '请输入主题关键词' });
      return;
    }

    await withSessionLock(sessionId, async () => {
      const chunks = retrieveForTopic(topic, sessionKbId(sessionId), 10);
      if (!chunks.length) {
        res.status(404).json({ detail: `主题「${topic}」未检索到足够相关的资料` });
        return;
      }

      try {
        const { kept, rejected } = await generateReviewed(chunks, count, `主题：${topic}`);
        if (!kept.length) {
          res.status(422).json({
            detail: {
              message: '题目未通过结构或内容校验，请调整主题重试',
              rejection_reasons: rejected,
            },
          });
          return;
        }
        res.json(examResponse(kept, `「${topic}」主题检测卷`, count, rejected));
      } catch (err: any) {
        res.status(502).json({ detail: String(err?.message || err) });
      }
    });
  });

  app.use((err: any, _req: Request, res: Response, _next: NextFunction) => {
    if (err?.code === 'LIMIT_FILE_SIZE') {
      res.status(413).json({ detail: '文件超过 200MB 上限' });
      return;
    }
    res.status(500).json({ detail: String(err?.message || '服务内部错误') });
  });

  return app;
}

const isMain =
  process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);

if (isMain) {
  const app = createApp();
  app.listen(PORT, HOST, () => {
    console.log(`学记个人学习备考助手服务已启动: http://${HOST}:${PORT}`);
  });
}
