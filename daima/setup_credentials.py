"""
一次性凭证配置脚本：把密钥写入 Windows 凭据管理器（OS 级隐藏存储）。

用法：
    python setup_credentials.py            # 交互式逐项配置（输入不回显）
    python setup_credentials.py --check    # 只检查当前配置状态
"""
import getpass
import sys

sys.path.insert(0, ".")

from core.credentials import SERVICE_NAME, get_credential, mask, set_credential

FIELDS = [
    ("api_key", "ANTHROPIC_API_KEY（输入不回显）", True, True),      # (字段名, 提示, 必填, 是否密钥)
    ("base_url", "ANTHROPIC_BASE_URL（直接回车 = 官方默认）", False, False),
    ("serper_key", "SERPER_API_KEY（输入不回显）", True, True),
]


def check():
    print(f"=== 凭证状态检查（服务名: {SERVICE_NAME}）===")
    all_ok = True
    for field, tip, required, secret in FIELDS:
        try:
            value = get_credential(field, required=False)
        except Exception:
            value = None
        status = "✅" if value else ("⚠️ 未设置" if required else "（可选，未设置）")
        if required and not value:
            all_ok = False
        print(f"  {field:<12} {status:<14} {mask(value)}")
    print("\n结论：", "配置齐全，可以运行程序。" if all_ok else "缺少必填凭证，请运行 python setup_credentials.py 配置。")
    return all_ok


def main():
    print(f"=== {SERVICE_NAME} 凭证配置（写入 Windows 凭据管理器）===\n")
    for field, tip, required, secret in FIELDS:
        while True:
            if secret:
                value = getpass.getpass(f"{tip}: ").strip()
            else:
                value = input(f"{tip}: ").strip()
            if value or not required:
                break
            print("  必填项，请输入。")
        if not value:
            print(f"  ⏭️ 跳过 {field}")
            continue
        if field == "base_url" and value.lower() in ("y", "官方", "default", ""):
            value = "https://api.anthropic.com"
        set_credential(field, value)
        print(f"  ✅ {field} 已安全存储（{mask(value)}）")
    print("\n完成！可在 控制面板 → 用户账户 → 凭据管理器 → Windows 凭据 中查看/修改。")
    print("运行 python setup_credentials.py --check 可随时检查状态。")


if __name__ == "__main__":
    sys.exit(check() if "--check" in sys.argv else main())
