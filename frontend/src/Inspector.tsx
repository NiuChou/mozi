import { useEffect, useState } from "react";
import { api } from "./api";
import type { Hit, RoutingMeta, SkillItem, VaultDoc } from "./types";

type Tab = "route" | "ground" | "vault" | "kg" | "skills" | "telemetry";

interface Props {
  routing: RoutingMeta | null;
  hits: Hit[];
  injected: boolean;
  refreshKey: number; // 触发 Vault/KG/遥测 刷新
}

export default function Inspector({ routing, hits, injected, refreshKey }: Props) {
  const [tab, setTab] = useState<Tab>("route");
  return (
    <div className="inspector">
      <div className="tabs" role="tablist" aria-label="检查器面板">
        {([
          ["route", "路由"], ["ground", "接地"], ["vault", "Vault"],
          ["kg", "KG"], ["skills", "Skills"], ["telemetry", "遥测"],
        ] as [Tab, string][]).map(([k, label]) => (
          <button key={k} role="tab" aria-selected={tab === k} className={`tab ${tab === k ? "active" : ""}`}
            onClick={() => setTab(k)}>{label}</button>
        ))}
      </div>
      {tab === "route" && <RoutePane routing={routing} />}
      {tab === "ground" && <GroundPane hits={hits} injected={injected} />}
      {tab === "vault" && <VaultPane refreshKey={refreshKey} />}
      {tab === "kg" && <KGPane refreshKey={refreshKey} />}
      {tab === "skills" && <SkillsPane />}
      {tab === "telemetry" && <TelemetryPane refreshKey={refreshKey} />}
    </div>
  );
}

function RoutePane({ routing }: { routing: RoutingMeta | null }) {
  if (!routing) return <div className="pane"><p className="muted">发送消息后，这里显示 UMA 路由决策（Agent 控制室）。</p></div>;
  const scores = Object.entries(routing.scores).sort((a, b) => b[1] - a[1]);
  const max = scores.length ? Math.max(...scores.map(([, v]) => v)) : 1;
  return (
    <div className="pane">
      <h3>UMA 路由决策</h3>
      <div className="kv"><span>选中模型</span><span><b>{routing.chosen_model}</b></span></div>
      <div className="kv"><span>策略</span><span>{routing.strategy}</span></div>
      <div className="kv"><span>任务类型</span><span>{routing.task_type}</span></div>
      <div className="kv"><span>隐私级</span><span>{routing.privacy_tier}</span></div>
      <div className="kv"><span>降级</span><span>{routing.fallback_used ? "是 ⚠" : "否"}</span></div>
      <p className="muted" style={{ marginTop: 6 }}>{routing.reason}</p>
      <h3 style={{ marginTop: 14 }}>降级链</h3>
      <div>{routing.fallback_chain.map((m, i) => <span key={m} className="chip">{i + 1}. {m}</span>)}</div>
      {scores.length > 0 && (
        <>
          <h3 style={{ marginTop: 14 }}>候选打分</h3>
          {scores.map(([m, v]) => (
            <div key={m} style={{ marginBottom: 6 }}>
              <div className="kv" style={{ border: "none" }}><span>{m}</span><span>{v.toFixed(2)}</span></div>
              <div className="score-bar"><div style={{ width: `${Math.max(2, (v / max) * 100)}%` }} /></div>
            </div>
          ))}
        </>
      )}
    </div>
  );
}

function GroundPane({ hits, injected }: { hits: Hit[]; injected: boolean }) {
  return (
    <div className="pane">
      <h3>接地引用 {injected ? <span className="chip green">已注入</span> : <span className="chip">未注入</span>}</h3>
      {hits.length === 0 && <p className="muted">本轮无知识库命中。先在 Vault 归档一些笔记。</p>}
      {hits.map((h) => (
        <div className="card" key={h.chunk_id}>
          <div className="t">{h.title} <span className="muted">· {h.score.toFixed(3)}</span></div>
          <div>{h.text}</div>
          <div className="src">出处 {h.provenance} · 路 {h.routes.join("+")}</div>
        </div>
      ))}
    </div>
  );
}

