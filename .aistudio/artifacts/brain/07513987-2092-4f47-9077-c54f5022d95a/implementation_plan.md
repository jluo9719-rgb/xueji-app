# 学记个人学习备考助手 — 剩余改造与验收实施计划

This plan reconciles your audit checklist (Items 1–10 and F1–F14) against the current Node.js/Express application, preserving every fix already in place while closing the remaining persistence, LLM integration, citation validation, health-check, cleanup, and automated acceptance gaps.

## User Review & Critical Decisions

> [!IMPORTANT]
> The following decisions were confirmed during interactive clarification and govern this implementation:

- **Confirmed Scope (Items 1–10 First)**: Focus exclusively on the minimum viable reliable workflow (`上传 → 问答 → 错题收录 → 出题练习 → 查看解析`) and automated acceptance tests; **Subject Space (Item 11)** remains deferred until after acceptance passes.
- **Confirmed Persistence Strategy**: Persist sessions, messages, mistake cards, uploaded raw files, and chunk indexes as atomic JSON files under a configurable `XUEJI_DATA_ROOT` directory (defaulting to `./data`), enabling server-restart recovery and isolated test runs.
- **Confirmed LLM Backend Support**: Retain Anthropic Messages API and local Ollama support while adding first-class server-side **Gemini API** support (`@google/genai` with `gemini-4-hydrogen` default).
- **Confirmed Acceptance Suite**: Add an automated acceptance test suite covering **A1–A11** in an isolated temporary `XUEJI_DATA_ROOT` without touching production data.

---

## 1. Current Gap Audit (Already Fixed vs. Remaining)

Your pasted audit identified **F1–F14** and **Items 1–11**. Inspection of the current application confirms that most UI and chunking/exam fixes are already implemented:

| Area | Already Implemented | Remaining Gap to Fix |
|---|---|---|
| **Session & UI (F1–F14)** | `localStorage` session restore (F1), no duplicate question bubble on general confirm (F2), `no_evidence` history persistence (F3), `AbortController` stop button (F4), 4-step progress bar (F5), `detail` error parsing & duplicate counter (F6–F7), memory drawer removed from UI (F8), filtered mistake stats (F10), collapsible exam answers (F11), exam preview reset & error display (F12–F13), viewport zoom enabled (F14) | Live LLM connectivity indicator & active data directory display in System Status modal (**F9 / B9**) |
| **Storage & Recovery (A10, B8)** | In-memory sessions, documents, chunks, and mistakes | All state currently vanishes on restart; needs `XUEJI_DATA_ROOT` file persistence and stale `processing` recovery |
| **LLM Integration** | Anthropic & Ollama `fetch` wrapper | Missing server-side `@google/genai` Gemini API integration and clean missing-credential error surfacing |
| **Retrieval & Grounding (Items 3–5)** | Markdown heading chunking, 800-char cap, 50-char merge, BM25-style scoring, `quoteInSource` exam validation, `unreviewed` exam status | Missing structured retrieval metadata (`channels`, `top_score`), threshold calibration (`0.12` default + top-5 score logs), partial-marker line stripping for `NO_DOCUMENT_EVIDENCE`, and no-ref citation repair verification |
| **Cleanup & Tests (Items 1, 9, 10)** | Legacy Python runtime removed | Unused `/memory` backend endpoints still present; no automated **A1–A11** acceptance suite |

---

## 2. User Experience & Visual Design

- **Preserve Existing Visual System**: Keep the existing "现代东方书院" aesthetic (`#221E1B` ink header/sidebar, `#FCF9F2` rice-paper workspace, `#C8553D` cinnabar accent, `#D4A574` gold highlights, `#4E7D65` sage status badges) with zero layout or restyling changes.
- **System Status Modal Refinement (F9 / B9)**: When clicking the header seal button, trigger a live connectivity probe (`/health?check=1`) and display:
  - Active LLM provider and model name
  - Real connectivity state (`服务可连接` vs. `未配置凭证` / `连接失败，请检查服务或密钥`)
  - Persistence mode (`文件持久化`) and active data root path (`data_dir`)
- **Clear Grounded vs. Missing-Credential Feedback**: When no LLM key is configured, show a clear, actionable error state in chat/exam preview rather than returning unverified raw excerpts or hanging silently.

---

## 3. Key Product Decisions & Trade-Offs

- **Atomic JSON File Persistence (`XUEJI_DATA_ROOT`)**
  - *Chosen Approach*: Store session metadata + transcripts, mistake cards, document registry, uploaded originals, and indexed chunks as JSON/binary files inside `XUEJI_DATA_ROOT` using write-to-temp-then-rename atomic saves.
  - *Why*: Survives server restarts (`B8`), preserves interrupted-index recovery (`A10`), requires zero native C++ compilation in Node.js 22, and allows tests to run cleanly in an isolated temp directory (`Item 1`).
  - *Alternatives Considered*: Native `sqlite3` (blocked by lack of native build tools in container) or in-memory only (fails `B8` and `A10`).

