"""
一次性迁移脚本：把旧项目 .env 里的密钥迁入 Windows 凭据管理器。

安全特性：
  - 读取的值全程不回显、不打印（只显示掩码）
  - 迁移完成后提示手动删除 .env

用法：
    python migrate_env.py                          # 默认找 ../agent/.env
    python migrate_env.py "D:\\path\\to\\.env"     # 指定 .env 路径
"""
import sys
from pathlib import Path

sys.path.insert(0, ".")

from core.log import setup_logging
setup_logging()  # 强制控制台 UTF-8

from core.credentials import mask, set_credential

# .env 变量名 → 凭据管理器字段名
MAPPING = {
    "ANTHROPIC_API_KEY": "api_key",
    "ANTHROPIC_BASE_URL": "base_url",
    "SERPER_API_KEY": "serper_key",
}

DEFAULT_ENV = Path(__file__).resolve().parent.parent.parent / "agent" / ".env"


def parse_env(path: Path) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE，忽略注释和空行。"""
    result = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip("'\"")
    return result


def main() -> int:
    env_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ENV
    if not env_path.exists():
        print(f"❌ 找不到 .env 文件：{env_path}")
        print("   可指定路径：python migrate_env.py \"D:\\path\\.env\"")
        return 1

    print(f"=== 从 {env_path} 迁移凭证到 Windows 凭据管理器 ===\n")
    data = parse_env(env_path)
    migrated = 0
    for env_key, field in MAPPING.items():
        value = data.get(env_key)
        if not value:
            print(f"  ⚠️ {env_key} 在 .env 中不存在，跳过")
            continue
        set_credential(field, value)
        migrated += 1
        print(f"  ✅ {field:<10} ← {env_key}  {mask(value)}")

    if migrated == 0:
        print("\n没有迁移任何凭证。")
        return 1
    print(f"\n🎉 已迁移 {migrated} 项。请手动删除旧 .env 文件以免泄露：")
    print(f"   Remove-Item \"{env_path}\"")
    print("\n验证：python setup_credentials.py --check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