function VaultPane({ refreshKey }: { refreshKey: number }) {
  const [docs, setDocs] = useState<VaultDoc[]>([]);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const load = () => api.documents().then((d) => setDocs(d.documents)).catch(() => {});
  useEffect(() => { load(); }, [refreshKey]);
  const archive = async () => {
    if (!content.trim()) return;
    await api.archive(title || "未命名笔记", content);
    setTitle(""); setContent(""); load();
  };
  return (
    <div className="pane">
      <h3>归档笔记 → Vault</h3>
      <input className="mini-input" placeholder="标题" aria-label="笔记标题" value={title} onChange={(e) => setTitle(e.target.value)} />
      <textarea className="mini-input" placeholder="正文（会自动分块/向量化/抽 KG）" rows={3} aria-label="笔记正文"
        value={content} onChange={(e) => setContent(e.target.value)} />
      <button className="mini-btn" onClick={archive}>归档</button>
      <h3 style={{ marginTop: 14 }}>知识库文档 ({docs.length})</h3>
      {docs.length === 0 && <p className="muted">空。对话会自动归档到这里。</p>}
      {docs.map((d) => (
        <div className="card" key={d.doc_id}>
          <div className="t">{d.title}</div>
          <div className="src">{d.type} · {d.chunk_count} 块 · {d.storage_mode}</div>
        </div>
      ))}
    </div>
  );
}

function KGPane({ refreshKey }: { refreshKey: number }) {
  // G3: 全图 dump 与 N-hop 子图并存。entity 为空 → 全图 (/v1/kg/graph);
  //     输入实体 → 以该实体为中心的有界 N-hop 子图 (/v1/kg/query)。
  const [g, setG] = useState<{ nodes: any[]; edges: any[] }>({ nodes: [], edges: [] });
  const [entity, setEntity] = useState("");      // 已应用的查询实体 ("" = 全图)
  const [draft, setDraft] = useState("");        // 输入框草稿 (点查询/回车才生效)
  const [hops, setHops] = useState(1);
  const [busy, setBusy] = useState(false);

  // refreshKey / 已应用实体 / hops 变化时重新拉取; 实体非空走 N-hop, 否则全图。
  useEffect(() => {
    let cancelled = false;
    setBusy(true);
    const e = entity.trim();
    const p = e ? api.kgQuery(e, hops) : api.kgGraph();
    p.then((d) => { if (!cancelled) setG(d); })
      .catch(() => { if (!cancelled) setG({ nodes: [], edges: [] }); })
      .finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; };
  }, [refreshKey, entity, hops]);

  const runQuery = () => setEntity(draft.trim());           // 应用草稿 → 触发子图查询
  const reset = () => { setDraft(""); setEntity(""); };      // 清空 → 回到全图

  const scoped = entity.trim().length > 0;
  return (
    <div className="pane">
      <h3>Mozi-KG ({g.nodes.length} 实体 / {g.edges.length} 关系)</h3>
      {/* G3: N-hop 实体查询控件 — 实体 + 跳数 + 查询/全图 */}
      <input
        className="mini-input"
        placeholder="实体名 (留空看全图)"
        aria-label="KG 查询实体"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") runQuery(); }}
      />
      <div className="kv" style={{ border: "none", padding: 0, marginBottom: 6 }}>
        <label htmlFor="kg-hops">跳数</label>
        <select id="kg-hops" className="mini-input" style={{ width: "auto", marginBottom: 0 }}
          value={hops} onChange={(e) => setHops(Number(e.target.value))}>
          <option value={1}>1 跳</option>
          <option value={2}>2 跳</option>
          <option value={3}>3 跳</option>
        </select>
      </div>
      <button className="mini-btn" onClick={runQuery} disabled={busy}>
        {busy ? "查询中…" : "查询子图"}
      </button>
      {scoped && (
        <button className="mini-btn" style={{ marginLeft: 6 }} onClick={reset} disabled={busy}>
          全图
        </button>
      )}
      <h3 style={{ marginTop: 14 }}>
        {scoped ? <>「{entity.trim()}」{hops} 跳子图</> : "全图三元组"}
        {scoped && <span className="chip" style={{ marginLeft: 6 }}>N-hop</span>}
      </h3>
      {g.edges.length === 0 && (
        <p className="muted">
          {scoped
            ? "该实体无 N-hop 关系。换个实体或加大跳数。"
            : "暂无三元组。归档含「A 是 B」「A 使用 B」的文本会抽取关系。"}
        </p>
      )}
      {g.edges.map((e, i) => (
        <div className="edge" key={i}>
          <b>{e.subject}</b><span className="pred">{e.predicate}</span><b>{e.object}</b>
          <span className="muted"> · {Number(e.confidence).toFixed(2)}</span>
        </div>
      ))}
    </div>
  );
}

