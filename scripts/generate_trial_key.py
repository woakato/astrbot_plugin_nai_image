#!/usr/bin/env python3
"""
试用密钥加密工具（方案 D — 代码内 XOR 混淆）

用法：
    python scripts/generate_trial_key.py <你的NAI公益密钥>

输出：
    1. 控制台打印可粘贴到 main.py 的 _TRIAL_KEY_ENC 常量值
    2. 同时写入 .trial_key_enc 文件备用

步骤：
    1. 运行此脚本，传入公益密钥
    2. 将输出的字符串粘贴到 main.py 中 _TRIAL_KEY_ENC = "..." 的引号内
    3. 重新打包发布插件
"""
import base64
import sys
import os

# 必须与 main.py 中的 _TRIAL_OBF_KEY 完全一致
_TRIAL_OBF_KEY = b"nai_plugin_trial_2024_obf_key"


def encrypt_trial_key(raw_key: str) -> str:
    """将明文密钥 XOR 加密后输出 base64 字符串。"""
    raw_bytes = raw_key.strip().encode("utf-8")
    key_len = len(_TRIAL_OBF_KEY)
    encrypted = bytes(
        raw_bytes[i] ^ _TRIAL_OBF_KEY[i % key_len]
        for i in range(len(raw_bytes))
    )
    return base64.b64encode(encrypted).decode("ascii")


def decrypt_trial_key(encrypted_b64: str) -> str:
    """验证用：从 base64 密文还原明文密钥。"""
    encrypted = base64.b64decode(encrypted_b64.strip())
    key_len = len(_TRIAL_OBF_KEY)
    decrypted = bytes(
        encrypted[i] ^ _TRIAL_OBF_KEY[i % key_len]
        for i in range(len(encrypted))
    )
    return decrypted.decode("utf-8").rstrip("\x00").strip()


def main():
    if len(sys.argv) < 2:
        print("用法: python scripts/generate_trial_key.py <你的NAI公益密钥>")
        sys.exit(1)

    raw_key = sys.argv[1]
    if not raw_key:
        print("错误：密钥不能为空")
        sys.exit(1)

    encrypted = encrypt_trial_key(raw_key)

    # 验证加解密一致性
    decrypted = decrypt_trial_key(encrypted)
    assert decrypted == raw_key.strip(), "加解密验证失败！"

    # 备份文件
    output_path = os.path.join(os.path.dirname(__file__), "..", ".trial_key_enc")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(encrypted)

    print("✅ 加密成功！")
    print()
    print("将以下字符串粘贴到 main.py 中 _TRIAL_KEY_ENC 的引号内：")
    print()
    print(f'    _TRIAL_KEY_ENC = "{encrypted}"')
    print()
    print(f"   原始密钥: {raw_key[:4]}{'*' * (len(raw_key) - 4)}")
    print(f"   备份文件: {os.path.abspath(output_path)}")
    print()
    print("注意：请勿将原始密钥提交到 Git 仓库！")


if __name__ == "__main__":
    main()
