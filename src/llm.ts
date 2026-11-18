import { GoogleGenAI } from '@google/genai';

export interface LlmMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
}

export interface LlmResponse {
  text: string;
  stop_reason?: string | null;
}

export type LlmCaller = (params: {
  messages: LlmMessage[];
  system?: string;
  maxTokens?: number;
  timeoutMs?: number;
}) => Promise<LlmResponse>;

let testLlmCaller: LlmCaller | null = null;
let geminiClient: GoogleGenAI | null = null;
let cachedGeminiKey: string | undefined;

export function setLlmCallerForTest(caller: LlmCaller | null): void {
  testLlmCaller = caller;
}

function getGeminiClient(apiKey: string): GoogleGenAI {
  if (!geminiClient || cachedGeminiKey !== apiKey) {
    cachedGeminiKey = apiKey;
    geminiClient = new GoogleGenAI({
      apiKey,
      httpOptions: {
        headers: {
          'User-Agent': 'aistudio-build',
        },
      },
    });
  }
  return geminiClient;
}

export function maskSecret(value: string | undefined): string {
  if (!value) return '<未设置>';
  if (value.length <= 8) return '****';
  return `****${value.slice(-4)}`;
}

export function getLlmStatus() {
  const requestedProvider = (process.env.LLM_PROVIDER?.trim() || 'auto').toLowerCase();
  const geminiKey = process.env.GEMINI_API_KEY?.trim();
  const anthropicKey = process.env.ANTHROPIC_API_KEY?.trim();
  const ollamaUrl = process.env.OLLAMA_BASE_URL?.trim();

  let provider: 'gemini' | 'anthropic' | 'ollama' = 'gemini';
  if (requestedProvider === 'gemini' || requestedProvider === 'anthropic' || requestedProvider === 'ollama') {
    provider = requestedProvider;
  } else if (anthropicKey) {
    provider = 'anthropic';
  } else if (ollamaUrl) {
    provider = 'ollama';
  } else if (geminiKey) {
    provider = 'gemini';
  }

  let configured = false;
  let model = 'gemini-4-hydrogen';
  let baseUrl = 'https://generativelanguage.googleapis.com';
  let keyMasked = '<未配置>';

  if (provider === 'gemini') {
    configured = Boolean(geminiKey);
    model = process.env.GEMINI_MODEL?.trim() || 'gemini-4-hydrogen';
    baseUrl = 'https://generativelanguage.googleapis.com';
    keyMasked = geminiKey ? maskSecret(geminiKey) : '<未配置>';
  } else if (provider === 'anthropic') {
    configured = Boolean(anthropicKey);
    model = process.env.LLM_MODEL?.trim() || 'claude-sonnet-4-20250514';
    baseUrl = process.env.ANTHROPIC_BASE_URL?.trim() || 'https://api.anthropic.com';
    keyMasked = anthropicKey ? maskSecret(anthropicKey) : '<未配置>';
  } else {
    configured = Boolean(ollamaUrl);
    model = process.env.OLLAMA_MODEL?.trim() || 'qwen3:8b';
    baseUrl = ollamaUrl || 'http://localhost:11434';
    keyMasked = ollamaUrl ? 'ollama' : '<未配置>';
  }

  return {
    configured: configured || Boolean(testLlmCaller),
    provider,
    model,
    baseUrl,
    keyMasked,
  };
}

export async function checkLlmConnectivity(): Promise<{
  reachable: boolean;
  reason?: string;
}> {
  if (testLlmCaller) return { reachable: true };
  const status = getLlmStatus();
  if (!status.configured) {
    return { reachable: false, reason: '未配置大模型凭证' };
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 3000);

  try {
    if (status.provider === 'ollama') {
      const resp = await fetch(`${status.baseUrl.replace(/\/$/, '')}/api/tags`, {
        signal: controller.signal,
      });
      return resp.ok
        ? { reachable: true }
        : { reachable: false, reason: `Ollama HTTP ${resp.status}` };
    }

    if (status.provider === 'gemini') {
      const ai = getGeminiClient(process.env.GEMINI_API_KEY!.trim());
      await ai.models.get({ model: status.model });
      return { reachable: true };
    }

    const resp = await fetch(`${status.baseUrl.replace(/\/$/, '')}/v1/models`, {
      signal: controller.signal,
      headers: {
        'x-api-key': process.env.ANTHROPIC_API_KEY!.trim(),
        'anthropic-version': '2023-06-01',
      },
    });
    return resp.ok || resp.status === 404
      ? { reachable: true }
      : { reachable: false, reason: `API HTTP ${resp.status}` };
  } catch (err: any) {
    return { reachable: false, reason: String(err?.message || '连接失败').slice(0, 120) };
  } finally {
    clearTimeout(timer);
  }
}

