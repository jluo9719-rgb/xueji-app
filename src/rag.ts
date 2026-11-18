import { createHash, randomUUID } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import iconv from 'iconv-lite';
import JSZip from 'jszip';
import { extractText as extractPdfText } from 'unpdf';
import {
  atomicWriteJsonSync,
  ensureDirSync,
  getDataRoot,
  getEvidenceMinScore,
  readJsonSync,
} from './config.js';
import { sessionKbId } from './store.js';

const ALLOWED_EXTS = new Set(['.pdf', '.txt', '.md', '.docx']);
const CHUNK_MAX_CHARS = 800;

export interface DocumentRecord {
  doc_id: string;
  kb_id: string;
  name: string;
  ext: string;
  status: 'processing' | 'ready' | 'error';
  stage: string;
  progress: number;
  chunk_count: number;
  size: number;
  uploaded_at: string;
  error: string | null;
  index_version?: string;
  revision?: string;
  duplicate?: boolean;
}

export interface ChunkRecord {
  id: string;
  text: string;
  kb_id: string;
  doc_id: string;
  doc_name: string;
  chunk_index: number;
  revision: string;
  chunk_id: string;
}

export interface EvidenceChunk {
  ref: number;
  text: string;
  name: string;
  chunk: number;
  score: number;
  doc_id: string;
  chunk_id: string;
  channels: Array<'vector' | 'bm25'>;
  metadata: Record<string, unknown>;
}

export interface RetrievalResult {
  query: string;
  scope: string;
  evidence: EvidenceChunk[];
  top_score: number;
  threshold: number;
  candidate_count: number;
}

const TYPOGRAPHY_MAP: Record<string, string> = {
  '，': ',',
  '。': '.',
  '；': ';',
  '：': ':',
  '！': '!',
  '？': '?',
  '（': '(',
  '）': ')',
  '“': '',
  '”': '',
  '‘': '',
  '’': '',
  '"': '',
  "'": '',
};

export function normalizeQuote(text: string): string {
  let out = '';
  for (const ch of text) {
    out += TYPOGRAPHY_MAP[ch] ?? ch;
  }
  return out.replace(/\s+/g, '');
}

export function quoteInSource(quote: string, source: string): boolean {
  const normalized = normalizeQuote(quote || '');
  return Boolean(normalized) && normalizeQuote(source || '').includes(normalized);
}

function decodeTextBuffer(buf: Buffer): string {
  let utf8 = buf.toString('utf8');
  if (utf8.charCodeAt(0) === 0xfeff) {
    utf8 = utf8.slice(1);
  }
  if (utf8.includes('\uFFFD')) {
    const gb = iconv.decode(buf, 'gb18030');
    if (!gb.includes('\uFFFD')) {
      return gb;
    }
    throw new Error('无法识别文本编码，请另存为 UTF-8 后重新上传');
  }
  return utf8;
}

