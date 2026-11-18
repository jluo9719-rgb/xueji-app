"""第七期：出题功能验证脚本

测试链路：
1. 从错题本生成试卷（POST /exam/from-mistakes）
2. 从知识库主题生成试卷（POST /exam/from-topic）
3. 检查试题结构、引用溯源、Markdown 格式
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.log import setup_logging
setup_logging()

from core.mistakes import MistakeBook
from core.exam_builder import (
    retrieve_for_mistakes, retrieve_for_topic,
    generate_questions, review_questions, build_exam_markdown
)
from core.log import get_logger

log = get_logger(__name__)


async def test_from_mistakes():
    """测试：从错题本生成试卷"""
    print("\n=== 测试 1：从错题本生成试卷 ===")

    # 1. 获取错题本卡片
    mb = MistakeBook()
    cards = mb.list_all()
    if not cards:
        print("❌ 错题本为空，跳过此测试（需先运行聊天产生错题）")
        mb.close()
        return

    mistake_ids = [c["id"] for c in cards[:2]]  # 取前 2 个
    print(f"选取错题卡片：{mistake_ids}")
    mb.close()

    # 2. 检索
    print("📥 检索相关资料...")
    retrieved = retrieve_for_mistakes(mistake_ids, top_k_per=3)
    print(f"检索完成，共 {len(retrieved)} 个卡片有资料")
    for item in retrieved:
        print(f"  - 卡片 #{item['mistake_id']}：{item['topic']} - {item['confusion'][:30]}")
        print(f"    检索到 {len(item['chunks'])} 条 chunk")

    if not retrieved or not any(item["chunks"] for item in retrieved):
        print("❌ 无相关资料，跳过（知识库可能为空）")
        return

    # 3. 出题
    print("\n📝 出题中...")
    all_questions = []
    for item in retrieved:
        if not item["chunks"]:
            continue
        context = f"{item['topic']} - {item['confusion']}"
        questions = await generate_questions(item["chunks"], count=2, context=context)
        print(f"  - 卡片 #{item['mistake_id']} 生成 {len(questions)} 题")

        # 4. 审题
        reviews = await review_questions(questions, item["chunks"])
        valid_count = sum(1 for r in reviews if r.get("valid"))
        print(f"    审题通过 {valid_count}/{len(questions)} 题")

        valid_qs = [q for q, r in zip(questions, reviews) if r.get("valid")]
        all_questions.extend(valid_qs)

    if not all_questions:
        print("❌ 无通过审题的题目")
        return

    # 5. 检查试题结构
    print(f"\n✅ 共生成 {len(all_questions)} 题，检查结构...")
    for i, q in enumerate(all_questions, 1):
        assert "type" in q and q["type"] in ["choice", "fill", "short"], f"题{i}类型错误"
        assert "question" in q and q["question"], f"题{i}缺少问题"
        assert "answer" in q and q["answer"], f"题{i}缺少答案"
        assert "explanation" in q, f"题{i}缺少解析"
        if q["type"] == "choice":
            assert "options" in q and len(q["options"]) == 4, f"题{i}选择题选项不足4个"
        print(f"  - 题{i}（{q['type']}）：{q['question'][:40]}... 答案：{q['answer'][:20]}")

    # 6. 拼装 Markdown
    print("\n📄 拼装试卷...")
    metadata = {"source_type": "mistakes", "mistake_count": len(mistake_ids)}
    markdown = build_exam_markdown(all_questions, title="测试-错题本检测卷", metadata=metadata)

    # 保存
    output = Path(__file__).parent.parent / "test_exam_mistakes.md"
    output.write_text(markdown, encoding="utf-8")
    print(f"✅ 试卷已保存：{output}")
    print(f"   字数：{len(markdown)} 字符")

    # 检查引用溯源
    if "📖 来源" in markdown:
        print("✅ 引用溯源标注存在")
    else:
        print("⚠️ 未找到引用溯源标注")


async def test_from_topic():
    """测试：从知识库主题生成试卷"""
    print("\n=== 测试 2：从知识库主题生成试卷 ===")

    topic = "内部控制"  # 手册中的主题

    # 1. 检索
    print(f"📥 检索主题「{topic}」...")
    chunks = retrieve_for_topic(topic, kb_id="default", top_k=10)
    if not chunks:
        print(f"❌ 未检索到「{topic}」相关资料（知识库可能为空）")
        return
    print(f"检索到 {len(chunks)} 条 chunk")

    # 2. 出题
    print("\n📝 出题中...")
    questions = await generate_questions(chunks, count=5, context=f"主题：{topic}")
    print(f"生成 {len(questions)} 题")

    # 3. 审题
    print("🔍 审题中...")
    reviews = await review_questions(questions, chunks)
    valid_qs = [q for q, r in zip(questions, reviews) if r.get("valid")]
    print(f"审题通过 {len(valid_qs)}/{len(questions)} 题")

    if not valid_qs:
        print("❌ 无通过审题的题目")
        return

    # 4. 检查
    print(f"\n✅ 检查题目结构...")
    for i, q in enumerate(valid_qs, 1):
        assert "type" in q, f"题{i}缺少type"
        assert "question" in q, f"题{i}缺少question"
        assert "answer" in q, f"题{i}缺少answer"
        print(f"  - 题{i}（{q['type']}）：{q['question'][:40]}...")

    # 5. 拼装
    print("\n📄 拼装试卷...")
    metadata = {"source_type": "topic", "topic": topic}
    markdown = build_exam_markdown(valid_qs, title=f"「{topic}」主题检测卷", metadata=metadata)

    output = Path(__file__).parent.parent / "test_exam_topic.md"
    output.write_text(markdown, encoding="utf-8")
    print(f"✅ 试卷已保存：{output}")
    print(f"   字数：{len(markdown)} 字符")


async def test_dedupe():
    """测试：错题去重"""
    print("\n=== 测试 3：错题卡片去重 ===")

    mb = MistakeBook()
    before = mb.count()
    print(f"去重前：共 {before['total']} 题")

    if before["total"] < 2:
        print("⚠️ 卡片少于 2 张，跳过去重测试")
        mb.close()
        return

    merged = mb.deduplicate_cards(threshold=0.85)
    after = mb.count()

    print(f"去重后：共 {after['total']} 题")
    print(f"✅ 合并了 {merged} 张相似卡片")
    mb.close()


async def main():
    print("=" * 60)
    print("第七期：出题功能验证")
    print("=" * 60)

    try:
        await test_from_mistakes()
    except Exception as e:
        print(f"❌ 测试 1 失败：{e}")
        import traceback
        traceback.print_exc()

    try:
        await test_from_topic()
    except Exception as e:
        print(f"❌ 测试 2 失败：{e}")
        import traceback
        traceback.print_exc()

    try:
        await test_dedupe()
    except Exception as e:
        print(f"❌ 测试 3 失败：{e}")
        import traceback
        traceback.print_exc()

    print("\n" + "=" * 60)
    print("验证完成")


if __name__ == "__main__":
    asyncio.run(main())
