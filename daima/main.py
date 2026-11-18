"""
学记个人学习备考助手 — 正式入口（CLI 对话模式）

用法：
    python main.py              # 进入对话
    python main.py --check      # 环境自检后退出
    python main.py --new        # 创建独立空白对话，开新对话
"""
import argparse
import asyncio
import sys

sys.path.insert(0, ".")

from core.log import setup_logging
setup_logging()

from core.config import config
from core.credentials import get_anthropic_config, get_serper_key, get_llm_model, mask
from core.log import get_logger
from core.document_chat import NeedsGeneralAnswer

log = get_logger(__name__)

BANNER = """
============================================================
  学记个人学习备考助手 v2.1
  输入问题即可对话；exit 退出；/new 开新会话；资料上传请使用网页
  /mistakes 错题本；/mistakes dedupe 去重相似错题
============================================================
"""


def check() -> bool:
    ok = True
    try:
        key, url = get_anthropic_config()
        print(f"✅ LLM 配置   {get_llm_model()} @ {url}（尚未请求模型）")
    except Exception as e:
        print(f"❌ LLM 凭证缺失：{e}")
        ok = False
    return ok


async def run(new_session: bool) -> None:
    from core.engine import AgentEngine

    print(BANNER)
    engine = AgentEngine()
    engine.setup_tools()
    if new_session:
        from uuid import uuid4
        engine.session_id = 's_' + uuid4().hex
        engine.session.create_session(engine.session_id, '新对话')
        print('已创建空白对话；原对话保留')
    print(f"💬 {engine.start()}")
    print("（首次使用知识检索需加载模型，请稍候）\n")

    while True:
        try:
            query = input("你：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break
        if not query:
            continue
        if query.lower() == "exit":
            print("再见！")
            break
        if query == "/new":
            engine.messages = []
            from uuid import uuid4
            engine.session_id = 's_' + uuid4().hex
            engine.session.create_session(engine.session_id, '新对话')
            print("已创建空白对话；资料上传请在网页选择此对话\n")
            continue
        if query == "/mistakes":
            cards = engine.mistakes.list_all()
            if cards:
                print(f"📕 错题本（共 {len(cards)} 题）：")
                for c in cards[:10]:  # 只显示前 10 条
                    mark = "✅" if c["status"] == "已掌握" else "❌"
                    print(f"   {mark} #{c['id']} {c['topic'] or '未分类'} - {c['confusion'][:40]}")
                if len(cards) > 10:
                    print(f"   ... 还有 {len(cards) - 10} 条，访问网页端查看全部")
            else:
                print("📕 错题本还是空的")
            print()
            continue
        if query == "/mistakes dedupe":
            print("📕 开始去重相似错题卡片...")
            count = engine.mistakes.deduplicate_cards()
            print(f"✅ 已合并 {count} 张相似卡片\n")
            continue

        t0 = __import__("time").time()
        try:
            answer = await engine.chat(query)
            print(f"\n助手：{answer}\n（耗时 {__import__('time').time()-t0:.1f}s）\n")
        except NeedsGeneralAnswer as e:
            print(str(e))
            if input("仅此问题使用模型通用知识回答？输入 y 确认：").strip().lower() == 'y':
                try:
                    print(await engine.chat(query, allow_general=True))
                except Exception as error:
                    print(f"回答失败：{error}")
        except Exception as e:
            log.exception("处理用户提问失败")
            print(f"⚠️ 出错了（已记录日志）：{str(e)[:200]}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="学记个人学习备考助手")
    parser.add_argument("--check", action="store_true", help="环境自检")
    parser.add_argument("--new", action="store_true", help="创建独立空白对话")
    args = parser.parse_args()

    if args.check:
        return 0 if check() else 1
    if not check():
        print("\n环境自检未通过，请先运行 python setup_credentials.py")
        return 1
    asyncio.run(run(new_session=args.new))
    return 0


if __name__ == "__main__":
    sys.exit(main())