function decodeXmlEntities(str: string): string {
  return str
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&#(\d+);/g, (_, num) => String.fromCharCode(Number(num)))
    .replace(/&#x([0-9a-fA-F]+);/g, (_, hex) => String.fromCharCode(parseInt(hex, 16)));
}

function extractXmlText(xml: string): string {
  const parts: string[] = [];
  const tokenRegex = /<w:t(?:\s[^>]*)?>([\s\S]*?)<\/w:t>|<w:tab\s*\/>|<w:br\s*\/>/g;
  let match: RegExpExecArray | null;
  while ((match = tokenRegex.exec(xml)) !== null) {
    if (match[1] !== undefined) {
      parts.push(decodeXmlEntities(match[1]));
    } else if (match[0].startsWith('<w:tab')) {
      parts.push('\t');
    } else {
      parts.push('\n');
    }
  }
  return parts.join('');
}

async function extractDocxText(buffer: Buffer): Promise<string> {
  const zip = await JSZip.loadAsync(buffer);
  const docFile = zip.file('word/document.xml');
  if (!docFile) {
    throw new Error('无效的 DOCX 文件（缺少 word/document.xml）');
  }
  const xml = await docFile.async('string');
  const parts: string[] = [];

  const blockRegex = /<w:tbl[\s>][\s\S]*?<\/w:tbl>|<w:p[\s>][\s\S]*?<\/w:p>/g;
  let blockMatch: RegExpExecArray | null;
  while ((blockMatch = blockRegex.exec(xml)) !== null) {
    const block = blockMatch[0];
    if (block.startsWith('<w:tbl')) {
      const rowRegex = /<w:tr[\s>][\s\S]*?<\/w:tr>/g;
      let rowMatch: RegExpExecArray | null;
      while ((rowMatch = rowRegex.exec(block)) !== null) {
        const cellRegex = /<w:tc[\s>][\s\S]*?<\/w:tc>/g;
        const cells: string[] = [];
        let cellMatch: RegExpExecArray | null;
        while ((cellMatch = cellRegex.exec(rowMatch[0])) !== null) {
          cells.push(extractXmlText(cellMatch[0]).replace(/\s+/g, ' ').trim());
        }
        if (cells.some(Boolean)) {
          parts.push(`| ${cells.join(' | ')} |`);
        }
      }
    } else {
      const text = extractXmlText(block).trim();
      if (text) parts.push(text);
    }
  }

  return parts.join('\n');
}

export async function loadDocumentContent(ext: string, content: Buffer): Promise<string> {
  if (ext === '.pdf') {
    const result = await extractPdfText(new Uint8Array(content), { mergePages: true });
    const rawText = Array.isArray(result.text) ? result.text.join('\n') : String(result.text || '');
    const lines = rawText.split(/\r?\n/).filter((l) => l.trim().length > 0);
    if (!lines.length) {
      throw new Error('PDF 未提取到文字，可能是扫描件；请先 OCR 识别后上传');
    }
    return lines.join('\n');
  }

  if (ext === '.docx') {
    const text = await extractDocxText(content);
    if (!text.trim()) {
      throw new Error('Word 文档没有可读取的文字');
    }
    return text;
  }

  const decoded = decodeTextBuffer(content);
  const lines = decoded.split(/\r?\n/).filter((l) => l.trim().length > 0);
  if (!lines.length) {
    throw new Error('文件没有可读取的文字');
  }
  return lines.join('\n');
}

export function chunkDocument(text: string, maxChars: number = CHUNK_MAX_CHARS): string[] {
  const maximum = Math.max(200, maxChars);
  const sections: Array<[string, string]> = [];
  let pathStack: string[] = [];
  const lines: string[] = [];

  const headingRe = /^(#{1,6})\s+(.+)$/;
  const numberRe = /^(?:-\s*)?(\d+(?:\.\d+)+)\s*\S+/;

  const flush = () => {
    if (lines.length > 0) {
      sections.push([pathStack.join(' > '), lines.join('\n').trim()]);
      lines.length = 0;
    }
  };

  for (const line of text.split(/\r?\n/)) {
    const stripped = line.trim();
    const heading = headingRe.exec(stripped);
    const numbered = !heading ? numberRe.exec(stripped) : null;
    if (heading || numbered) {
      flush();
      const level = heading ? heading[1].length : numbered![1].split('.').length + 1;
      const title = heading ? heading[2] : stripped;
      pathStack = [...pathStack.slice(0, Math.max(0, level - 1)), title];
    } else if (stripped) {
      lines.push(line);
    }
  }
  flush();

  const result: string[] = [];
  for (const [title, body] of sections) {
    const prefix = title ? `章节：${title.slice(0, 160)}\n` : '';
    const budget = Math.max(80, maximum - prefix.length);
    const parts: string[] = [];
    let current = '';

    for (let rawLine of body.split(/\r?\n/)) {
      const segments: string[] = [];
      while (rawLine.length > budget) {
        const head = rawLine.slice(0, budget);
        let cut = budget;
        const endRegex = /[。！？；.!?;]/g;
        let m: RegExpExecArray | null;
        while ((m = endRegex.exec(head)) !== null) {
          cut = m.index + 1;
        }
        segments.push(rawLine.slice(0, cut));
        rawLine = rawLine.slice(cut);
      }
      if (rawLine) segments.push(rawLine);

      for (const segment of segments) {
        if (current && current.length + segment.length + 1 > budget) {
          parts.push(current);
          current = '';
        }
        current += (current ? '\n' : '') + segment;
      }
    }
    if (current) parts.push(current);

    if (parts.length > 1 && parts[parts.length - 1].length < 50) {
      const combined = `${parts[parts.length - 2]}\n${parts[parts.length - 1]}`;
      if (combined.length <= budget) {
        parts.splice(parts.length - 2, 2, combined);
      } else {
        const endRegex = /[。！？；.!?;]/g;
        const candidates: number[] = [];
        let m: RegExpExecArray | null;
        while ((m = endRegex.exec(combined)) !== null) {
          const endPos = m.index + 1;
          if (combined.length - budget <= endPos && endPos <= budget) {
            candidates.push(endPos);
          }
        }
        const mid = Math.floor(combined.length / 2);
        const cut = candidates.length
          ? candidates.reduce((best, cur) => (Math.abs(cur - mid) < Math.abs(best - mid) ? cur : best))
          : mid;
        parts.splice(parts.length - 2, 2, combined.slice(0, cut), combined.slice(cut));
      }
    }

    for (const part of parts) {
      const block = prefix + part;
      if (block.length < 50 && result.length > 0 && result[result.length - 1].length + block.length + 2 <= maximum) {
        result[result.length - 1] += `\n\n${block}`;
      } else {
        result.push(block);
      }
    }
  }

  return result;
}

const CJK_STOPWORDS = new Set([
  '的', '了', '是', '在', '有', '和', '与', '或', '吗', '呢', '啊', '吧',
  '着', '过', '被', '把', '给', '让', '向', '从', '到', '为', '对', '以',
  '及', '等', '之', '其', '这', '那', '哪', '谁', '什', '么', '个', '中',
]);

function tokenizeLexical(text: string): string[] {
  const lower = (text || '').toLowerCase();
  const tokens: string[] = [];

  const wordMatches = lower.match(/[a-z0-9]+(?:[._-][a-z0-9]+)*/g);
  if (wordMatches) {
    tokens.push(...wordMatches);
  }

  const cjkRuns = lower.match(/[\u4e00-\u9fff]+/g);
  if (cjkRuns) {
    for (const run of cjkRuns) {
      if (run.length === 1) {
        if (!CJK_STOPWORDS.has(run)) {
          tokens.push(run);
        }
        continue;
      }
      for (let i = 0; i < run.length; i++) {
        if (i + 1 < run.length) {
          tokens.push(run.slice(i, i + 2));
        }
        if (i + 2 < run.length) {
          tokens.push(run.slice(i, i + 3));
        }
      }
    }
  }
  return tokens;
}

function charNgramVector(text: string): Map<string, number> {
  const clean = (text || '').toLowerCase().replace(/\s+/g, '');
  const vec = new Map<string, number>();
  for (let i = 0; i < clean.length; i++) {
    const uni = clean[i];
    if (!CJK_STOPWORDS.has(uni)) {
      vec.set(uni, (vec.get(uni) || 0) + 0.15);
    }
    if (i + 1 < clean.length) {
      const bi = clean.slice(i, i + 2);
      vec.set(bi, (vec.get(bi) || 0) + 1.0);
    }
  }
  return vec;
}

function cosineSim(a: Map<string, number>, b: Map<string, number>): number {
  let dot = 0;
  let normA = 0;
  let normB = 0;
  for (const [k, va] of a.entries()) {
    normA += va * va;
    const vb = b.get(k);
    if (vb) dot += va * vb;
  }
  for (const vb of b.values()) {
    normB += vb * vb;
  }
  if (!normA || !normB) return 0;
  return dot / (Math.sqrt(normA) * Math.sqrt(normB));
}

interface PersistedRegistry {
  kb_rev: number;
  docs: DocumentRecord[];
}

export class KBManager {
  private dataRoot: string;
  private uploadsDir: string;
  private chunksDir: string;
  private registryPath: string;
  private docs: DocumentRecord[] = [];
  private chunksByDoc = new Map<string, ChunkRecord[]>();
  private kbRev = 0;
  private indexingPromises = new Map<string, Promise<void>>();

  constructor(dataRoot?: string) {
    this.dataRoot = dataRoot || getDataRoot();
    this.uploadsDir = path.join(this.dataRoot, 'uploads');
    this.chunksDir = path.join(this.dataRoot, 'chunks');
    this.registryPath = path.join(this.dataRoot, 'kb_registry.json');
    ensureDirSync(this.uploadsDir);
    ensureDirSync(this.chunksDir);
    this.loadFromDisk();
    this.recoverStale();
  }

  private loadFromDisk(): void {
    const reg = readJsonSync<PersistedRegistry>(this.registryPath, { kb_rev: 0, docs: [] });
    this.kbRev = typeof reg.kb_rev === 'number' ? reg.kb_rev : 0;
    this.docs = Array.isArray(reg.docs) ? reg.docs : [];
    for (const d of this.docs) {
      if (d.status === 'ready') {
        const chunkFile = path.join(this.chunksDir, `${d.doc_id}.json`);
        const chunks = readJsonSync<ChunkRecord[]>(chunkFile, []);
        if (chunks.length) {
          this.chunksByDoc.set(d.doc_id, chunks);
        }
      }
    }
  }

  private saveRegistry(): void {
    atomicWriteJsonSync(this.registryPath, {
      kb_rev: this.kbRev,
      docs: this.docs,
    });
  }

  private recoverStale(): void {
    let changed = false;
    for (const d of this.docs) {
      if (d.status === 'processing') {
        const chunkFile = path.join(this.chunksDir, `${d.doc_id}.json`);
        if (fs.existsSync(chunkFile)) {
          try {
            fs.unlinkSync(chunkFile);
          } catch {
          }
        }
        d.status = 'error';
        d.stage = '处理失败';
        d.error = '上次索引被中断，请点击重建索引重试';
        changed = true;
      }
    }
    if (changed) {
      this.saveRegistry();
    }
  }

  addDocument(filename: string, content: Buffer, kbId: string): DocumentRecord {
    if (!content || content.length === 0) {
      throw new Error('文件为空，请上传包含文字的资料');
    }
    const ext = path.extname(filename || '').toLowerCase();
    if (!ALLOWED_EXTS.has(ext)) {
      throw new Error(`不支持的格式 ${ext || '(无扩展名)'}，支持：pdf / txt / md / docx`);
    }

    const docId = createHash('sha256')
      .update(kbId, 'utf8')
      .update(Buffer.from([0]))
      .update(content)
      .digest('hex')
      .slice(0, 24);

    const existing = this.docs.find((d) => d.doc_id === docId);
    if (existing && (existing.status === 'processing' || existing.status === 'ready')) {
      return { ...existing, duplicate: true };
    }

    const uploadPath = path.join(this.uploadsDir, `${docId}${ext}`);
    fs.writeFileSync(uploadPath, content);

    const rec: DocumentRecord = {
      doc_id: docId,
      kb_id: kbId,
      name: filename || `document${ext}`,
      ext,
      status: 'processing',
      stage: '排队中',
      progress: 0,
      chunk_count: 0,
      size: content.length,
      uploaded_at: new Date().toISOString().slice(0, 19),
      error: null,
    };

    this.docs = [rec, ...this.docs.filter((d) => d.doc_id !== docId)];
    this.saveRegistry();
    this.enqueueIndexing(docId);
    return { ...rec };
  }

  private enqueueIndexing(docId: string): void {
    const promise = new Promise<void>((resolve) => {
      setImmediate(async () => {
        try {
          await this.indexDocument(docId);
        } catch (err: any) {
          const doc = this.docs.find((d) => d.doc_id === docId);
          if (doc) {
            doc.status = 'error';
            doc.stage = '处理失败';
            doc.error = String(err?.message || err).slice(0, 200);
            this.saveRegistry();
          }
        } finally {
          this.indexingPromises.delete(docId);
          resolve();
        }
      });
    });
    this.indexingPromises.set(docId, promise);
  }

  async waitForIndexing(docId: string): Promise<void> {
    const p = this.indexingPromises.get(docId);
    if (p) await p;
  }

  private async indexDocument(docId: string): Promise<void> {
    if (indexingDelayMsForTest > 0) {
      await new Promise((resolve) => setTimeout(resolve, indexingDelayMsForTest));
    }
    const doc = this.docs.find((d) => d.doc_id === docId);
    if (!doc) return;
    const uploadPath = path.join(this.uploadsDir, `${docId}${doc.ext}`);
    if (!fs.existsSync(uploadPath)) {
      throw new Error('上传原件丢失，请重新上传');
    }
    const buf = fs.readFileSync(uploadPath);

    doc.stage = '解析文档中';
    doc.progress = 15;
    this.saveRegistry();
    const text = await loadDocumentContent(doc.ext, buf);

    doc.stage = '切分中';
    doc.progress = 50;
    this.saveRegistry();
    const chunks = chunkDocument(text);
    if (!chunks.length) {
      throw new Error('文档切分后内容为空');
    }

    const revision = randomUUID().replace(/-/g, '').slice(0, 12);
    doc.stage = '构建检索索引';
    doc.progress = 80;
    doc.chunk_count = chunks.length;

    const records: ChunkRecord[] = chunks.map((chunkText, idx) => {
      const cid = `${docId}_${revision}_${idx}`;
      return {
        id: cid,
        text: chunkText,
        kb_id: doc.kb_id,
        doc_id: docId,
        doc_name: doc.name,
        chunk_index: idx,
        revision,
        chunk_id: cid,
      };
    });

    atomicWriteJsonSync(path.join(this.chunksDir, `${docId}.json`), records);
    this.chunksByDoc.set(docId, records);

    doc.status = 'ready';
    doc.stage = '就绪';
    doc.progress = 100;
    doc.error = null;
    doc.index_version = 'heading-v2';
    doc.revision = revision;
    this.kbRev += 1;
    this.saveRegistry();
  }

  listDocuments(kbId?: string): DocumentRecord[] {
    return this.docs
      .filter((d) => !kbId || d.kb_id === kbId)
      .map((d) => ({ ...d }));
  }

  rebuildDocument(docId: string, kbId: string): DocumentRecord {
    const doc = this.docs.find((d) => d.doc_id === docId);
    if (!doc || doc.kb_id !== kbId) {
      throw new Error('资料不存在');
    }
    if (doc.status === 'processing') {
      throw new Error('资料已在处理中');
    }
    const uploadPath = path.join(this.uploadsDir, `${docId}${doc.ext}`);
    if (!fs.existsSync(uploadPath)) {
      throw new Error('原件不存在，请重新上传');
    }
    doc.status = 'processing';
    doc.stage = '等待重建';
    doc.progress = 0;
    doc.error = null;
    this.kbRev += 1;
    this.saveRegistry();
    this.enqueueIndexing(docId);
    return { ...doc };
  }

  deleteDocument(docId: string, kbId?: string): boolean {
    const doc = this.docs.find((d) => d.doc_id === docId);
    if (!doc || (kbId && doc.kb_id !== kbId)) {
      return false;
    }
    if (doc.status === 'processing') {
      throw new Error('文档正在索引中，请稍后再删除');
    }
    this.chunksByDoc.delete(docId);
    const chunkFile = path.join(this.chunksDir, `${docId}.json`);
    if (fs.existsSync(chunkFile)) fs.unlinkSync(chunkFile);
    const uploadPath = path.join(this.uploadsDir, `${docId}${doc.ext}`);
    if (fs.existsSync(uploadPath)) fs.unlinkSync(uploadPath);
    this.docs = this.docs.filter((d) => d.doc_id !== docId);
    this.kbRev += 1;
    this.saveRegistry();
    return true;
  }

  deleteScope(kbId: string): void {
    const scopeDocs = this.listDocuments(kbId);
    if (scopeDocs.some((d) => d.status === 'processing')) {
      throw new Error('资料仍在索引，请完成后再删除对话');
    }
    for (const d of scopeDocs) {
      this.deleteDocument(d.doc_id, kbId);
    }
  }

  getReadyChunks(kbId: string): ChunkRecord[] {
    const readyDocs = this.docs.filter((d) => d.kb_id === kbId && d.status === 'ready');
    const out: ChunkRecord[] = [];
    for (const d of readyDocs) {
      const list = this.chunksByDoc.get(d.doc_id) || [];
      for (const c of list) {
        if (c.revision === d.revision) out.push(c);
      }
    }
    return out;
  }
}

let indexingDelayMsForTest = 0;

export function setIndexingDelayForTest(ms: number): void {
  indexingDelayMsForTest = ms;
}

export let kbManager = new KBManager();

export function reloadKbManager(dataRoot?: string): void {
  kbManager = new KBManager(dataRoot);
}

export function retrieveEvidenceResult(
  sessionId: string,
  query: string,
  options: { kbId?: string; topK?: number; minScore?: number } = {}
): RetrievalResult {
  const scope = options.kbId || sessionKbId(sessionId);
  const topK = options.topK ?? 5;
  const threshold = options.minScore ?? getEvidenceMinScore();
  const chunks = kbManager.getReadyChunks(scope);

  if (!chunks.length) {
    return { query, scope, evidence: [], top_score: 0, threshold, candidate_count: 0 };
  }

  const qTokens = tokenizeLexical(query);
  if (!qTokens.length) {
    return { query, scope, evidence: [], top_score: 0, threshold, candidate_count: 0 };
  }

  const docTokensList = chunks.map((c) => tokenizeLexical(c.text));
  const avgLen =
    docTokensList.reduce((acc, t) => acc + t.length, 0) / Math.max(1, docTokensList.length);
  const df = new Map<string, number>();
  for (const tokens of docTokensList) {
    for (const t of new Set(tokens)) {
      df.set(t, (df.get(t) || 0) + 1);
    }
  }

  const N = chunks.length;
  const k1 = 1.5;
  const b = 0.75;
  const uniqueQuery = Array.from(new Set(qTokens));
  const cleanQuery = query.trim().toLowerCase();
  const qVec = charNgramVector(query);

  const bm25Scores = chunks.map((_chunk, idx) => {
    const tokens = docTokensList[idx];
    const tf = new Map<string, number>();
    for (const t of tokens) tf.set(t, (tf.get(t) || 0) + 1);
    let bm25 = 0;
    for (const qt of uniqueQuery) {
      const freq = tf.get(qt) || 0;
      if (freq > 0) {
        const nq = df.get(qt) || 0;
        const idf = Math.log(1 + (N - nq + 0.5) / (nq + 0.5));
        const denom = freq + k1 * (1 - b + b * (tokens.length / Math.max(1, avgLen)));
        bm25 += idf * ((freq * (k1 + 1)) / denom);
      }
    }
    return bm25 > 0 ? bm25 / (bm25 + 3.0) : 0;
  });

  const vecScores = chunks.map((chunk) => cosineSim(qVec, charNgramVector(chunk.text)));

  const vecIndices = chunks
    .map((_, i) => i)
    .sort((a, b) => vecScores[b] - vecScores[a])
    .slice(0, 8);
  const bm25Indices = chunks
    .map((_, i) => i)
    .sort((a, b) => bm25Scores[b] - bm25Scores[a])
    .slice(0, 5);

  const vecSet = new Set(vecIndices);
  const bm25Set = new Set(bm25Indices);
  const candIndices = Array.from(new Set([...vecIndices, ...bm25Indices]));

  const ranked = candIndices
    .map((idx) => {
      const chunk = chunks[idx];
      const lowerText = chunk.text.toLowerCase();
      const phraseBonus = cleanQuery.length >= 2 && lowerText.includes(cleanQuery) ? 0.35 : 0;
      const meaningfulTokens = uniqueQuery.filter((t) => t.length >= 2);
      const tokensSet = new Set(docTokensList[idx]);
      const matchedMeaningful = meaningfulTokens.filter((t) => tokensSet.has(t)).length;
      const coverageBonus = meaningfulTokens.length
        ? (matchedMeaningful / meaningfulTokens.length) * 0.25
        : 0;
      const score = Math.min(1, bm25Scores[idx] * 0.45 + vecScores[idx] * 0.35 + phraseBonus + coverageBonus);
      const channels: Array<'vector' | 'bm25'> = [];
      if (vecSet.has(idx)) channels.push('vector');
      if (bm25Set.has(idx)) channels.push('bm25');
      return { chunk, score, channels };
    })
    .sort((a, b) => b.score - a.score);

  const topScore = ranked.length ? Number(ranked[0].score.toFixed(5)) : 0;
  console.info(
    `[retrieval] scope=${scope} threshold=${threshold.toFixed(4)} top_scores=${JSON.stringify(
      ranked.slice(0, 5).map((r) => Number(r.score.toFixed(5)))
    )}`
  );

  const evidence: EvidenceChunk[] = [];
  for (const item of ranked) {
    if (item.score < threshold) continue;
    evidence.push({
      ref: evidence.length + 1,
      text: item.chunk.text,
      name: item.chunk.doc_name || '资料',
      chunk: item.chunk.chunk_index + 1,
      score: Number(item.score.toFixed(4)),
      doc_id: item.chunk.doc_id,
      chunk_id: item.chunk.chunk_id,
      channels: item.channels,
      metadata: {
        kb_id: item.chunk.kb_id,
        doc_id: item.chunk.doc_id,
        doc_name: item.chunk.doc_name,
        chunk_index: item.chunk.chunk_index,
        revision: item.chunk.revision,
        chunk_id: item.chunk.chunk_id,
      },
    });
    if (evidence.length >= topK) break;
  }

  return {
    query,
    scope,
    evidence,
    top_score: topScore,
    threshold,
    candidate_count: candIndices.length,
  };
}

export function retrieveEvidence(
  sessionId: string,
  query: string,
  options: { kbId?: string; topK?: number; minScore?: number } = {}
): EvidenceChunk[] {
  return retrieveEvidenceResult(sessionId, query, options).evidence;
}
