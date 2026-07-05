// 与后端 §7 契约对应的类型

export type Role = "user" | "assistant" | "system";

export interface ChatMsg {
  id: string;
  role: Role;
  content: string;
  // 推理模型 (doubao-seed-evolving 等) 的思维链文本; 与 content 分流, 供「思考过程」折叠展示。
  reasoning?: string;
  model?: string;
  streaming?: boolean;
  error?: boolean;
  // G1: per-message 元数据快照 (重开历史会话回填路由/接地/用量, 后端 GET messages 已返回)。
  routing?: RoutingMeta | null;
  hits?: Hit[];
  usage?: UsageEvent | null;
  injected?: boolean;
}

export interface RoutingMeta {
  type: "routing_metadata";
  chosen_model: string;
  strategy: string;
  fallback_used: boolean;
  fallback_chain: string[];
  privacy_tier: string;
  task_type: string;
  reason: string;
  scores: Record<string, number>;
}

export interface Hit {
  chunk_id: string;
  doc_id: string;
  title: string;
  text: string;
  score: number;
  provenance: string;
  routes: string[];
}

export interface RetrievalEvent {
  type: "retrieval";
  injected: boolean;
  hits: Hit[];
  latency_ms: number;
}

export interface UsageEvent {
  type: "usage";
  prompt_tokens: number;
  completion_tokens: number;
  cost_cny: number;
  model: string;
  fallback_used: boolean;
}

export interface VaultArchiveEvent {
  type: "vault_archive";
  doc_id: string;
  title: string;
  chunks: number;
  triples: number;
}

// G2: GET /v1/quota 返回体。前端用于展示配额/降级状态。
export interface QuotaState {
  plan_code: string;
  token_budget: number;
  tokens_used: number;
  remaining: number;
  rate_multiplier: number;
  period: string;
  over_hard_cap: boolean;
}

export interface ModelSpec {
  id: string;
  provider: string;
  context_window: number;
  strengths: string[];
  domestic: boolean;
  price_in: number;
  price_out: number;
}

export interface SkillItem {
  skill_id: string;
  name: string;
  source: string;
  tier: string;
  version: string;
  scan_status: string;
  auto_invoke: boolean;
  capability: Record<string, boolean>;
  allowed_tools: string[];
}

// /v1/skills/invoke 返回体。run_id 非空 = 走了 agentic 工具循环 (否则旧静态路径)。
export interface SkillInvokeResult {
  skill_id: string;
  name: string;
  tier: string;
  chosen_model: string;
  strategy: string;
  capability: Record<string, boolean>;
  tools_used: string[];
  status: string;            // ok | blocked | error
  output: string;
  cost_cny: number;
  metered: boolean;
  run_id: string | null;     // 静态路径为 null
  steps: number;             // 循环步数 (静态路径 0)
}

export interface SessionItem {
  session_id: string;
  title: string;
  model_policy: string;
  archived?: number;
  created_at: string;
}

export interface VaultDoc {
  doc_id: string;
  title: string;
  type: string;
  storage_mode: string;
  chunk_count: number;
  updated_at: string;
}

export type StreamEvent =
  | RoutingMeta
  | RetrievalEvent
  | UsageEvent
  | VaultArchiveEvent
  | { type: "delta"; text: string }
  | { type: "reasoning"; text: string }
  | { type: "session"; session_id: string }
  | { type: "fallback"; from_model: string; to_model: string }
  | { type: "stream_reset"; reason?: string }
  | { type: "error"; detail: string; fallback_chain?: string[] }
  | { type: "done"; message_id: string; session_id: string }
  // G2: 配额降级 — 后端强制改用降级模型 (越限/限流), 前端可提示。
  | { type: "quota_degrade"; forced_model: string; reason: string }
  // G5: 断线退避重连中, 供 App 显示重连提示。
  | { type: "reconnecting"; attempt: number }
  | { type: string; [k: string]: unknown };