export async function callLlm(params: {
  messages: LlmMessage[];
  system?: string;
  maxTokens?: number;
  timeoutMs?: number;
}): Promise<LlmResponse> {
  if (testLlmCaller) {
    return testLlmCaller(params);
  }

  const status = getLlmStatus();
  if (!status.configured) {
    throw new Error(
      '未配置大模型凭证：请在环境变量中设置 GEMINI_API_KEY、ANTHROPIC_API_KEY 或 OLLAMA_BASE_URL'
    );
  }

  const systemParts = [
    params.system,
    ...params.messages.filter((m) => m.role === 'system').map((m) => m.content),
  ].filter(Boolean);
  const cleanMessages = params.messages
    .filter((m) => m.role !== 'system')
    .map((m) => ({ role: m.role, content: m.content }));

  if (status.provider === 'gemini') {
    const ai = getGeminiClient(process.env.GEMINI_API_KEY!.trim());
    const contents = cleanMessages.map((m) => ({
      role: m.role === 'assistant' ? 'model' : 'user',
      parts: [{ text: m.content }],
    }));
    const config: Record<string, unknown> = {};
    if (systemParts.length) {
      config.systemInstruction = systemParts.join('\n\n');
    }
    const response = await ai.models.generateContent({
      model: status.model,
      contents,
      config: Object.keys(config).length ? config : undefined,
    });
    const finishReason = response.candidates?.[0]?.finishReason;
    return {
      text: response.text ?? '',
      stop_reason: finishReason === 'MAX_TOKENS' ? 'max_tokens' : finishReason || 'end_turn',
    };
  }

  const maxTokens = params.maxTokens ?? 4000;
  const timeoutMs = params.timeoutMs ?? 120_000;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    if (status.provider === 'ollama') {
      const endpoint = `${status.baseUrl.replace(/\/$/, '')}/api/chat`;
      const ollamaMessages = systemParts.length
        ? [{ role: 'system', content: systemParts.join('\n\n') }, ...cleanMessages]
        : cleanMessages;

      const resp = await fetch(endpoint, {
        method: 'POST',
        signal: controller.signal,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model: status.model,
          messages: ollamaMessages,
          stream: false,
          options: { num_predict: maxTokens },
        }),
      });
      if (!resp.ok) {
        const body = await resp.text();
        throw new Error(`Ollama 调用失败 (${resp.status}): ${body.slice(0, 200)}`);
      }
      const data = (await resp.json()) as {
        message?: { content?: string };
        done_reason?: string;
      };
      return {
        text: data.message?.content || '',
        stop_reason: data.done_reason === 'length' ? 'max_tokens' : data.done_reason || 'end_turn',
      };
    }

    const anthropicKey = process.env.ANTHROPIC_API_KEY!.trim();
    const endpoint = `${status.baseUrl.replace(/\/$/, '')}/v1/messages`;
    const payload: Record<string, unknown> = {
      model: status.model,
      max_tokens: maxTokens,
      messages: cleanMessages,
    };
    if (systemParts.length) {
      payload.system = systemParts.join('\n\n');
    }

    const resp = await fetch(endpoint, {
      method: 'POST',
      signal: controller.signal,
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': anthropicKey,
        'anthropic-version': '2023-06-01',
      },
      body: JSON.stringify(payload),
    });

    if (!resp.ok) {
      const body = await resp.text();
      throw new Error(`LLM 调用失败 (${resp.status}): ${body.slice(0, 200)}`);
    }

    const data = (await resp.json()) as {
      content?: Array<{ type?: string; text?: string }>;
      stop_reason?: string;
    };
    const text = (data.content || [])
      .filter((b) => b.type === 'text' && typeof b.text === 'string')
      .map((b) => b.text!)
      .join('');
    return { text, stop_reason: data.stop_reason || null };
  } finally {
    clearTimeout(timer);
  }
}
