// 零安装: 仅用 node 内置 node:test + node:assert。
// 覆盖契约: (a) 拼接/截断 data: 帧 → delta/usage/done 增量解析, 半截 JSON 不崩;
//          (b) stream_reset → reduce 清空当前正文; (c) 多 delta 顺序拼接。
import { test } from "node:test";
import assert from "node:assert/strict";
import { parseSSE, initState, reduceEvent, reduceEvents } from "../src/sse.mjs";

const frame = (obj) => `data: ${JSON.stringify(obj)}\n\n`;

test("parseSSE: 单帧 delta 正常解析, 无剩余半包", () => {
  const { events, rest } = parseSSE(frame({ type: "delta", text: "你好" }));
  assert.equal(rest, "");
  assert.deepEqual(events, [{ type: "delta", text: "你好" }]);
});

test("parseSSE: 多帧一次性解析 delta/usage/done", () => {
  const buf =
    frame({ type: "delta", text: "A" }) +
    frame({ type: "usage", prompt_tokens: 3, completion_tokens: 1, cost_cny: 0, model: "m", fallback_used: false }) +
    frame({ type: "done", message_id: "mid", session_id: "sid" });
  const { events, rest } = parseSSE(buf);
  assert.equal(rest, "");
  assert.equal(events.length, 3);
  assert.equal(events[0].type, "delta");
  assert.equal(events[1].type, "usage");
  assert.equal(events[2].type, "done");
});

test("parseSSE: 截断帧 → 半包留在 rest, 拼回下一块后可解析", () => {
  const full = frame({ type: "delta", text: "hello world" });
  const cut = Math.floor(full.length / 2);
  const chunk1 = full.slice(0, cut);
  const chunk2 = full.slice(cut) + frame({ type: "done", message_id: "m", session_id: "s" });

  // 第一块: 半截 data 帧, 不应产生事件, 不崩
  const r1 = parseSSE(chunk1);
  assert.deepEqual(r1.events, []);
  assert.ok(r1.rest.length > 0);

  // 第二块: 把上轮 rest 拼回再解析
  const r2 = parseSSE(r1.rest + chunk2);
  assert.equal(r2.events.length, 2);
  assert.deepEqual(r2.events[0], { type: "delta", text: "hello world" });
  assert.equal(r2.events[1].type, "done");
});

test("parseSSE: 半截 JSON 不崩, 仅跳过损坏帧", () => {
  const buf = `data: {"type":"delta","text":"ok"}\n\n` + `data: {"type":"delta","text":\n\n`;
  const { events, rest } = parseSSE(buf);
  assert.deepEqual(events, [{ type: "delta", text: "ok" }]);
  assert.equal(rest, ""); // 损坏帧后跟 \n\n, 已被消费但解析失败 → 被忽略
});

test("parseSSE: 非 data 行被忽略 (注释/心跳)", () => {
  const buf = `: keep-alive\n\n` + frame({ type: "delta", text: "x" });
  const { events } = parseSSE(buf);
  assert.deepEqual(events, [{ type: "delta", text: "x" }]);
});

test("parseSSE: 逐字符喂入也能跨块拼出完整帧", () => {
  const stream = frame({ type: "delta", text: "墨子" }) + frame({ type: "delta", text: "上线" });
  let buf = "";
  const collected = [];
  for (const ch of stream) {
    buf += ch;
    const { events, rest } = parseSSE(buf);
    buf = rest;
    collected.push(...events);
  }
  assert.deepEqual(collected, [
    { type: "delta", text: "墨子" },
    { type: "delta", text: "上线" },
  ]);
});

test("reduceEvent: 多 delta 顺序拼接", () => {
  let s = initState();
  for (const t of ["墨", "子", "·", "Chat"]) {
    s = reduceEvent(s, { type: "delta", text: t });
  }
  assert.equal(s.text, "墨子·Chat");
});

test("reduceEvent: stream_reset 清空当前正文, 再继续接 delta", () => {
  let s = initState();
  s = reduceEvent(s, { type: "delta", text: "半截被降级的内容" });
  assert.equal(s.text, "半截被降级的内容");
  s = reduceEvent(s, { type: "stream_reset", reason: "fallback" });
  assert.equal(s.text, ""); // 契约B: 清空
  assert.equal(s.resets, 1);
  s = reduceEvent(s, { type: "delta", text: "重试后" });
  s = reduceEvent(s, { type: "delta", text: "的正文" });
  assert.equal(s.text, "重试后的正文");
});

