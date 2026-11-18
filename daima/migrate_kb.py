"""
一次性迁移脚本（第五期）：把原内控手册导入新版用户知识库（默认库）。

新版架构下知识库不再来自 config.paths.source_docs 的启动时索引，
而是走"上传 → 后台异步索引"。本脚本把旧手册按新管线重新登记一遍，
迁移完成后老问答效果不变，且旧 chroma "ljs" collection 不再被使用。

用法：
    python migrate_kb.py
"""
import hashlib
import os
import sys
import time

sys.path.insert(0, ".")

from core.log import setup_logging
setup_logging()  # 先配置日志（顺带强制控制台 UTF-8）

from core.config import config
from core.log import get_logger
from rag.kb_manager import get_kb_manager

log = get_logger(__name__)


def main() -> int:
    srcs = config.get("paths.source_docs", [])
    if not srcs or not os.path.exists(srcs[0]):
        print(f"❌ 找不到源文档：{srcs}")
        return 1
    src = srcs[0]
    with open(src, "rb") as f:
        content = f.read()
    doc_id = hashlib.sha256(content).hexdigest()[:12]

    kb = get_kb_manager()
    if any(d["doc_id"] == doc_id for d in kb.list_documents()):
        print("✅ 手册已在知识库中，无需迁移")
        return 0

    rec = kb.add_document(os.path.basename(src), content)
    print(f"已入队：{rec['name']}（{rec['doc_id']}），后台索引中 ...")

    while True:
        time.sleep(2)
        d = next(x for x in kb.list_documents() if x["doc_id"] == rec["doc_id"])
        print(f"  [{d['status']}] {d.get('stage') or ''} {d.get('progress', 0)}%")
        if d["status"] == "error":
            print(f"❌ 索引失败：{d.get('error')}")
            return 1
        if d["status"] == "ready":
            print(f"✅ 迁移完成：{d['chunk_count']} chunks 已可检索")
            return 0


if __name__ == "__main__":
    sys.exit(main())