function SkillsPane() {
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<any>(null);
  const [input, setInput] = useState("墨子是本地优先应用，使用 SQLite 存储。");
  const load = () => api.skills().then((d) => setSkills(d.skills)).catch(() => {});
  useEffect(() => { load(); }, []);
  const discover = async () => { setBusy(true); await api.discoverSkills(); await load(); setBusy(false); };
  const invoke = async (id: string) => { setBusy(true); setResult(await api.invokeSkill(id, input)); setBusy(false); };
  return (
    <div className="pane">
      <h3>Skill 兼容层</h3>
      <button className="mini-btn" onClick={discover} disabled={busy}>{busy ? "扫描中…" : "发现 skill (.claude/.codex/.mozi)"}</button>
      <input className="mini-input" style={{ marginTop: 8 }} aria-label="Skill 调用输入" value={input} onChange={(e) => setInput(e.target.value)} placeholder="调用输入" />
      <h3 style={{ marginTop: 10 }}>已装载 ({skills.length})</h3>
      {skills.length === 0 && <p className="muted">点上方「发现」扫描本机 skill。</p>}
      {skills.slice(0, 20).map((s) => (
        <div className="card" key={s.skill_id}>
          <div className="t">{s.name} <span className={`chip ${s.tier === "A" ? "green" : "amber"}`}>Tier {s.tier}</span></div>
          <div className="src">{s.source} · scan {s.scan_status} ·
            {Object.entries(s.capability).filter(([, v]) => v).map(([k]) => " " + k).join("")}</div>
          <button className="mini-btn" style={{ marginTop: 6 }} onClick={() => invoke(s.skill_id)} disabled={busy}>调用</button>
        </div>
      ))}
      {result && (
        <div className="card" style={{ borderColor: "var(--blue)" }}>
          <div className="t">
            产物 · {result.chosen_model} ({result.strategy})
            {result.run_id && (
              <span className={`chip ${result.status === "ok" ? "green" : "amber"}`} style={{ marginLeft: 6 }}>
                agentic · {result.steps} 步{result.status !== "ok" ? ` · ${result.status}` : ""}
              </span>
            )}
          </div>
          {(result.tools_used?.length ?? 0) > 0 && (
            <div className="src" style={{ marginTop: 4 }}>
              调用工具：{result.tools_used.map((t: string) => <span key={t} className="chip">{t}</span>)}
            </div>
          )}
          <div style={{ whiteSpace: "pre-wrap", marginTop: 4 }}>{result.output}</div>
        </div>
      )}
    </div>
  );
}

function TelemetryPane({ refreshKey }: { refreshKey: number }) {
  const [evts, setEvts] = useState<any[]>([]);
  useEffect(() => { api.events(30).then((d) => setEvts(d.events)).catch(() => {}); }, [refreshKey]);
  return (
    <div className="pane">
      <h3>遥测事件流 (PostHog 雏形)</h3>
      {evts.map((e, i) => (
        <div className="evt" key={i}><b>{e.event}</b> <span className="muted">{e.ts}</span>
          <div className="muted">{JSON.stringify(e.props)}</div></div>
      ))}
    </div>
  );
}
