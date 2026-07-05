// 纯函数 SSE 解析 + 事件归约 (无 DOM 依赖, 供 api.ts 复用 / node:test 测试)。
// 与后端 §7 SSE 帧契约对应: 帧以 "\n\n" 分隔, 每帧形如 "data: {json}"。

// parseSSE(buffer): 输入「已累积的字符串缓冲」, 返回本轮可解析出的事件与剩余半包。
//   - 仅消费以 "data:" 开头的完整帧 (末尾出现 "\n\n" 之前不算完整)。
//   - 半截 JSON / 非 data 行被安全跳过, 不抛异常。
//   - 返回 { events, rest }: rest 是尾部未闭合的半包, 调用方应拼回下一块。
export function parseSSE(buffer) {
  const events = [];
  let lastId = null;
  const parts = (buffer ?? "").split("\n\n");
  const rest = parts.pop() ?? ""; // 最后一段可能是半包, 留到下次
  for (const part of parts) {
    // 逐行解析 SSE 多行字段: id: 行更新 lastId, data: 行累积; 注释(:开头)/其他字段跳过。
    // (修复 'id: N\ndata: {...}' 同帧被整段 startsWith('data:') 早退丢弃)
    let payload = null;
    for (const raw of part.split("\n")) {
      const line = raw.trim();
      if (line.startsWith("id:")) lastId = line.slice(3).trim();
      else if (line.startsWith("data:")) payload = line.slice(5).trim();
    }
    if (!payload) continue;
    try {
      events.push(JSON.parse(payload));
    } catch {
      /* 半截 / 损坏 JSON: 安全忽略, 不崩 */
    }
  }
  return { events, rest, lastId };
}

// 归约状态初值。text 为当前助手气泡累积正文; 其余为最近一次相关事件的快照。
export function initState() {
  return {
    text: "",
    usage: null,
    done: false,
    sessionId: null,
    messageId: null,
    fallbackUsed: false,
    resets: 0,
    error: null,
  };
}

// reduceEvent(state, evt) -> 新 state (纯函数, 不可变更新)。
//   - delta: 顺序拼接正文
//   - stream_reset (契约B): 清空当前正文再继续接后续 delta
//   - usage / done / session / fallback: 记录快照
export function reduceEvent(state, evt) {
  if (!evt || typeof evt !== "object") return state;
  switch (evt.type) {
    case "delta":
      return { ...state, text: state.text + (evt.text ?? "") };
    case "stream_reset":
      return { ...state, text: "", resets: state.resets + 1 };
    case "error":
      // 全降级失败: 流正常关闭, 须显式落错误并停 streaming (否则空泡卡死)
      return { ...state, done: true, error: evt.detail || "生成失败" };
    case "usage":
      return { ...state, usage: evt };
    case "session":
      return { ...state, sessionId: evt.session_id ?? state.sessionId };
    case "fallback":
      return { ...state, fallbackUsed: true };
    case "done":
      return {
        ...state,
        done: true,
        messageId: evt.message_id ?? state.messageId,
        sessionId: evt.session_id ?? state.sessionId,
      };
    default:
      return state;
  }
}

// reduceEvents(state, events): 顺序折叠一批事件 (便于测试与上层一次性归约)。
export function reduceEvents(state, events) {
  return (events ?? []).reduce(reduceEvent, state);
}
