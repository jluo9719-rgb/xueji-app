"""第二期非交互端到端测试：引擎初始化 → 记忆提取 → 带工具的完整对话。"""
import asyncio
import sys
import time

sys.path.insert(0, ".")

from core.log import setup_logging
setup_logging()


async def main():
    from core.engine import AgentEngine

    engine = AgentEngine()
    engine.setup_tools()
    print(f"\n[1] 引擎就绪：{engine.start()}")

    # 第一问：带个人偏好（测记忆）+ 知识库（测 RAG 工具）
    t0 = time.time()
    a1 = await engine.chat("我对花生过敏。另外帮我查一下总公司和子公司在内控上分别负责什么？")
    t1 = time.time()
    print(f"\n[2] 第一问回答（{t1-t0:.0f}s）：\n{'-'*60}\n{a1[:500]}\n{'-'*60}")

    # 检查长时记忆是否入库
    items = engine.memory.list_all()
    print(f"\n[3] 长时记忆条目：{items}")
    assert any("花生" in str(v) for _, v in items), "❌ 花生过敏未被记住！"

    # 第二问：测记忆注入 + 会话上下文
    a2 = await engine.chat("我上次说我不能吃什么来着？")
    print(f"\n[4] 第二问回答：\n{'-'*60}\n{a2[:300]}\n{'-'*60}")

    # 检查持久化：messages 里应包含 assistant 最终回答（BUG-03）
    last = engine.messages[-1]
    assert last["role"] == "assistant" and last["content"] == a2, "❌ BUG-03 未修复：最终回答未持久化"
    print(f"\n[5] ✅ 会话持久化正确（最后一条是 assistant 回答，共 {len(engine.messages)} 条消息）")

    # 模拟重启：新建引擎加载会话
    engine2 = AgentEngine()
    engine2.session  # 触发连接
    loaded = engine2.start()
    print(f"[6] 模拟重启加载：{loaded}")

    print("\n🎉 第二期端到端测试全部通过")


if __name__ == "__main__":
    asyncio.run(main())
