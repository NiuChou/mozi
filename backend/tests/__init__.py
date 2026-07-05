"""墨子后端单元测试包 (Phase Tests)。

针对 Foundation+Features (auth / txn / stream_reset / 计量 / skill 工具桥 / export-delete)
补齐 test-adequacy 7 条 + 断言强度。

约束 (信创/本地优先): 零新增 pip 依赖 —— 真实适配器用 httpx 内置 MockTransport,
异步测试用 stdlib unittest.IsolatedAsyncioTestCase。
"""
