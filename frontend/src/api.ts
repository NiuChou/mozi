// 后端 API 客户端。/v1 经 Vite 代理到本地网关 :8000。

import type { ModelSpec, QuotaState, SessionItem, SkillInvokeResult, SkillItem, StreamEvent, VaultDoc } from "./types";
// 复用纯解析函数 (与 node:test 共享同一实现, 保证测试覆盖真实路径)。
// @ts-ignore — .mjs 无类型声明, 运行期由 Vite/bundler 解析。
import { parseSSE } from "./sse.mjs";

const H = { "Content-Type": "application/json" };

// G4: 可选 Bearer 鉴权。默认模式无 token → 返回空对象, 不附 Authorization (行为不变)。
//     多用户模式登录后写入 localStorage 'mozi_user_token', 此后所有请求自动带 Bearer。
function authHeaders(): Record<string, string> {
  try {
    const t = localStorage.getItem("mozi_user_token");
    return t ? { Authorization: `Bearer ${t}` } : {};
  } catch {
    return {}; // localStorage 不可用 (SSR/隐私模式) → 退化为默认模式
  }
}

// 合并 Content-Type / 鉴权 / 调用方附加头, 供所有写请求复用。
function hdrs(extra?: Record<string, string>): Record<string, string> {
  return { ...H, ...authHeaders(), ...extra };
}

async function j<T>(r: Response): Promise<T> {
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json() as Promise<T>;
}

// 注: 所有 fetch 经 authHeaders()/hdrs() 注入可选 Bearer; 默认模式无 token → 不附, 行为不变 (G4)。
export const api = {
  health: () => fetch("/health", { headers: authHeaders() }).then(j<{ status: string; active_providers: string[]; local_first: boolean }>),
  models: () => fetch("/v1/models", { headers: authHeaders() }).then(j<{ models: ModelSpec[]; active_providers: string[]; local_first: boolean }>),
  usage: () => fetch("/v1/usage", { headers: authHeaders() }).then(j<{ tokens_used: number; requests: number; cost_cny: number; period: string }>),
  // G2: 配额可见性 — 拉取当前计划/预算/已用/剩余/降级状态。
  quota: () => fetch("/v1/quota", { headers: authHeaders() }).then(j<QuotaState>),
  sessions: (archived = false) =>
    fetch(`/v1/sessions${archived ? "?archived=1" : ""}`, { headers: authHeaders() }).then(j<{ sessions: SessionItem[] }>),
  // 会话管理: 重命名/归档切换 (PATCH) + 硬删 (DELETE)。
  updateSession: (sid: string, body: { title?: string; archived?: boolean }) =>
    fetch(`/v1/sessions/${sid}`, { method: "PATCH", headers: hdrs(), body: JSON.stringify(body) }).then(j<{ session: SessionItem }>),
  deleteSession: (sid: string) =>
    fetch(`/v1/sessions/${sid}`, { method: "DELETE", headers: authHeaders() }).then(j<{ deleted: string }>),
  messages: (sid: string) => fetch(`/v1/sessions/${sid}/messages`, { headers: authHeaders() }).then(j<{ messages: any[] }>),
  documents: () => fetch("/v1/vault/documents", { headers: authHeaders() }).then(j<{ documents: VaultDoc[] }>),
  archive: (title: string, content: string, type = "笔记") =>
    fetch("/v1/vault/archive", { method: "POST", headers: hdrs(), body: JSON.stringify({ title, content, type }) })
      .then(j<{ doc_id: string; chunks: number; triples: number }>),
  search: (query: string, k = 5) =>
    fetch("/v1/vault/search", { method: "POST", headers: hdrs(), body: JSON.stringify({ query, k }) }).then(j<any>),
  kgGraph: () => fetch("/v1/kg/graph", { headers: authHeaders() }).then(j<{ nodes: any[]; edges: any[] }>),
  // G3: KG N-hop 实体查询 — 以 entity 为中心按 hops 扩展子图, max_nodes/max_edges 限幅。
  kgQuery: (entity: string, hops = 1, maxNodes = 50, maxEdges = 100) =>
    fetch("/v1/kg/query", {
      method: "POST",
      headers: hdrs(),
      body: JSON.stringify({ entity, hops, max_nodes: maxNodes, max_edges: maxEdges }),
    }).then(j<{ nodes: any[]; edges: any[] }>),
  events: (limit = 30) => fetch(`/v1/events?limit=${limit}`, { headers: authHeaders() }).then(j<{ events: any[] }>),
  skills: () => fetch("/v1/skills", { headers: authHeaders() }).then(j<{ skills: SkillItem[] }>),
  discoverSkills: () => fetch("/v1/skills/discover", { method: "POST", headers: authHeaders() }).then(j<{ discovered: number }>),
  invokeSkill: (skill_id: string, input: string) =>
    fetch("/v1/skills/invoke", { method: "POST", headers: hdrs(), body: JSON.stringify({ skill_id, input }) }).then(j<SkillInvokeResult>),
};
// 注: /v1/route-preview 后端端点与 RoutePreviewRequest 保留 (smoke 路由预览段用); 前端无调用方故删客户端封装。

