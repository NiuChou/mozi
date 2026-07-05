# 贡献指南 · Contributing

感谢参与墨子。本项目本地优先、零 key / 零外呼可跑，贡献门槛低——`make bootstrap` 即可上手。

## 环境准备

前置：Python 3.12+、Node 20+。

```bash
make bootstrap   # 装后端 + 前端依赖, 并启用 pre-push 本地 CI 钩子
make dev         # 同起后端(8000) + 前端(5173)
```

无需任何 API key：缺 key 的模型自动走本地 mock，端到端可跑。接入真实模型见 [README](README.md#接入真实模型可选)。

## 提交前自检

改动落地前请本地跑通（pre-push 钩子也会拦）：

```bash
make lint                                                    # ruff
cd backend && .venv/bin/python smoke_test.py                 # 冒烟 48 项
cd backend && .venv/bin/python -m unittest discover -s tests # 单元/回归
cd backend && .venv/bin/python -m tests.eval.run_eval        # 离线 eval 不回退基线
cd frontend && npm test && npm run build                     # 前端单测 + 类型检查 + 构建
```

任一红都不要提 PR。改了行为就补/改对应测试——非平凡逻辑（分支、循环、解析、money/security 路径）至少留一个可跑断言。

## 约定

- **分支**：从 `main` 切 `feat/*`、`fix/*`、`docs/*`；一 PR 一主题，diff 越小越好。
- **提交信息**：`type(scope): 摘要`（如 `fix(gateway): 修复重试污染工具参数`）。中文/英文均可。
- **代码风格**：跟随周边代码——注释密度、命名、惯用法保持一致；后端过 `ruff`。
- **硬约束别破**：零 key / 零外呼可降级、`user_id` 行级隔离、单一 egress 出网门、sovereign 信创硬过滤。触及这些的改动请在 PR 里说明为何不破。
- **安全**：绝不提交密钥。密钥只放 `backend/.env.local`（已 gitignored）；`.env.example` 只留空模板。

## Bug / 需求

开 Issue，附复现步骤、期望 vs 实际、环境（OS / Python / Node 版本）。安全问题请私下联系维护者，勿公开披露。

## 许可

提交即表示同意你的贡献以 [Apache-2.0](LICENSE) 授权发布。
