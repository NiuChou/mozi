#!/usr/bin/env python3
"""自检 wangfenjin/simple FTS5 中文分词扩展是否可加载 (配 MOZI_FTS_SIMPLE_PATH)。

用法: MOZI_FTS_SIMPLE_PATH=/path/to/libsimple .venv/bin/python backend/scripts/check_simple.py
"""
import os
import sqlite3
import sys

path = os.environ.get("MOZI_FTS_SIMPLE_PATH")
if not path:
    print("✗ 未设 MOZI_FTS_SIMPLE_PATH (缺则墨子降级 unicode61+预切词, 不退化)")
    sys.exit(1)
conn = sqlite3.connect(":memory:")
try:
    conn.enable_load_extension(True)
    conn.load_extension(path)
    row = conn.execute("SELECT simple_query('墨子是本地优先应用')").fetchone()
    print(f"✓ simple 扩展可用: simple_query 返回 {row}")
except Exception as e:  # noqa: BLE001
    print(f"✗ simple 扩展加载失败: {e}")
    sys.exit(1)
finally:
    conn.close()
