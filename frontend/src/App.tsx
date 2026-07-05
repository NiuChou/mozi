import { useEffect, useRef, useState } from "react";
import { api, streamChat } from "./api";
import Inspector from "./Inspector";
import Seal from "./Seal";
import type { ChatMsg, Hit, ModelSpec, QuotaState, RoutingMeta, SessionItem, UsageEvent } from "./types";

const POLICIES = ["auto", "balanced", "quality", "economy", "speed", "code", "long_context"];
const PRIVACY = [["local_first", "本地优先"], ["cloud", "云端"], ["sovereign", "信创主权"]];

type Theme = "light" | "dark";

// 内联 SVG 图标 (零依赖, 替代 emoji —— 矢量随主题着色)
const SunIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
  </svg>
);
const MoonIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
  </svg>
);
// 警示三角 (替代 ⚠ emoji)
const WarnIcon = () => (
  <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={{ flex: "none", verticalAlign: "-2px" }}>
    <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
    <path d="M12 9v4M12 17h.01" />
  </svg>
);

// ── 自研轻量 Markdown 渲染 (零依赖, 安全转义) ──────────────────────────────
function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// 行内: 先整体转义, 再把 `code` / **bold** 还原成安全标签 (输入已转义, 无注入风险)。
function renderInline(text: string): string {
  let out = escapeHtml(text);
  out = out.replace(/`([^`]+)`/g, (_m, c) => `<code class="inline">${c}</code>`);
  out = out.replace(/\*\*([^*]+)\*\*/g, (_m, c) => `<strong>${c}</strong>`);
  return out;
}

interface MdNode {
  kind: "code" | "h" | "ul" | "ol" | "p";
  level?: number;
  lang?: string;
  text?: string;
  items?: string[];
}

function parseMarkdown(src: string): MdNode[] {
  const lines = src.replace(/\r\n/g, "\n").split("\n");
  const nodes: MdNode[] = [];
  let i = 0;
  let para: string[] = [];
  const flushPara = () => {
    if (para.length) { nodes.push({ kind: "p", text: para.join("\n") }); para = []; }
  };
  while (i < lines.length) {
    const line = lines[i];
    // 围栏代码块
    const fence = line.match(/^```(\w*)\s*$/);
    if (fence) {
      flushPara();
      const lang = fence[1] || "";
      const buf: string[] = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) { buf.push(lines[i]); i++; }
      i++; // 跳过结束 ```
      nodes.push({ kind: "code", lang, text: buf.join("\n") });
      continue;
    }
    // 标题
    const h = line.match(/^(#{1,3})\s+(.*)$/);
    if (h) { flushPara(); nodes.push({ kind: "h", level: h[1].length, text: h[2] }); i++; continue; }
    // 无序列表
    if (/^\s*[-*+]\s+/.test(line)) {
      flushPara();
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*+]\s+/, "")); i++;
      }
      nodes.push({ kind: "ul", items });
      continue;
    }
    // 有序列表
    if (/^\s*\d+\.\s+/.test(line)) {
      flushPara();
      const items: string[] = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s+/, "")); i++;
      }
      nodes.push({ kind: "ol", items });
      continue;
    }
    // 空行 = 段落分隔
    if (line.trim() === "") { flushPara(); i++; continue; }
    para.push(line); i++;
  }
  flushPara();
  return nodes;
}

function CodeBlock({ code }: { code: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try { await navigator.clipboard.writeText(code); setCopied(true); setTimeout(() => setCopied(false), 1200); }
    catch { /* 剪贴板不可用时静默 */ }
  };
  return (
    <div className="codeblock">
      <button type="button" className="copy-code" onClick={copy} aria-label="复制代码">
        {copied ? "已复制" : "复制"}
      </button>
      <pre><code>{code}</code></pre>
    </div>
  );
}

