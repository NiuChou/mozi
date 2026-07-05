// 零安装: 仅用 node 内置 node:test + node:assert (与 sse.test.mjs 同栈, 不引新依赖)。
// 覆盖 v3 契约新增的两个 StreamEvent 变体:
//   - quota_degrade { type, forced_model, reason }  (G2: 配额硬上限强制降级)
//   - reconnecting  { type, attempt }               (G5: SSE 断线重连提示, attempt 从 1 起)
// 重点 (按下游集成约定):
//   (a) parseSSE 必须原样解析出这两类帧, 字段不丢 (forced_model/reason/attempt);
//   (b) 带 'id: N' 同帧时 data 不被吞、lastId 命中 (Last-Event-ID 续传不破);
//   (c) 截断/半包场景下这两类帧不崩、拼回后可还原;
//   (d) reduceEvent 当前对这两类未建专门 case → 走 default 透传 (状态不变),
//       这是 src/sse.mjs 的现行行为契约 (UI 在 onEvent switch 层消费, 而非归约层),
//       此处断言其确实「不污染 text/done/usage 等归约快照」。
import { test } from "node:test";
import assert from "node:assert/strict";
import { parseSSE, initState, reduceEvent, reduceEvents } from "../src/sse.mjs";

const frame = (obj) => `data: ${JSON.stringify(obj)}\n\n`;

test("parseSSE: quota_degrade 帧解析, forced_model/reason 字段不丢", () => {
  const { events, rest, lastId } = parseSSE(
    frame({ type: "quota_degrade", forced_model: "glm-4-flash", reason: "over_hard_cap" })
  );
  assert.equal(rest, "");
  assert.equal(lastId, null);
  assert.deepEqual(events, [
    { type: "quota_degrade", forced_model: "glm-4-flash", reason: "over_hard_cap" },
  ]);
});

test("parseSSE: reconnecting 帧解析, attempt 数值原样保留", () => {
  const { events } = parseSSE(frame({ type: "reconnecting", attempt: 1 }));
  assert.equal(events.length, 1);
  assert.equal(events[0].type, "reconnecting");
  assert.equal(events[0].attempt, 1);
  // 数值类型不应被解析成字符串
  assert.equal(typeof events[0].attempt, "number");
});

test("parseSSE: 'id: N' + quota_degrade 同帧 → 事件不被吞且 lastId 命中", () => {
  const buf =
    `id: 42\ndata: ${JSON.stringify({ type: "quota_degrade", forced_model: "m-cheap", reason: "budget" })}\n\n`;
  const { events, lastId } = parseSSE(buf);
  assert.deepEqual(events, [{ type: "quota_degrade", forced_model: "m-cheap", reason: "budget" }]);
  assert.equal(lastId, "42");
});

test("parseSSE: 'id: N' + reconnecting 同帧 → attempt 与 lastId 同时正确", () => {
  const buf = `id: 9\ndata: ${JSON.stringify({ type: "reconnecting", attempt: 3 })}\n\n`;
  const { events, lastId } = parseSSE(buf);
  assert.equal(events.length, 1);
  assert.equal(events[0].attempt, 3);
  assert.equal(lastId, "9");
});

test("parseSSE: reconnecting → 续传 delta → done 多帧交织, 顺序与计数正确", () => {
  const buf =
    frame({ type: "reconnecting", attempt: 1 }) +
    frame({ type: "reconnecting", attempt: 2 }) +
    frame({ type: "delta", text: "续" }) +
    frame({ type: "quota_degrade", forced_model: "fm", reason: "r" }) +
    frame({ type: "delta", text: "传" }) +
    frame({ type: "done", message_id: "m", session_id: "s" });
  const { events, rest } = parseSSE(buf);
  assert.equal(rest, "");
  assert.equal(events.length, 6);
  assert.deepEqual(
    events.map((e) => e.type),
    ["reconnecting", "reconnecting", "delta", "quota_degrade", "delta", "done"]
  );
  assert.equal(events[0].attempt, 1);
  assert.equal(events[1].attempt, 2);
  assert.equal(events[3].forced_model, "fm");
});

