# FTS5 中文分词扩展 (wangfenjin/simple) — 信创编译/分发

墨子 BM25 走 SQLite FTS5。默认 `unicode61` 分词器不切 CJK，故墨子在**应用层用
`embedder.tokenize()` 预切词**写入/查询（中文按字、英文按词），与内存 BM25 同口径，
中文召回不退化。若要更高精度（拼音/同义），可装 wangfenjin/simple 扩展。

## 是否需要
- **不装**：`unicode61 + 预切词`，零依赖，召回接近现状，**默认路径**。
- **装 simple**：原生 CJK 分词 + 拼音，精度更高。非 pip 包，须按平台自编译 `.so/.dylib`。

## 三平台编译 (CPU 架构 ABI 不可移植，须分别编译)
```bash
# x86_64 麒麟/统信
gcc -fPIC -shared -I sqlite simple.c cppjieba -o libsimple.so
# aarch64 鲲鹏 (同 toolchain 交叉编译)
aarch64-linux-gnu-gcc -fPIC -shared -I sqlite simple.c cppjieba -o libsimple.so
# 龙芯 LoongArch
loongarch64-linux-gnu-gcc -fPIC -shared -I sqlite simple.c cppjieba -o libsimple.so
```
产物放 `backend/vendor/libsimple/<arch>/libsimple.so`（约定 gitignored，不入仓污染零依赖核心）。

## 启用
```bash
export MOZI_FTS_SIMPLE_PATH=backend/vendor/libsimple/x86_64/libsimple.so
backend/.venv/bin/python backend/scripts/check_simple.py   # 自检可加载
```
启用后 `chunks_fts` 建表时 `tokenize='simple'`（缺失则 `unicode61`+预切词）。

## 降级
缺 `.so` / 缺 `MOZI_FTS_SIMPLE_PATH` → `unicode61 + embedder.tokenize()` 预切词，
全检索回归仍绿；缺 FTS5 编译 → 二级兜底内存 BM25 (`bm25.py`)。
