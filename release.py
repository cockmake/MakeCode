"""
发布脚本 - 自动生成 version.json 并准备上传文件。
用法: python release.py --release_log LOG
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

from version import CURRENT_VERSION, UPDATE_SERVER_URL


def get_sha256(file_path: Path) -> str:
    """计算文件的 SHA256。"""
    sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)
    return sha.hexdigest()


def main():
    parser = argparse.ArgumentParser(description="生成 version.json 发布文件")
    parser.add_argument("--release_log", required=True, help="发布日志（markdown 格式）")
    args = parser.parse_args()

    exe_path = Path("dist") / "MakeCode.exe"
    if not exe_path.exists():
        print(f"❌ 找不到 {exe_path}，请先运行 pyinstaller MakeCode.spec")
        sys.exit(1)

    sha256 = get_sha256(exe_path)
    version_info = {
        "version": CURRENT_VERSION,
        "download_url": f"{UPDATE_SERVER_URL}/MakeCode.exe",
        "sha256": sha256,
        "release_log": args.release_log,
    }

    output = Path("dist") / "version.json"
    output.write_text(json.dumps(version_info, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"[OK] 已生成 {output}")
    print(f"   版本: {CURRENT_VERSION}")
    print(f"   发布日志: {args.release_log}")
    print(f"   SHA256: {sha256}")
    print()
    print("请将 dist 目录下的文件上传到服务器:")
    print(f"   1. MakeCode.exe  ->  {UPDATE_SERVER_URL}/MakeCode.exe")
    print(f"   2. version.json  ->  {UPDATE_SERVER_URL}/version.json")


if __name__ == "__main__":
    main()
