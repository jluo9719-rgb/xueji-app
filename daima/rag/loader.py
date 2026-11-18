"""文档读取：txt / pdf(md) 统一转纯文本。PDF 走 pymupdf4llm（激活原代码未使用的依赖）。"""
from pathlib import Path

from core.log import get_logger

log = get_logger(__name__)


def load_document(path: str) -> str:
    """
    按扩展名分发：
      .pdf  → pymupdf4llm 转 Markdown 再取文本
      其他  → 按 utf-8 文本读取，去掉空行
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"文档不存在: {path}")
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        return _load_pdf(p)
    if suffix == ".docx":
        return _load_docx(p)
    return _load_txt(p)


def _load_txt(p: Path) -> str:
    raw = p.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gb18030")
        except UnicodeDecodeError as error:
            raise ValueError("无法识别文本编码，请另存为 UTF-8 后重新上传") from error
    if not text.strip():
        raise ValueError("文件没有可读取的文字")
    # 原逻辑保留：去空行再拼回
    lines = [l for l in text.split("\n") if l.strip()]
    log.info("已读取文本文档 %s（%d 行非空）", p.name, len(lines))
    return "\n".join(lines)


def _load_pdf(p: Path) -> str:
    import pymupdf4llm  # 延迟导入：只有真的处理 PDF 时才加载
    log.info("正在解析 PDF %s ...", p.name)
    md_text = pymupdf4llm.to_markdown(str(p))
    if not md_text.strip():
        raise ValueError("PDF 未提取到文字，可能是扫描件；请先 OCR 识别后上传")
    lines = [l for l in md_text.split("\n") if l.strip()]
    log.info("PDF 解析完成，%d 行 Markdown", len(lines))
    return "\n".join(lines)


def _load_docx(p: Path) -> str:
    """Word 文档：正文段落 + 表格行（管道分隔，切分器会把 | 行并入当前块）。"""
    import docx  # python-docx，延迟导入
    log.info("正在解析 Word %s ...", p.name)
    d = docx.Document(str(p))
    parts = [para.text for para in d.paragraphs if para.text.strip()]
    for table in d.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append("| " + " | ".join(cells) + " |")
    log.info("Word 解析完成，%d 段", len(parts))
    return "\n".join(parts)
