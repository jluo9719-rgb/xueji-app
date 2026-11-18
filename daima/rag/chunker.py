"""按标题路径和段落切分，限制字符长度，不为每一行单独运行嵌入模型。"""
import re
from core.config import config

CHUNK_VERSION = 'heading-v2'
_HEADING = re.compile(r'^(#{1,6})\s+(.+)$')
_NUMBER = re.compile(r'^(?:-\s*)?(\d+(?:\.\d+)+)\s*\S+')


def chunk_document(embedder, text: str) -> list[str]:
    maximum = max(200, int(config.get('retrieval.chunk_max_chars', 800)))
    sections, path, lines = [], [], []
    def flush():
        if lines:
            sections.append((' > '.join(path), '\n'.join(lines).strip()))
            lines.clear()
    for line in text.splitlines():
        stripped = line.strip()
        heading = _HEADING.match(stripped)
        numbered = _NUMBER.match(stripped)
        if heading or numbered:
            flush()
            level = len(heading[1]) if heading else numbered[1].count('.') + 1
            title = heading[2] if heading else stripped
            path = path[:level-1] + [title]
        elif stripped:
            lines.append(line)
    flush()
    result = []
    for title, body in sections:
        prefix = ('章节：' + title[:160] + '\n') if title else ''
        budget = maximum-len(prefix)
        parts, current = [], ''
        # 保留每行内容（包括表格），超长单行才切开。
        for line in body.splitlines():
            segments = []
            while len(line) > budget:
                ends = [m.end() for m in re.finditer(r'[。！？；.!?;]', line[:budget])]
                cut = ends[-1] if ends else budget
                segments.append(line[:cut])
                line = line[cut:]
            if line:
                segments.append(line)
            for segment in segments:
                if current and len(current)+len(segment)+1>budget:
                    parts.append(current); current=''
                current += ('\n' if current else '')+segment
        if current: parts.append(current)
        if len(parts)>1 and len(parts[-1])<50:
            combined=parts[-2]+'\n'+parts[-1]
            if len(combined)<=budget:
                parts[-2:]=[combined]
            else:
                ends = [m.end() for m in re.finditer(r'[。！？；.!?;]', combined)
                        if len(combined)-budget <= m.end() <= budget]
                cut = min(ends, key=lambda n: abs(n-len(combined)//2)) if ends else len(combined)//2
                parts[-2:]=[combined[:cut],combined[cut:]]
        for part in parts:
            block=prefix+part
            if len(block)<50 and result and len(result[-1])+len(block)+2<=maximum:
                result[-1]+='\n\n'+block
            else: result.append(block)
    return result


def rough_chunk(text):
    return chunk_document(None,text)


def semantic_chunk(embedder,text):
    return chunk_document(embedder,text)