test("reduceEvent: usage / done / session / fallback 快照", () => {
  let s = initState();
  s = reduceEvent(s, { type: "session", session_id: "sid-1" });
  assert.equal(s.sessionId, "sid-1");
  s = reduceEvent(s, { type: "fallback", from_model: "a", to_model: "b" });
  assert.equal(s.fallbackUsed, true);
  s = reduceEvent(s, { type: "usage", prompt_tokens: 10, completion_tokens: 5, cost_cny: 0.01, model: "b", fallback_used: true });
  assert.equal(s.usage.prompt_tokens, 10);
  s = reduceEvent(s, { type: "done", message_id: "mid-9", session_id: "sid-2" });
  assert.equal(s.done, true);
  assert.equal(s.messageId, "mid-9");
  assert.equal(s.sessionId, "sid-2");
});

test("reduceEvent: 未知/空事件不改变状态", () => {
  const s0 = initState();
  assert.equal(reduceEvent(s0, null), s0);
  assert.equal(reduceEvent(s0, { type: "weird" }), s0);
});

test("端到端: parseSSE → reduceEvents 复刻一次降级重试对话", () => {
  const stream =
    frame({ type: "session", session_id: "s1" }) +
    frame({ type: "delta", text: "草稿" }) +
    frame({ type: "stream_reset", reason: "degrade" }) +
    frame({ type: "delta", text: "最终" }) +
    frame({ type: "delta", text: "回答" }) +
    frame({ type: "usage", prompt_tokens: 8, completion_tokens: 4, cost_cny: 0, model: "m", fallback_used: true }) +
    frame({ type: "done", message_id: "m1", session_id: "s1" });
  const { events, rest } = parseSSE(stream);
  assert.equal(rest, "");
  const s = reduceEvents(initState(), events);
  assert.equal(s.text, "最终回答");
  assert.equal(s.resets, 1);
  assert.equal(s.done, true);
  assert.equal(s.sessionId, "s1");
  assert.equal(s.messageId, "m1");
  assert.equal(s.usage.completion_tokens, 4);
});

test("error 事件: 全降级失败 → 记录 error 且 done, 不留空泡卡死", () => {
  const stream =
    frame({ type: "delta", text: "片段" }) +
    frame({ type: "stream_reset" }) +
    frame({ type: "error", detail: "所有模型降级均失败", fallback_chain: ["glm-5.2"] });
  const { events } = parseSSE(stream);
  const s = reduceEvents(initState(), events);
  assert.equal(s.error, "所有模型降级均失败");
  assert.equal(s.done, true);
  assert.equal(s.text, ""); // stream_reset 已清空, error 不再追加正文
});

// ---- B-MAJOR4: 'id: N\ndata: {...}' 同帧逐行解析 + lastId 透出 ----
test("parseSSE: id 行 + data 行同帧 → 事件解析且 lastId 命中", () => {
  const buf = `id: 7\ndata: ${JSON.stringify({ type: "delta", text: "续传" })}\n\n`;
  const { events, lastId } = parseSSE(buf);
  assert.deepEqual(events, [{ type: "delta", text: "续传" }]);
  assert.equal(lastId, "7");
});

test("parseSSE: 无 id 帧 lastId 为 null", () => {
  const { lastId } = parseSSE(frame({ type: "delta", text: "x" }));
  assert.equal(lastId, null);
});

test("parseSSE: 多帧取最后一个 id", () => {
  const buf =
    `id: 1\ndata: ${JSON.stringify({ type: "delta", text: "a" })}\n\n` +
    `id: 2\ndata: ${JSON.stringify({ type: "delta", text: "b" })}\n\n`;
  const { events, lastId } = parseSSE(buf);
  assert.equal(events.length, 2);
  assert.equal(lastId, "2");
});

test("parseSSE: 注释行 + id 行 + data 行混合不丢 data", () => {
  const buf = `: heartbeat\nid: 3\ndata: ${JSON.stringify({ type: "done", message_id: "m", session_id: "s" })}\n\n`;
  const { events, lastId } = parseSSE(buf);
  assert.equal(events.length, 1);
  assert.equal(events[0].type, "done");
  assert.equal(lastId, "3");
});
