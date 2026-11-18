"""问答与出题共用的结构化混合检索；保留完整块和稳定版本 ID。"""
from core.config import config
from core.log import get_logger
from rag.kb_manager import get_kb_manager, session_kb_id
log=get_logger(__name__)

def retrieve_evidence(session_id, query, *, kb_id=None, top_k=5):
    from rag.kb_manager import get_embedder
    from rag.retriever import Reranker
    kb = get_kb_manager()
    scope = kb_id or session_kb_id(session_id)
    ready = [d for d in kb.list_documents(scope) if d['status'] == 'ready']
    if not ready:
        return []
    ids = [d['doc_id'] for d in ready]
    count = sum(d.get('chunk_count', 0) for d in ready)
    if count < 1:
        return []
    q_vec = get_embedder().encode([query])
    # 向量通道：只在查询期间持锁与删除互斥；编码和重排都在锁外，
    # 检索不再阻塞其他会话的上传进度与文档列表轮询。
    with kb._lock:
        result = kb.collection.query(query_embeddings=q_vec,
                                     n_results=min(count, int(config.get("retrieval.vector_top_k", 8))),
                                     where={'$and': [{'kb_id': scope}, {'doc_id': {'$in': ids}}]},
                                     include=['documents', 'metadatas'])
    candidates = [(text, {**meta, 'chunk_id': cid}) for text,meta,cid in zip(result['documents'][0] or [], result['metadatas'][0] or [], result['ids'][0] or [])]
    # BM25 通道：会话范围快照打分（快照构建在 KBManager 锁内），
    # 只保留已就绪文档的文本块，弥补精确术语/编号类查询。
    chunks, metas, bm25 = kb.get_snapshot(scope)
    if bm25 is not None and chunks:
        import jieba
        scores = bm25.get_scores(list(jieba.cut(query)))
        allowed = sorted((i for i, m in enumerate(metas) if m.get('doc_id') in ids),
                         key=lambda i: scores[i], reverse=True)
        seen = {text for text, _ in candidates}
        for i in allowed[:int(config.get('retrieval.bm25_top_k', 5))]:
            if chunks[i] not in seen:
                seen.add(chunks[i])
                candidates.append((chunks[i], metas[i]))
    if not candidates:
        return []
    scores = Reranker.instance().score(query, [c[0] for c in candidates])
    threshold = float(config.get('retrieval.evidence_min_score', 0.12))
    ranked = sorted(zip(scores, candidates), key=lambda item: item[0], reverse=True)
    log.info("检索 scope=%s threshold=%.4f top_scores=%s", scope, threshold,
             [round(float(score), 5) for score, _ in ranked[:5]])
    with kb._lock:
        active = {d['doc_id']: d for d in kb.list_documents(scope) if d['status']=='ready'}
    output=[]
    for score,(text,meta) in ranked:
        doc=active.get(meta.get('doc_id'))
        if not doc or float(score)<threshold or doc.get('revision') != meta.get('revision'):
            continue
        output.append({'ref':len(output)+1, 'text':text, 'name':meta.get('doc_name','资料'),
                       'chunk':meta.get('chunk_index',0)+1,'score':float(score),
                       'doc_id':meta['doc_id'],'chunk_id':meta.get('chunk_id'),
                       'metadata':meta})
        if len(output)>=top_k: break
    return output