function Markdown({ text }: { text: string }) {
  const nodes = parseMarkdown(text);
  return (
    <div className="md">
      {nodes.map((n, idx) => {
        if (n.kind === "code") return <CodeBlock key={idx} code={n.text ?? ""} />;
        if (n.kind === "h") {
          const H = (`h${n.level ?? 3}`) as "h1" | "h2" | "h3";
          return <H key={idx} dangerouslySetInnerHTML={{ __html: renderInline(n.text ?? "") }} />;
        }
        if (n.kind === "ul") {
          return (
            <ul key={idx}>
              {(n.items ?? []).map((it, j) => <li key={j} dangerouslySetInnerHTML={{ __html: renderInline(it) }} />)}
            </ul>
          );
        }
        if (n.kind === "ol") {
          return (
            <ol key={idx}>
              {(n.items ?? []).map((it, j) => <li key={j} dangerouslySetInnerHTML={{ __html: renderInline(it) }} />)}
            </ol>
          );
        }
        return <p key={idx} dangerouslySetInnerHTML={{ __html: renderInline(n.text ?? "") }} />;
      })}
    </div>
  );
}
// ──────────────────────────────────────────────────────────────────────────

export default function App() {
  const [health, setHealth] = useState<{ active_providers: string[]; local_first: boolean } | null>(null);
  const [models, setModels] = useState<ModelSpec[]>([]);
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [activeSession, setActiveSession] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [usage, setUsage] = useState<{ tokens_used: number; requests: number; cost_cny: number } | null>(null);
  // G2: 配额状态 (预算/已用/剩余/越硬上限)。free_local 等无预算计划 token_budget 可为 null → 显示"无上限"。
  const [quota, setQuota] = useState<QuotaState | null>(null);

  const [policy, setPolicy] = useState("auto");
  const [privacyTier, setPrivacyTier] = useState("local_first");
  const [modelOverride, setModelOverride] = useState("");
  const [injectContext, setInjectContext] = useState(true);

  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [routing, setRouting] = useState<RoutingMeta | null>(null);
  const [hits, setHits] = useState<Hit[]>([]);
  const [injected, setInjected] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  // G1: 选中的历史 assistant 气泡 id。选中后 Inspector 喂该气泡快照; null 时跟随实时。
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // G5: 当前在途气泡的重连次数 (>0 显示 "重连中 (第 N 次)…"), 0/null 清除。本地 UI 态, 不入 ChatMsg 契约。
  const [reconnectAttempt, setReconnectAttempt] = useState<number | null>(null);
  // G2: 配额触顶被强制降级到的模型 (随本轮在途气泡), null 表示未降级。本地 UI 态, 不入 ChatMsg 契约。
  const [degradedModel, setDegradedModel] = useState<string | null>(null);
  // 会话管理: 打开操作菜单的会话 id; 重命名中的会话 id + 临时标题; 归档视图开关 + 已归档列表。
  const [menuOpenId, setMenuOpenId] = useState<string | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const [archivedSessions, setArchivedSessions] = useState<SessionItem[]>([]);
  // 主题 (宣纸/墨夜)。index.html 已防闪烁预置, 此处仅作切换 + 持久化。
  const [theme, setTheme] = useState<Theme>(() => {
    const t = typeof localStorage !== "undefined" ? localStorage.getItem("mozi-theme") : null;
    return t === "dark" ? "dark" : "light";
  });
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("mozi-theme", theme); } catch { /* 静默 */ }
  }, [theme]);

  const msgEnd = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  // 在途请求归属的会话标识(null 视为新会话占位)。用于切会话竞态校验。
  const streamSessionRef = useRef<string | null>(null);
  // 单调递增的会话加载令牌: 每次 openSession 自增, 异步返回后只认最新令牌, 防晚到的旧加载覆盖。
  const openTokenRef = useRef(0);

  useEffect(() => {
    api.health().then(setHealth).catch(() => {});
    api.models().then((d) => setModels(d.models)).catch(() => {});
    api.usage().then(setUsage).catch(() => {});
    api.quota().then(setQuota).catch(() => {}); // G2: 进入时取配额; 无端点/默认计划时静默
    refreshSessions();
  }, []);
  useEffect(() => { msgEnd.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  const refreshSessions = () => api.sessions().then((d) => setSessions(d.sessions)).catch(() => {});
  const refreshArchived = () => api.sessions(true).then((d) => setArchivedSessions(d.sessions)).catch(() => {});

  // 归档视图展开时拉取已归档列表 (随会话变更刷新)。
  useEffect(() => { if (showArchived) refreshArchived(); }, [showArchived, sessions]);
  // 操作菜单打开时, 点击别处即关闭。
  useEffect(() => {
    if (!menuOpenId) return;
    const close = () => setMenuOpenId(null);
    document.addEventListener("click", close);
    return () => document.removeEventListener("click", close);
  }, [menuOpenId]);

  // 重命名: 提交临时标题 → PATCH → 刷新两列表。
  const commitRename = async (sid: string) => {
    const title = renameValue.trim();
    setRenamingId(null);
    if (!title) return;
    try { await api.updateSession(sid, { title }); } catch { /* 忽略 */ }
    refreshSessions(); if (showArchived) refreshArchived();
  };

  // 归档/恢复: PATCH archived → 刷新; 若正归档当前会话则退回新对话占位。
  const archiveSession = async (sid: string, archived: boolean) => {
    setMenuOpenId(null);
    try { await api.updateSession(sid, { archived }); } catch { /* 忽略 */ }
    if (archived && activeSession === sid) newChat();
    refreshSessions(); refreshArchived();
  };

  // 硬删: 二次确认 → DELETE → 刷新; 若删当前会话则退回新对话占位。
  const removeSession = async (sid: string) => {
    setMenuOpenId(null);
    if (!window.confirm("删除该会话及其全部消息? 此操作不可恢复。")) return;
    try { await api.deleteSession(sid); } catch { /* 忽略 */ }
    if (activeSession === sid) newChat();
    refreshSessions(); if (showArchived) refreshArchived();
  };

  // 会话行: 标题(可内联重命名) + ⋮ 操作菜单 (重命名/归档·取消归档/删除)。归档/主列表复用。
  const renderSession = (s: SessionItem, isArchived: boolean) => {
    const isActive = activeSession === s.session_id;
    const isRenaming = renamingId === s.session_id;
    return (
      <div key={s.session_id} className={`sess ${isActive ? "active" : ""}`}
        role="button" tabIndex={busy ? -1 : 0} aria-disabled={busy}
        aria-current={isActive ? "true" : undefined}
        onClick={() => { if (!isRenaming) openSession(s.session_id); }}
        onKeyDown={(e) => { if (!isRenaming && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openSession(s.session_id); } }}>
        {isRenaming ? (
          <input className="sess-rename" autoFocus value={renameValue}
            onClick={(e) => e.stopPropagation()}
            onChange={(e) => setRenameValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") { e.preventDefault(); commitRename(s.session_id); }
              else if (e.key === "Escape") { e.preventDefault(); setRenamingId(null); }
            }}
            onBlur={() => commitRename(s.session_id)} />
        ) : (
          <span className="sess-title">{s.title || "未命名"}</span>
        )}
        {!isRenaming && (
          <button type="button" className="sess-kebab" aria-label="会话操作" aria-haspopup="menu"
            onClick={(e) => { e.stopPropagation(); setMenuOpenId(menuOpenId === s.session_id ? null : s.session_id); }}>⋮</button>
        )}
        {menuOpenId === s.session_id && (
          <div className="sess-menu" role="menu" onClick={(e) => e.stopPropagation()}>
            <button type="button" role="menuitem"
              onClick={() => { setRenamingId(s.session_id); setRenameValue(s.title || ""); setMenuOpenId(null); }}>重命名</button>
            <button type="button" role="menuitem"
              onClick={() => archiveSession(s.session_id, !isArchived)}>{isArchived ? "取消归档" : "归档"}</button>
            <button type="button" role="menuitem" className="danger"
              onClick={() => removeSession(s.session_id)}>删除</button>
          </div>
        )}
      </div>
    );
  };

  // 中断在途流(停止 / 切会话)。在途流归属清空, 避免遗留的 streamSessionRef 误判后续会话加载。
  const abortStream = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    streamSessionRef.current = null;
  };

  const newChat = () => {
    if (busy) return; // busy 期间禁用切换
    abortStream();
    setActiveSession(null); setMessages([]); setRouting(null); setHits([]); setInjected(false);
    setSelectedId(null);
  };

  const openSession = async (sid: string) => {
    if (busy) return; // busy 期间禁用会话切换
    abortStream();
    openTokenRef.current += 1;
    const token = openTokenRef.current;
    setActiveSession(sid);
    setRouting(null); setHits([]); setInjected(false);
    setSelectedId(null);
    try {
      const d = await api.messages(sid);
      // 异步返回后校验本次加载仍是最新意图(无更晚的切会话/发送), 避免竞态覆盖。
      if (openTokenRef.current !== token || streamSessionRef.current !== null) return;
      // G1: 后端字段名 routing_meta/usage_meta 映射为 ChatMsg.routing/usage; hits/injected 同名。
      //     缺省 → undefined (视为无快照, Inspector 退回实时)。
      setMessages(d.messages.map((m: any) => ({
        id: m.message_id, role: m.role, content: m.content_ref, model: m.model,
        routing: m.routing_meta ?? undefined,
        hits: m.hits ?? undefined,
        usage: m.usage_meta ?? undefined,
        injected: m.injected ?? undefined,
      })));
    } catch { /* 忽略加载失败 */ }
  };

  // 真正的发送/重发逻辑。text 来自输入框或重生成。
  const runChat = async (text: string) => {
    setBusy(true);
    setRouting(null); setHits([]); setInjected(false);
    setSelectedId(null); setReconnectAttempt(null); setDegradedModel(null);
    const ac = new AbortController();
    abortRef.current = ac;
    const sessionAtStart = activeSession;
    streamSessionRef.current = sessionAtStart;

    const userMsg: ChatMsg = { id: "u" + Date.now(), role: "user", content: text };
    const botId = "a" + Date.now();
    // 当前助手气泡的有效 id。G6: done 时用服务端 message_id 替换占位 botId, 后续 patch 仍命中。
    let curBotId = botId;
    setMessages((m) => [...m, userMsg, { id: botId, role: "assistant", content: "", streaming: true }]);

    // delta/状态写入前校验: 在途会话与组件当前会话仍一致才落地, 防切会话竞态。
    const sameSession = () =>
      streamSessionRef.current === sessionAtStart && abortRef.current === ac;
    const patchBot = (fn: (x: ChatMsg) => ChatMsg) => {
      if (!sameSession()) return;
      setMessages((m) => m.map((x) => (x.id === curBotId ? fn(x) : x)));
    };

    try {
      await streamChat(
        { text, sessionId: sessionAtStart, policy, privacyTier, injectContext, model: modelOverride || null },
        (e) => {
          if (e.type === "session") {
            const sid = (e as any).session_id as string;
            // 服务端分配的会话 id 回填到在途引用与状态。
            streamSessionRef.current = sid;
            if (abortRef.current === ac) setActiveSession(sid);
          } else if (e.type === "routing_metadata") {
            // 实时路由 → 既驱动实时 Inspector, 也落到本气泡供 done 后历史回看 (G1)。
            if (sameSession()) { setRouting(e as RoutingMeta); patchBot((x) => ({ ...x, routing: e as RoutingMeta })); }
          } else if (e.type === "retrieval") {
            if (sameSession()) {
              const rHits = (e as any).hits as Hit[];
              const rInj = (e as any).injected as boolean;
              setHits(rHits); setInjected(rInj);
              patchBot((x) => ({ ...x, hits: rHits, injected: rInj }));
            }
          } else if (e.type === "stream_reset") {
            // 契约B: 降级重试前清空当前助手气泡已累积正文 (含上次尝试的思维链), 再继续接后续 delta。
            patchBot((x) => ({ ...x, content: "", reasoning: "" }));
          } else if (e.type === "reconnecting") {
            // G5: 断线退避重连中 → 轻提示 "重连中 (第 N 次)…"; done/error/后续 delta 会清除。
            // 重连会带 Last-Event-ID 重发, 后端从头重放全量 delta → 先清空已累积正文, 让重放替换而非追加(否则重复/串字)。
            if (sameSession()) setReconnectAttempt((e as any).attempt as number);
            patchBot((x) => ({ ...x, content: "", reasoning: "" }));
          } else if (e.type === "quota_degrade") {
            // G2: 配额触顶被强制降级 → 标注降级到的模型, 并把气泡 model tag 切到该模型。
            const fm = (e as any).forced_model as string;
            if (sameSession()) setDegradedModel(fm);
            patchBot((x) => ({ ...x, model: fm }));
          } else if (e.type === "reasoning") {
            // 推理模型思维链分片: 累积到气泡 reasoning, 在「思考过程」区展示 (出正文前先有动静, 不再空白)。
            const t = (e as any).text as string;
            if (sameSession()) setReconnectAttempt(null);
            patchBot((x) => ({ ...x, reasoning: (x.reasoning || "") + t }));
          } else if (e.type === "delta") {
            const t = (e as any).text as string;
            // 收到正文即视为已重连成功, 清除重连提示。
            if (sameSession()) setReconnectAttempt(null);
            patchBot((x) => ({ ...x, content: x.content + t }));
          } else if (e.type === "usage") {
            // G1: 用量快照落到气泡 (usage_meta), 同时更新 model tag。
            patchBot((x) => ({ ...x, model: (e as any).model, usage: e as UsageEvent }));
          } else if (e.type === "done") {
            const mid = (e as any).message_id as string | undefined;
            // G6: 用服务端 message_id 作为稳定气泡 id, 替换客户端占位, 便于选中/历史回填精确对齐。
            patchBot((x) => ({ ...x, id: mid || x.id, streaming: false }));
            if (mid && sameSession()) {
              // 若用户此前正选中该占位气泡, 同步选中态到新 id。
              setSelectedId((s) => (s === curBotId ? mid : s));
              curBotId = mid;
            }
          } else if (e.type === "error") {
            // 全降级失败: 后端发 error 事件 (流正常关闭, 不抛异常) → 落错误气泡并停光标, 避免空泡卡死
            patchBot((x) => ({ ...x, content: "⚠ " + ((e as any).detail || "生成失败"), streaming: false, error: true }));
          }
        },
        ac.signal,
      );
    } catch (err) {
      // 主动中断(停止/切会话)不显示错误气泡。
      if (ac.signal.aborted) {
        patchBot((x) => ({ ...x, streaming: false }));
      } else {
        patchBot((x) => ({ ...x, content: "⚠ 出错: " + String(err), streaming: false, error: true }));
      }
    } finally {
      if (abortRef.current === ac) {
        abortRef.current = null;
        streamSessionRef.current = null;
        setBusy(false);
        setReconnectAttempt(null); // 本轮结束 → 清除重连提示 (降级标注随气泡 model tag 已持久, 不在此清)
        api.usage().then(setUsage).catch(() => {});
        api.quota().then(setQuota).catch(() => {}); // G2: 随用量一起刷新配额
        refreshSessions();
        setRefreshKey((k) => k + 1);
      }
    }
  };

  const send = () => {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    void runChat(text);
  };

  // 重生成: 以该助手消息前一条 user 文本重发, 并丢弃其后(含自身)的消息。
  const regenerate = (botId: string) => {
    if (busy) return;
    const idx = messages.findIndex((m) => m.id === botId);
    if (idx < 0) return;
    let userText = "";
    for (let i = idx - 1; i >= 0; i--) {
      if (messages[i].role === "user") { userText = messages[i].content; break; }
    }
    if (!userText) return;
    setMessages((m) => m.slice(0, idx - 1 >= 0 && m[idx - 1].role === "user" ? idx - 1 : idx));
    void runChat(userText);
  };

  const copyMsg = async (content: string) => {
    try { await navigator.clipboard.writeText(content); } catch { /* 静默 */ }
  };

  // G1: Inspector 喂哪份快照 —— 选中了某条历史 assistant 气泡 (且其有快照) 则喂该气泡的;
  //     否则跟随实时 (流期间默认看最新)。气泡快照字段缺省 → 退回实时, 不留空白。
  const selectedMsg = selectedId ? messages.find((m) => m.id === selectedId) : undefined;
  const insRouting = selectedMsg && selectedMsg.routing !== undefined ? selectedMsg.routing ?? null : routing;
  const insHits = selectedMsg && selectedMsg.hits !== undefined ? (selectedMsg.hits ?? []) : hits;
  const insInjected = selectedMsg && selectedMsg.injected !== undefined ? !!selectedMsg.injected : injected;

  return (
    <div className="app">
      <div className="topbar">
        <div className="brand-wrap">
          <Seal />
          <div style={{ minWidth: 0 }}>
            <div className="brand">墨子<span className="brand-en">Mozi · Chat</span></div>
          </div>
        </div>
        <div className="spacer" />
        <span className={`badge ${health?.local_first ? "ok" : ""}`}>
          {health?.local_first ? "本地优先 · 零外呼" : "云端"}
        </span>
        <span className="badge">
          模型在线: {health?.active_providers.length ? health.active_providers.join(", ") : "mock (无 key)"}
        </span>
        <button type="button" className="theme-toggle"
          aria-label={theme === "light" ? "切换到墨夜模式" : "切换到宣纸模式"}
          title={theme === "light" ? "墨夜" : "宣纸"}
          onClick={() => setTheme((t) => (t === "light" ? "dark" : "light"))}>
          {theme === "light" ? <MoonIcon /> : <SunIcon />}
        </button>
      </div>

      <div className="main">
        {/* 左: 会话 + 用量 */}
        <div className="sidebar">
          <button className="newbtn" onClick={newChat} disabled={busy}>+ 新对话</button>
          <div className="side-h">会话</div>
          {sessions.length === 0 && <p className="muted" style={{ padding: 4 }}>暂无</p>}
          {sessions.map((s) => renderSession(s, false))}
          {/* 归档区: 折叠开关 + 已归档会话 (可恢复/删除) */}
          {(archivedSessions.length > 0 || showArchived) && (
            <button type="button" className="arch-toggle"
              onClick={() => setShowArchived((v) => !v)}>
              {showArchived ? "▾" : "▸"} 已归档{archivedSessions.length ? ` (${archivedSessions.length})` : ""}
            </button>
          )}
          {showArchived && archivedSessions.map((s) => renderSession(s, true))}
          <div className="side-h">本期用量 · 可信回执</div>
          <div className="usage-card">
            <div><b>{usage?.tokens_used ?? 0}</b> tokens</div>
            <div>{usage?.requests ?? 0} 次请求 · ¥{(usage?.cost_cny ?? 0).toFixed(4)}</div>
            {/* G2: 配额条 — 有 token_budget 才显示预算/剩余; null (如 free_local) 显示"无上限"。over_hard_cap 高亮告警。 */}
            {quota && (
              <div className={`quota ${quota.over_hard_cap ? "over" : ""}`} style={{ marginTop: 6 }}>
                {quota.token_budget == null
                  ? <div className="muted">预算: 无上限</div>
                  : <>
                      <div>预算 {quota.token_budget} · 剩余 <b>{quota.remaining}</b></div>
                      <div className="quota-bar">
                        <div style={{ width: `${Math.min(100, Math.max(0, quota.token_budget ? (quota.tokens_used / quota.token_budget) * 100 : 0))}%` }} />
                      </div>
                      {quota.over_hard_cap && <div className="quota-warn"><WarnIcon /> 已越硬上限, 后续请求将被降级</div>}
                    </>}
              </div>
            )}
          </div>
        </div>

        {/* 中: 对话 */}
        <div className="center">
          <div className="messages">
            {messages.length === 0 && (
              <div className="empty">
                <h2>开始与墨子对话</h2>
                <p>UMA 网关自动选模型；开启「智能上下文注入」会先检索你的 Vault 知识库并标注来源。</p>
              </div>
            )}
            {messages.map((m) => {
              // G1: 仅 assistant 气泡可选中查看其路由/接地/用量快照 (含历史快照 or 实时刚落地的)。
              const selectable = m.role === "assistant" && !m.error;
              const isSelected = selectable && selectedId === m.id;
              return (
              <div key={m.id}
                className={`msg ${m.error ? "error" : m.role}${isSelected ? " selected" : ""}`}
                aria-current={isSelected ? "true" : undefined}
                onClick={selectable ? () => setSelectedId((s) => (s === m.id ? null : m.id)) : undefined}
                style={selectable ? { cursor: "pointer" } : undefined}>
                <div className="who">{m.role === "user" ? "你" : "墨子"}
                  {m.model && <span className="model-tag">{m.model}</span>}
                  {/* G2: 本轮被强制降级时, 在在途气泡标注 */}
                  {m.streaming && degradedModel && <span className="model-tag warn">配额触顶 · 已降级 {degradedModel}</span>}
                </div>
                <div className="bubble">
                  {/* 推理模型思维链: 出正文前先展示『思考中…』, 不再空白卡死; 有正文后折叠为『思考过程』可回看。 */}
                  {m.role === "assistant" && m.reasoning && (
                    <details className="reasoning" open={!!m.streaming && !m.content}>
                      <summary>{m.streaming && !m.content ? "思考中…" : "思考过程"}</summary>
                      <div className="reasoning-body">{m.reasoning}</div>
                    </details>
                  )}
                  {m.role === "assistant" && !m.error
                    ? <Markdown text={m.content} />
                    : m.content}
                  {/* G5: 在途气泡显示重连提示 */}
                  {m.streaming && reconnectAttempt != null && (
                    <span className="reconnecting">重连中 (第 {reconnectAttempt} 次)…</span>
                  )}
                  {m.streaming && <span className="cursor">&nbsp;</span>}
                </div>
                {m.role === "assistant" && !m.streaming && m.content && (
                  <div className="msg-actions">
                    {/* stopPropagation: 点按钮不触发气泡选中切换 */}
                    <button type="button" className="msg-action" onClick={(e) => { e.stopPropagation(); copyMsg(m.content); }}>复制</button>
                    <button type="button" className="msg-action" onClick={(e) => { e.stopPropagation(); regenerate(m.id); }} disabled={busy}>重生成</button>
                  </div>
                )}
              </div>
              );
            })}
            <div ref={msgEnd} />
          </div>

          <div className="composer">
            <div className="controls">
              <label htmlFor="sel-policy">策略</label>
              <select id="sel-policy" value={policy} onChange={(e) => setPolicy(e.target.value)}>
                {POLICIES.map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
              <label htmlFor="sel-privacy">隐私</label>
              <select id="sel-privacy" value={privacyTier} onChange={(e) => setPrivacyTier(e.target.value)}>
                {PRIVACY.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
              <label htmlFor="sel-model">模型</label>
              <select id="sel-model" value={modelOverride} onChange={(e) => setModelOverride(e.target.value)}>
                <option value="">自动 (UMA)</option>
                {models
                  .filter((m) => health?.active_providers.includes(m.provider))
                  .map((m) => <option key={m.id} value={m.id}>{m.id}</option>)}
              </select>
              <label className="toggle">
                <input type="checkbox" checked={injectContext}
                  onChange={(e) => setInjectContext(e.target.checked)} /> 智能上下文注入
              </label>
            </div>
            <div className="row">
              <textarea value={input} placeholder="描述你想要的结果…（Enter 发送，Shift+Enter 换行）"
                aria-label="对话输入框"
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
              {busy
                ? <button type="button" className="sendbtn" onClick={abortStream} aria-label="停止生成">停止</button>
                : <button type="button" className="sendbtn" onClick={send}>发送</button>}
            </div>
          </div>
        </div>

        {/* 右: 检查器 (Agent 控制室)。G1: 选中历史气泡时喂其快照, 否则喂实时。签名不变。 */}
        <Inspector routing={insRouting} hits={insHits} injected={insInjected} refreshKey={refreshKey} />
      </div>
    </div>
  );
}