test("parseSSE: quota_degrade 帧被截断 → 半包留 rest, 拼回后还原不丢字段", () => {
  const full = frame({ type: "quota_degrade", forced_model: "glm-4-flash", reason: "over_hard_cap" });
  const cut = Math.floor(full.length / 2);
  const r1 = parseSSE(full.slice(0, cut));
  assert.deepEqual(r1.events, []); // 半截不应产出事件
  assert.ok(r1.rest.length > 0);
  const r2 = parseSSE(r1.rest + full.slice(cut));
  assert.deepEqual(r2.events, [
    { type: "quota_degrade", forced_model: "glm-4-flash", reason: "over_hard_cap" },
  ]);
  assert.equal(r2.rest, "");
});

test("parseSSE: 逐字符喂入 reconnecting+quota_degrade 也能跨块拼出完整帧", () => {
  const stream =
    frame({ type: "reconnecting", attempt: 2 }) +
    frame({ type: "quota_degrade", forced_model: "fm", reason: "hard" });
  let buf = "";
  const collected = [];
  for (const ch of stream) {
    buf += ch;
    const { events, rest } = parseSSE(buf);
    buf = rest;
    collected.push(...events);
  }
  assert.deepEqual(collected, [
    { type: "reconnecting", attempt: 2 },
    { type: "quota_degrade", forced_model: "fm", reason: "hard" },
  ]);
});

test("reduceEvent: quota_degrade 走 default 透传, 不污染归约快照 (UI 层消费, 非归约层)", () => {
  let s = initState();
  s = reduceEvent(s, { type: "delta", text: "正文" });
  const before = s;
  const after = reduceEvent(s, { type: "quota_degrade", forced_model: "fm", reason: "r" });
  // 现行 src 无专门 case → 返回同一引用, 状态完全不变
  assert.equal(after, before);
  assert.equal(after.text, "正文");
  assert.equal(after.done, false);
  assert.equal(after.usage, null);
});

test("reduceEvent: reconnecting 走 default 透传, 不清空已累积正文 (重连不丢草稿)", () => {
  let s = initState();
  s = reduceEvent(s, { type: "delta", text: "重连前已收到的内容" });
  const after = reduceEvent(s, { type: "reconnecting", attempt: 1 });
  assert.equal(after.text, "重连前已收到的内容"); // 不被当成 stream_reset 清空
  assert.equal(after.resets, 0);
  assert.equal(after.done, false);
});

test("端到端: 重连 + 配额降级 + 续传, parseSSE→reduceEvents 仍还原最终正文/快照", () => {
  const stream =
    frame({ type: "session", session_id: "s1" }) +
    frame({ type: "delta", text: "开头" }) +
    frame({ type: "reconnecting", attempt: 1 }) +
    frame({ type: "quota_degrade", forced_model: "glm-4-flash", reason: "over_hard_cap" }) +
    frame({ type: "delta", text: "结尾" }) +
    frame({ type: "usage", prompt_tokens: 5, completion_tokens: 2, cost_cny: 0, model: "glm-4-flash", fallback_used: false }) +
    frame({ type: "done", message_id: "m9", session_id: "s1" });
  const { events, rest } = parseSSE(stream);
  assert.equal(rest, "");
  assert.equal(events.length, 7);
  const s = reduceEvents(initState(), events);
  // reconnecting/quota_degrade 不参与文本归约 → 正文仍是两段 delta 顺序拼接
  assert.equal(s.text, "开头结尾");
  assert.equal(s.resets, 0); // reconnecting 不是 stream_reset, 不计入 resets
  assert.equal(s.done, true);
  assert.equal(s.messageId, "m9");
  assert.equal(s.sessionId, "s1");
  assert.equal(s.usage.completion_tokens, 2);
  assert.equal(s.error, null);
});