export interface ChatParams {
  text: string;
  sessionId: string | null;
  policy: string;
  privacyTier: string;
  injectContext: boolean;
  model: string | null;
}

// 手写 fetch+parseSSE+指数退避; 不依赖第三方 SSE 库
// (信创/离线/零依赖, 且唯一可在重连前回写 session_id 防孤儿 session/二次计费)。
// G8: 429 (配额/限流瞬时) 纳入可重试 — 退避后多半已恢复, 与网关侧 5xx 同等对待。
const RETRYABLE = (s: number) => s === 0 || s === 429 || s === 502 || s === 503 || s === 504;

// 流式对话: 解析 SSE, 逐事件回调。signal 用于中断在途请求(停止/切会话竞态)。
// 断线退避重连: 重连前回写最新 currentSessionId, 命中原 session (Last-Event-ID 触发尾态回放)。
export async function streamChat(
  p: ChatParams,
  onEvent: (e: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  let currentSessionId = p.sessionId;     // 可变: session 事件回写, 每次重连经 buildBody 读最新
  let lastEventId: string | null = null;
  let done = false;
  const buildBody = () => JSON.stringify({
    messages: [{ role: "user", content: p.text }],
    stream: true,
    session_id: currentSessionId,         // 重连取最新 → 命中原 session, 不新建孤儿
    inject_context: p.injectContext,
    model: p.model,
    routing: { policy: p.policy, privacy_tier: p.privacyTier },
  });
  const handle = (e: StreamEvent) => {
    if (e.type === "session") currentSessionId = (e as any).session_id; // 回写
    onEvent(e);
    if (e.type === "done" || e.type === "error") done = true;
  };
  for (let attempt = 0; attempt <= 3 && !done; attempt++) {
    if (signal?.aborted) return;
    try {
      // G4: 合并可选 Bearer; 重连时附 Last-Event-ID 触发尾态回放。默认模式无 token → 不附。
      const headers = lastEventId ? hdrs({ "Last-Event-ID": lastEventId }) : hdrs();
      const resp = await fetch("/v1/chat", { method: "POST", headers, body: buildBody(), signal });
      if (!resp.ok) {
        if (!RETRYABLE(resp.status)) throw new Error(`${resp.status} ${resp.statusText}`);
        throw new Error(`retryable ${resp.status}`);
      }
      if (!resp.body) throw new Error("无响应流");
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done: d, value } = await reader.read();
        if (d) break;
        buf += dec.decode(value, { stream: true });
        const { events, rest, lastId } = parseSSE(buf);
        buf = rest;
        if (lastId != null) lastEventId = lastId;
        for (const e of events) {
          handle(e as StreamEvent);
          if (done) break;
        }
        if (done) break;
      }
      if (done) return;
      throw new Error("stream ended without done");   // 自然结束无 done → 视为中断重试
    } catch (err) {
      if (signal?.aborted) throw err;                  // abort 优先
      if (done) return;
      if (attempt === 3) throw err;                    // 收敛: ≤3 次, 防重复生成/计费
      onEvent({ type: "reconnecting", attempt: attempt + 1 } as any);
      await new Promise((r) => setTimeout(r, 200 * 2 ** attempt));  // 200/400/800ms
    }
  }
}