- **Multi-Provider Server-Side LLM Router (Gemini + Anthropic + Ollama)**
  - *Chosen Approach*: Support `@google/genai` (`gemini-4-hydrogen`) automatically when `GEMINI_API_KEY` is present, alongside Anthropic Messages API (`ANTHROPIC_API_KEY`) and Ollama (`OLLAMA_BASE_URL`), selectable via `LLM_PROVIDER`.
  - *Why*: Lets the app run natively inside AI Studio using `GEMINI_API_KEY` while preserving full backward compatibility with Anthropic and local Ollama deployments.

- **Clean Removal of Dead Legacy Surface**
  - *Chosen Approach*: Remove the unused long-term memory store and `/memory` API endpoints (`Item 9`) while keeping all active chat, document, mistake-book, and exam endpoints intact.

---

## 4. Technical Architecture & Data Strategy

```
┌──────────────────────────────────────────────────────────────────────┐
│                   Browser UI (Single-Page HTML/CSS/JS)               │
│  ┌──────────────┐  ┌────────────────┐  ┌──────────┐  ┌────────────┐  │
│  │ Session Rail │  │ Docs & SSE Chat│  │ Mistakes │  │ Exam View  │  │
│  └──────┬───────┘  └───────┬────────┘  └────┬─────┘  └─────┬──────┘  │
└─────────┼──────────────────┼────────────────┼──────────────┼─────────┘
          │ REST + SSE       │ Multipart/SSE  │ REST         │ REST
┌─────────▼──────────────────▼────────────────▼──────────────▼─────────┐
│                    Express HTTP/SSE Server (:3000)                   │
│  ┌─────────────────┐  ┌───────────────────┐  ┌────────────────────┐  │
│  │  Chat & Exam    │  │  Document Loader  │  │   LLM Router       │  │
│  │  Orchestrator   │  │  Chunker & BM25   │  │  • Gemini SDK      │  │
│  │  • Grounding    │  │  Hybrid Retriever │  │  • Anthropic API   │  │
│  │  • Ref Repair   │  │  • Scope Isolation│  │  • Ollama API      │  │
│  └────────┬────────┘  └─────────┬─────────┘  └────────────────────┘  │
│           │                     │                                    │
│  ┌────────▼─────────────────────▼─────────────────────────────────┐  │
│  │        Persistent Store (Rooted at XUEJI_DATA_ROOT)            │  │
│  │  • sessions.json   • mistakes.json   • kb_registry.json        │  │
│  │  • uploads/        • chunks/         • startup stale recovery  │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

### Data Model & State Flows

- **Data Root Configuration**: Resolve `XUEJI_DATA_ROOT` from environment (default `./data`). On startup, load persisted state from disk; mark any document left in `processing` state as `error` ("上次索引被中断，请点击重建索引重试") so incomplete indexes never masquerade as ready (**A10**).
- **Structured Retrieval & Calibration**: Return `RetrievalResult` containing `evidence` (`ref`, `doc_id`, `chunk_id`, `chunk_index`, `doc_name`, `text`, `score`, `channels`), `top_score`, `threshold` (default `0.12`, configurable via `EVIDENCE_MIN_SCORE`), and `candidate_count`. Log top-5 retrieval scores per query.
- **Strict Grounded Answer Validation (Item 5 / A5)**:
  - Standalone `NO_DOCUMENT_EVIDENCE` (after trimming whitespace) triggers `NeedsGeneralAnswer` and persists the `no_evidence` pair.
  - `NO_DOCUMENT_EVIDENCE` accompanied by substantive content strips only the marker line and proceeds as a partial grounded answer.
  - Missing or out-of-range `[n]` citations trigger one citation-repair pass; if invalid or empty citations remain, mark `meta.mode = 'unverified'`, prepend `【出处未验证，请核对原文】`, and omit the reference list.
- **Automated Acceptance Test Suite (A1–A11)**: Spawn an isolated test instance pointed at a fresh temporary `XUEJI_DATA_ROOT` with a deterministic mock LLM hook to verify document parsing (UTF-8, GBK, MD, PDF, DOCX, empty, invalid ext, duplicate), session scope isolation, threshold gating, citation validation, `no_evidence` persistence, mistake CRUD/export, exam generation/review/rejection handling, cascade delete & `409` guards, restart persistence & stale recovery, and non-blocking session listing under load.
