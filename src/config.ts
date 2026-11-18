import fs from 'node:fs';
import path from 'node:path';

export function getDataRoot(): string {
  const raw = process.env.XUEJI_DATA_ROOT?.trim() || './data';
  return path.resolve(process.cwd(), raw);
}

export function getEvidenceMinScore(): number {
  const raw = process.env.EVIDENCE_MIN_SCORE;
  if (raw !== undefined && raw.trim() !== '') {
    const parsed = Number(raw);
    if (!Number.isNaN(parsed) && parsed >= 0 && parsed <= 1) {
      return parsed;
    }
  }
  return 0.12;
}

export function getAnswerabilityCheckEnabled(): boolean {
  return process.env.ANSWERABILITY_CHECK === 'true';
}

export function ensureDirSync(dirPath: string): void {
  fs.mkdirSync(dirPath, { recursive: true });
}

export function readJsonSync<T>(filePath: string, fallback: T): T {
  try {
    if (!fs.existsSync(filePath)) return fallback;
    const raw = fs.readFileSync(filePath, 'utf8');
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

export function atomicWriteJsonSync(filePath: string, data: unknown): void {
  ensureDirSync(path.dirname(filePath));
  const tmpPath = `${filePath}.tmp.${process.pid}.${Date.now()}`;
  fs.writeFileSync(tmpPath, JSON.stringify(data, null, 2), 'utf8');
  fs.renameSync(tmpPath, filePath);
}
