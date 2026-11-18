import { createHash } from 'node:crypto';
import path from 'node:path';
import { atomicWriteJsonSync, ensureDirSync, getDataRoot, readJsonSync } from './config.js';

export const DEFAULT_SESSION_ID = 'default';
export const SYNTHETIC_HINT = '请结合这个新线索继续检索：';
export const STATUS_UNLEARNED = '未掌握';
export const STATUS_LEARNED = '已掌握';

export interface ChatMessageMeta {
  mode?: 'grounded' | 'general' | 'unverified' | 'no_evidence';
  question?: string;
  evidence?: Array<{ ref: number; doc_id: string; chunk_id: string }>;
  cited_refs?: number[];
  coverage?: string;
  checked_refs?: number[];
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
  meta?: ChatMessageMeta;
}

export interface SessionMeta {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
  user_turns: number;
  title_manual?: boolean;
  archived?: boolean;
}

export interface MistakeCard {
  id: number;
  topic: string;
  confusion: string;
  question: string;
  answer: string;
  session_id: string;
  session_title: string;
  status: string;
  created_at: string;
  updated_at: string;
}

export function nowTimestamp(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export function countUserTurns(messages: ChatMessage[]): number {
  return messages.filter(
    (m) =>
      m.role === 'user' &&
      typeof m.content === 'string' &&
      !m.content.startsWith(SYNTHETIC_HINT)
  ).length;
}

export function sessionKbId(sessionId: string): string {
  return 'session_' + createHash('sha256').update(sessionId, 'utf8').digest('hex').slice(0, 24);
}

interface PersistedSessions {
  sessions: SessionMeta[];
  messages: Record<string, ChatMessage[]>;
}

export class SessionStore {
  private filePath: string;
  private sessionsOrder: SessionMeta[] = [];
  private messagesMap = new Map<string, ChatMessage[]>();

  constructor(dataRoot?: string) {
    const root = dataRoot || getDataRoot();
    ensureDirSync(root);
    this.filePath = path.join(root, 'sessions.json');
    this.loadFromDisk();
  }

  private loadFromDisk(): void {
    const data = readJsonSync<PersistedSessions>(this.filePath, { sessions: [], messages: {} });
    this.sessionsOrder = Array.isArray(data.sessions) ? data.sessions : [];
    this.messagesMap = new Map(Object.entries(data.messages || {}));
  }

  private saveToDisk(): void {
    const messagesObj: Record<string, ChatMessage[]> = {};
    for (const [k, v] of this.messagesMap.entries()) {
      messagesObj[k] = v;
    }
    atomicWriteJsonSync(this.filePath, {
      sessions: this.sessionsOrder,
      messages: messagesObj,
    });
  }

  touchIndex(sessionId: string, title?: string, stats?: Partial<SessionMeta>): void {
    const existing = this.sessionsOrder.find((s) => s.id === sessionId);
    const now = nowTimestamp();
    if (existing) {
      if (title) existing.title = title;
      if (stats) Object.assign(existing, stats);
      existing.updated_at = now;
      this.saveToDisk();
      return;
    }
    const newItem: SessionMeta = {
      id: sessionId,
      title: title || `会话 ${sessionId}`,
      created_at: now,
      updated_at: now,
      message_count: 0,
      user_turns: 0,
      ...stats,
    };
    this.sessionsOrder.unshift(newItem);
    this.saveToDisk();
  }

  createSession(sessionId: string, title?: string | null): SessionMeta {
    const now = nowTimestamp();
    const meta: SessionMeta = {
      id: sessionId,
      title: title || `会话 ${sessionId}`,
      created_at: now,
      updated_at: now,
      message_count: 0,
      user_turns: 0,
    };
    this.messagesMap.set(sessionId, []);
    this.sessionsOrder = this.sessionsOrder.filter((s) => s.id !== sessionId);
    this.sessionsOrder.unshift(meta);
    this.saveToDisk();
    return { ...meta };
  }

  listSessions(): SessionMeta[] {
    return this.sessionsOrder.map((s) => ({ ...s }));
  }

  hasSession(sessionId: string): boolean {
    return this.sessionsOrder.some((s) => s.id === sessionId);
  }

  getSessionTitle(sessionId: string): string {
    const found = this.sessionsOrder.find((s) => s.id === sessionId);
    return found?.title || sessionId;
  }

  deleteSession(sessionId: string): boolean {
    const existed = this.hasSession(sessionId);
    this.messagesMap.delete(sessionId);
    this.sessionsOrder = this.sessionsOrder.filter((s) => s.id !== sessionId);
    this.saveToDisk();
    return existed;
  }

  renameSession(sessionId: string, title: string, manual = true): boolean {
    const found = this.sessionsOrder.find((s) => s.id === sessionId);
    if (!found) return false;
    found.title = title;
    if (manual) found.title_manual = true;
    this.saveToDisk();
    return true;
  }

  archiveSession(sessionId: string, archived: boolean): boolean {
    const found = this.sessionsOrder.find((s) => s.id === sessionId);
    if (!found) return false;
    found.archived = archived;
    this.saveToDisk();
    return true;
  }

  loadMessages(sessionId: string = DEFAULT_SESSION_ID): ChatMessage[] {
    const msgs = this.messagesMap.get(sessionId);
    return msgs ? msgs.map((m) => ({ ...m, meta: m.meta ? { ...m.meta } : undefined })) : [];
  }

  saveMessages(sessionId: string, messages: ChatMessage[], title?: string): void {
    const cloned = messages.map((m) => ({ ...m, meta: m.meta ? { ...m.meta } : undefined }));
    this.messagesMap.set(sessionId, cloned);
    this.touchIndex(sessionId, title, {
      message_count: cloned.length,
      user_turns: countUserTurns(cloned),
    });
  }
}

interface PersistedMistakes {
  nextId: number;
  cards: MistakeCard[];
}

export class MistakeBook {
  private filePath: string;
  private nextId = 1;
  private cards: MistakeCard[] = [];

  constructor(dataRoot?: string) {
    const root = dataRoot || getDataRoot();
    ensureDirSync(root);
    this.filePath = path.join(root, 'mistakes.json');
    this.loadFromDisk();
  }

  private loadFromDisk(): void {
    const data = readJsonSync<PersistedMistakes>(this.filePath, { nextId: 1, cards: [] });
    this.nextId = typeof data.nextId === 'number' && data.nextId >= 1 ? data.nextId : 1;
    this.cards = Array.isArray(data.cards) ? data.cards : [];
  }

  private saveToDisk(): void {
    atomicWriteJsonSync(this.filePath, {
      nextId: this.nextId,
      cards: this.cards,
    });
  }

  add(params: {
    question: string;
    answer: string;
    confusion: string;
    topic?: string;
    session_id?: string;
    session_title?: string;
  }): number {
    const now = nowTimestamp();
    const id = this.nextId++;
    const card: MistakeCard = {
      id,
      topic: params.topic || '',
      confusion: params.confusion,
      question: params.question,
      answer: params.answer.slice(0, 3000),
      session_id: params.session_id || '',
      session_title: params.session_title || '',
      status: STATUS_UNLEARNED,
      created_at: now,
      updated_at: now,
    };
    this.cards.unshift(card);
    this.saveToDisk();
    return id;
  }

  get(mistakeId: number): MistakeCard | null {
    const card = this.cards.find((c) => c.id === mistakeId);
    return card ? { ...card } : null;
  }

  setStatus(mistakeId: number, status: string): boolean {
    if (status !== STATUS_UNLEARNED && status !== STATUS_LEARNED) {
      throw new Error(`非法状态：${status}（只能为 未掌握/已掌握）`);
    }
    const card = this.cards.find((c) => c.id === mistakeId);
    if (!card) return false;
    card.status = status;
    card.updated_at = nowTimestamp();
    this.saveToDisk();
    return true;
  }

  delete(mistakeId: number): boolean {
    const before = this.cards.length;
    this.cards = this.cards.filter((c) => c.id !== mistakeId);
    if (this.cards.length < before) {
      this.saveToDisk();
      return true;
    }
    return false;
  }

  listAll(status?: string | null): MistakeCard[] {
    const filtered = status ? this.cards.filter((c) => c.status === status) : this.cards;
    return filtered.map((c) => ({ ...c }));
  }

  count(): { total: number; learned: number; unlearned: number } {
    const total = this.cards.length;
    const learned = this.cards.filter((c) => c.status === STATUS_LEARNED).length;
    return { total, learned, unlearned: total - learned };
  }

  exportMarkdown(): string {
    const cards = this.listAll();
    const stats = this.count();
    const now = nowTimestamp().slice(0, 16);
    const lines = [
      '# 我的错题本',
      '',
      `> 导出时间：${now} · 共 ${stats.total} 题（未掌握 ${stats.unlearned} / 已掌握 ${stats.learned}）`,
      '',
    ];
    if (!cards.length) {
      lines.push('（错题本还是空的，去聊天里暴露一些困惑吧）');
      return lines.join('\n');
    }

    const groups = new Map<string, MistakeCard[]>();
    for (const c of cards) {
      const key = c.topic || '未分类';
      const arr = groups.get(key) || [];
      arr.push(c);
      groups.set(key, arr);
    }

    const topics = Array.from(groups.keys()).sort((a, b) => {
      const learnedA = (groups.get(a) || []).filter((c) => c.status === STATUS_LEARNED).length;
      const learnedB = (groups.get(b) || []).filter((c) => c.status === STATUS_LEARNED).length;
      return learnedA - learnedB;
    });

    let n = 0;
    for (const topic of topics) {
      lines.push(`## ${topic}`, '');
      for (const c of groups.get(topic) || []) {
        n += 1;
        const mark = c.status === STATUS_LEARNED ? '✅ 已掌握' : '❌ 未掌握';
        let ans = c.answer;
        if (ans.length > 600) ans = ans.slice(0, 600) + '……';
        const src = c.session_title || c.session_id || '未知会话';
        lines.push(
          `### ${n}. ${c.confusion} 【${mark}】`,
          '',
          `- **原问题**：${c.question}`,
          `- **混淆点**：${c.confusion}`,
          `- **当时解答**：${ans.replace(/\n/g, ' ')}`,
          `- 来源：${src} · ${c.created_at.slice(0, 10)}`,
          ''
        );
      }
    }
    return lines.join('\n');
  }
}

export let sessionStore = new SessionStore();
export let mistakeBook = new MistakeBook();

export function reloadStores(dataRoot?: string): void {
  sessionStore = new SessionStore(dataRoot);
  mistakeBook = new MistakeBook(dataRoot);
}

export const sessionLocks = new Map<string, Promise<void>>();

export async function withSessionLock<T>(sessionId: string, fn: () => Promise<T>): Promise<T> {
  const prev = sessionLocks.get(sessionId) || Promise.resolve();
  let release!: () => void;
  const current = new Promise<void>((resolve) => {
    release = resolve;
  });
  const chained = prev.then(() => current);
  sessionLocks.set(sessionId, chained);
  await prev;
  try {
    return await fn();
  } finally {
    release();
    if (sessionLocks.get(sessionId) === chained) {
      sessionLocks.delete(sessionId);
    }
  }
}
