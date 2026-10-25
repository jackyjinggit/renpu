# -*- coding: utf-8 -*-
"""仁谱 · 关系图谱渲染（纯原生 SVG，零第三方依赖，零联网）

设计约束（都是为了「别人能直接测」）：
  · 不引 graphviz / networkx / d3 —— 装了就能跑，别人 clone 完不用装一堆东西
  · 输出单个自包含 HTML 文件 —— 可以直接发给别人看
  · 力导向布局用纯 JS 跑在浏览器端 —— 数据不外传，本地看
  · 节点标签默认脱敏为昵称 —— 截图分享不泄 PII
"""
import html
import json
import math
import time
from pathlib import Path

import core

# ── 调色（跟产品基调一致：深色底 + 珊瑚/青强调） ──
BG = "#0f1115"
PANEL = "#171a21"
TEXT = "#e8eaf0"
DIM = "#8b93a7"
ACCENT = "#ff7a6b"
ACCENT2 = "#4ecdc4"
EDGE = "#39404f"
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif"

# 社区配色：同色=同一个圈子。顺序按「亮→暗」排，深底上都能分辨。
# 前两位留给主强调色，后续依次轮转。
COMMUNITY_COLORS = [
    "#ff7a6b",  # 珊瑚
    "#4ecdc4",  # 青
    "#ffd166",  # 金
    "#a78bfa",  # 紫
    "#7dd3fc",  # 天蓝
    "#f472b6",  # 粉
    "#86efac",  # 浅绿
    "#fb923c",  # 橙
    "#c4b5fd",  # 淡紫
    "#67e8f9",  # 亮青
]


def community_color(cid):
    if cid is None:
        return DIM
    return COMMUNITY_COLORS[int(cid) % len(COMMUNITY_COLORS)]


def _esc(s):
    return html.escape(str(s if s is not None else ""))


def collect_graph(con, limit=60, min_weight=1.0, tier=None, focus=None, share=False):
    """从库里取一张关系图：节点 = 人，边 = 共现强度。

    选人策略（关键：真库 21823 人，画前 60 个会碎成散点）：
      focus='connected'  → 按「有多少条关系边」排序，只取真正有关系的人
      focus='active'     → 按近期活跃度（最近有来往）排序
      focus='tier'       → 按分层
      默认（有 tier 时用 tier，否则 connected）

    share=True → 分享模式：群名脱敏、消息数隐藏（只留人名+分层），
                  避免把真实群名带进可分享的文件里。
    """
    # 1. 先算出每个人的关系边数量（这是「有没有价值上图」的唯一硬指标）
    #    edge 表用 src/dst 两列表示一条边，两个方向各算一次
    deg = {}
    for r in con.execute("SELECT src, dst FROM edge").fetchall():
        deg[r[0]] = deg.get(r[0], 0) + 1
        deg[r[1]] = deg.get(r[1], 0) + 1

    where, vals = [], []
    if tier:
        where.append("person.tier = ?")
        vals.append(tier)
    if focus == "active":
        where.append("person.last_active > ?")
        vals.append(int(time.time()) - 30 * 86400)

    order = "person.msg_total DESC"

    sql = ("SELECT person.person_key AS person_key, person.display_name AS display_name, "
           "person.tier AS tier, person.circle AS circle, "
           "person.role AS role, person.msg_total AS msg_total, person.msg_d30 AS msg_d30 "
           "FROM person ")
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    # 多取一些，Python 侧再按关系度排序（SQL 侧算 degree 要 join，太慢）
    fetch = max(limit * 6, 200) if focus != "tier" else limit
    sql += f"ORDER BY {order} LIMIT ?"
    rows = [dict(r) for r in con.execute(sql, vals + [fetch]).fetchall()]
    for r in rows:
        r["degree"] = deg.get(r["person_key"], 0)
    if focus != "tier":
        rows.sort(key=lambda r: (-r["degree"], -(r.get("msg_total") or 0)))
    people = rows[:limit]

    # 2. 大库里只画「有关系的人」—— 否则 21823 人里挑前 50 个会碎成散点。
    #    小库（demo 10 人）不过滤，否则会让人以为数据丢了。
    if len(people) > 20:
        linked = [p for p in people if p.get("degree", 0) > 0]
        if len(linked) >= 3:
            people = linked
    if not people:
        return {"nodes": [], "links": [], "rooms": []}

    if share:
        # 分享模式：去掉群名、圈子、角色、消息数——只留「谁」和「多紧密」
        for p in people:
            p["circle"] = None
            p["role"] = None
            p["msg_total"] = None

    keys = [p["person_key"] for p in people]
    ph = ",".join("?" * len(keys))
    links = [dict(r) for r in con.execute(
        f"SELECT src, dst, weight, kind, rooms FROM edge "
        f"WHERE src IN ({ph}) AND dst IN ({ph}) AND weight >= ? "
        f"ORDER BY weight DESC", keys + keys + [min_weight]).fetchall()]
    # 去重（A→B 与 B→A 只留一条）
    seen, uniq = set(), []
    for l in links:
        pair = tuple(sorted((l["src"], l["dst"])))
        if pair in seen:
            continue
        seen.add(pair)
        uniq.append(l)
    # 只保留两端都在节点集里的边
    kset = set(keys)
    uniq = [l for l in uniq if l["src"] in kset and l["dst"] in kset]

    # 3. 社区检测：群共现天然形成社区，一眼看出「谁跟谁是一伙的」
    import community as comm
    room_members = comm.merge_rooms_from_links(uniq)
    comm_by_room = comm.community_of_rooms(room_members)
    comm_by_lp = comm.detect_communities(uniq)
    # 群足够多时用「按群归属」（更符合直觉）；否则用标签传播（群少时更稳）
    comm_info = comm_by_room if len(room_members) >= 3 else comm_by_lp
    for p in people:
        info = comm_info.get(p["person_key"])
        if info:
            p["community"] = info["community"]
            p["community_size"] = info["size"]
        else:
            p["community"] = None
            p["community_size"] = 0
    # 边也带上共同群数量，用于边标签
    for l in uniq:
        rooms_raw = l.get("rooms")
        cnt = 0
        if rooms_raw:
            try:
                cnt = len(json.loads(rooms_raw)) if isinstance(rooms_raw, str) else len(rooms_raw)
            except (ValueError, TypeError):
                cnt = 0
        l["shared_rooms"] = cnt

    # 房间（作为图上的分组提示）
    rooms = [dict(r) for r in con.execute(
        f"SELECT room.room_key AS room_key, room.name AS name, "
        f"       room.member_cnt AS member_cnt, COUNT(*) AS mine "
        f"FROM room_member rm JOIN room ON room.room_key = rm.room_key "
        f"WHERE rm.person_key IN ({ph}) "
        f"GROUP BY room.room_key, room.name, room.member_cnt "
        f"ORDER BY mine DESC LIMIT 12", keys).fetchall()]
    if share:
        for r in rooms:
            r["name"] = "群（已脱敏）"
    return {"nodes": people, "links": uniq, "rooms": rooms}


def render_html(graph, title="仁谱 · 关系图谱", subtitle="", embed_data=True):
    """把图谱数据渲染成一个自包含 HTML。"""
    payload = json.dumps(graph, ensure_ascii=False, default=str)
    data_block = (f"<script id='graph-data' type='application/json'>{payload}</script>"
                  if embed_data else "")
    return _HTML_TEMPLATE.replace("__TITLE__", _esc(title)).replace(
        "__SUBTITLE__", _esc(subtitle)).replace("__DATA__", data_block)


def render_svg(graph, width=900, height=620):
    """服务端直接出静态 SVG（不需要浏览器，用于 README 嵌图 / CI 截图）。"""
    nodes = graph["nodes"]
    if not nodes:
        return "<svg xmlns='http://www.w3.org/2000/svg'></svg>"
    # 简易圆形布局：按度数排圈
    n = len(nodes)
    cx, cy, r = width / 2, height / 2, min(width, height) / 2 - 60
    pos = {}
    deg = {nd["person_key"]: 0 for nd in nodes}
    for l in graph["links"]:
        deg[l["src"]] = deg.get(l["src"], 0) + 1
        deg[l["dst"]] = deg.get(l["dst"], 0) + 1
    order = sorted(nodes, key=lambda x: -deg.get(x["person_key"], 0))
    for i, nd in enumerate(order):
        a = 2 * math.pi * i / max(n, 1) - math.pi / 2
        pos[nd["person_key"]] = (cx + r * math.cos(a), cy + r * math.sin(a))
    parts = [f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' viewBox='0 0 {width} {height}'>",
             f"<rect width='{width}' height='{height}' fill='{BG}'/>"]
    for l in graph["links"]:
        if l["src"] in pos and l["dst"] in pos:
            x1, y1 = pos[l["src"]]; x2, y2 = pos[l["dst"]]
            w = min(1 + l["weight"] * 0.8, 5)
            op = min(0.25 + l["weight"] * 0.15, 0.85)
            parts.append(f"<line x1='{x1:.1f}' y1='{y1:.1f}' x2='{x2:.1f}' y2='{y2:.1f}' "
                         f"stroke='{EDGE}' stroke-width='{w:.1f}' opacity='{op:.1f}'/>")
    for nd in nodes:
        x, y = pos[nd["person_key"]]
        d = deg.get(nd["person_key"], 0)
        rr = 6 + min(d * 2, 12)
        tier = (nd.get("tier") or "").upper()
        col = ACCENT if tier in ("AAA", "AAAA") else (ACCENT2 if tier == "AA" else DIM)
        parts.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='{rr}' fill='{col}' opacity='0.9'/>")
        parts.append(f"<text x='{x:.1f}' y='{y + rr + 13:.1f}' fill='{TEXT}' font-size='11' "
                     f"text-anchor='middle' font-family='{FONT}'>{_esc(nd['display_name'])}</text>")
    parts.append("</svg>")
    return "\n".join(parts)


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
  :root{
    --bg:#0f1115; --panel:#171a21; --text:#e8eaf0; --dim:#8b93a7;
    --accent:#ff7a6b; --accent2:#4ecdc4; --edge:#39404f; --line:#252a35;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;}
  header{padding:22px 26px 12px;border-bottom:1px solid var(--line);}
  h1{margin:0 0 6px;font-size:19px;font-weight:650;letter-spacing:.3px}
  .sub{color:var(--dim);font-size:13px}
  .wrap{display:grid;grid-template-columns:1fr 300px;gap:0;height:calc(100vh - 92px)}
  @media(max-width:900px){.wrap{grid-template-columns:1fr;height:auto}}
  #stage{position:relative;overflow:hidden}
  svg{width:100%;height:100%;display:block;cursor:grab}
  svg:active{cursor:grabbing}
  aside{background:var(--panel);border-left:1px solid var(--line);padding:18px;overflow:auto}
  @media(max-width:900px){aside{border-left:none;border-top:1px solid var(--line)}}
  h2{font-size:12px;text-transform:uppercase;letter-spacing:1.2px;color:var(--dim);
     margin:0 0 12px;font-weight:600}
  .legend{display:flex;flex-direction:column;gap:9px;margin-bottom:24px}
  .lg{display:flex;align-items:center;gap:9px;font-size:12.5px;color:var(--text)}
  .dot{width:10px;height:10px;border-radius:50%;flex:none}
  .hint{font-size:11.5px;color:var(--dim);line-height:1.7;margin-top:6px}
  .rooms{list-style:none;padding:0;margin:0}
  .rooms li{font-size:12.5px;padding:7px 0;border-bottom:1px solid var(--line);
     display:flex;justify-content:space-between;gap:10px}
  .rooms li:last-child{border-bottom:none}
  .rooms .n{color:var(--dim);font-variant-numeric:tabular-nums;flex:none}
  .rooms .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .toolbar{position:absolute;top:14px;left:16px;display:flex;gap:8px;z-index:5}
  button{background:rgba(23,26,33,.86);color:var(--text);border:1px solid var(--line);
    border-radius:7px;padding:6px 12px;font-size:12px;cursor:pointer;font-family:inherit}
  button:hover{border-color:var(--accent)}
  .tip{position:absolute;pointer-events:none;background:rgba(23,26,33,.96);
    border:1px solid var(--line);border-radius:8px;padding:9px 12px;font-size:12.5px;
    display:none;z-index:9;max-width:250px;line-height:1.6}
  .tip b{color:var(--accent)}
  .stats{display:flex;gap:20px;margin-top:12px;flex-wrap:wrap}
  .st{font-size:11.5px;color:var(--dim);letter-spacing:.3px}
  .st b{display:block;color:var(--text);font-size:20px;font-weight:600;
     font-variant-numeric:tabular-nums;margin-top:3px;line-height:1.1}
  .st b span{font-size:11.5px;color:var(--dim);font-weight:400;margin-left:4px}
  .empty{color:var(--dim);font-size:13px;padding:30px;text-align:center}
  .tipname{font-size:13.5px;font-weight:600;margin-bottom:3px}
  .tipmeta{color:var(--dim);font-size:11.5px;line-height:1.7}
  .legend-note{border-top:1px solid var(--line);padding-top:16px;margin-top:18px;
     font-size:11.5px;color:var(--dim);line-height:1.85}
  .legend-note b{color:var(--text);font-weight:500}
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <div class="sub">__SUBTITLE__</div>
  <div class="stats" id="stats"></div>
</header>
<div class="wrap">
  <div id="stage">
    <div class="toolbar">
      <button onclick="restart()">重新布局</button>
      <button onclick="toggleLabels()">显示全部标签</button>
    </div>
    <svg id="svg"></svg>
    <div class="tip" id="tip"></div>
  </div>
  <aside>
    <h2>图例</h2>
    <div class="legend">
      <div class="lg"><span class="dot" style="background:#ff7a6b"></span>圈子 1</div>
      <div class="lg"><span class="dot" style="background:#4ecdc4"></span>圈子 2</div>
      <div class="lg"><span class="dot" style="background:#ffd166"></span>圈子 3</div>
      <div class="lg"><span class="dot" style="background:#a78bfa"></span>圈子 4+</div>
    </div>
    <div class="legend-note">
      <b>节点大小</b> = 共现强度<br>
      <b>连线粗细</b> = 共同出现的群数<br>
      <b>同色</b> = 同一个圈子<br><br>
      <b>点击某人</b> → 高亮他的关系圈，淡化无关的人<br>
      <b>点击空白</b> → 取消聚焦<br><br>
      拖动平移 · 滚轮缩放 · 悬停看详情
    </div>
    <h2 style="margin-top:26px">共同出现的群</h2>
    <ul class="rooms" id="rooms"></ul>
  </aside>
</div>
__DATA__
<script>
(function(){
  const el=document.getElementById('graph-data');
  if(!el){document.getElementById('stage').innerHTML='<div class="empty">没有数据</div>';return;}
  const G=JSON.parse(el.textContent);
  const svg=document.getElementById('svg');
  const tip=document.getElementById('tip');
  const NS='http://www.w3.org/2000/svg';
  const FONT="-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif";
  let labelMode='auto';   // auto | all | none

  // 统计条
  const deg={}; G.nodes.forEach(n=>deg[n.person_key]=0);
  G.links.forEach(l=>{deg[l.src]=(deg[l.src]||0)+1;deg[l.dst]=(deg[l.dst]||0)+1;});
  const nComm = new Set(G.nodes.map(n=>n.community).filter(c=>c!==null&&c!==undefined)).size;
  const gstat=document.getElementById('stats');
  if(gstat) gstat.innerHTML=
    `<div class="st"><b>${G.nodes.length}<span>人</span></b></div>`+
    `<div class="st"><b>${G.links.length}<span>关系边</span></b></div>`+
    `<div class="st"><b>${nComm}<span>圈子</span></b></div>`+
    `<div class="st"><b>${G.rooms.length}<span>共同群</span></b></div>`;

  // 群列表
  const rl=document.getElementById('rooms');
  if(rl) rl.innerHTML=G.rooms.map(r=>
    `<li><span class="nm">${r.name||'(群)'}</span><span class="n">${r.mine||0} 人</span></li>`).join('')
      ||'<li style="color:#8b93a7">（无）</li>';

  const W=1200,H=760;
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
  const root=document.createElementNS(NS,'g');
  svg.appendChild(root);

  // 密度自适应：节点多时自动降级，避免标签糊成一团
  //  （判定必须在 nodes 定义之后 —— 之前写这里会触发 TDZ 报错）
  const DENSE = G.nodes.length>28;
  const MIN_DEG_LABEL = DENSE ? 3 : 1;   // 密集时只给关系度高的节点加标签

  // 初始位置（随机环形）
  const nodes=G.nodes.map((n,i)=>({
    d:n, x:W/2+Math.cos(i/G.nodes.length*6.28)*260, y:H/2+Math.sin(i/G.nodes.length*6.28)*260,
    vx:0, vy:0, r:6+Math.min(deg[n.person_key]*2,12)
  }));
  const idx={}; nodes.forEach((n,i)=>idx[n.d.person_key]=i);
  const links=G.links.map(l=>({s:idx[l.src],t:idx[l.dst],w:l.weight,
                               shared:l.shared_rooms!=null?l.shared_rooms:null}))
                   .filter(l=>l.s!=null&&l.t!=null);

  // 力导向（斥力随节点数增强，保证能摊开）
  const REPULSE = 1400 + nodes.length*38;
  function tick(){
    for(let i=0;i<nodes.length;i++){const a=nodes[i];
      for(let j=i+1;j<nodes.length;j++){const b=nodes[j];
        let dx=b.x-a.x,dy=b.y-a.y;let d2=dx*dx+dy*dy||1;
        if(d2<98000){const f=REPULSE/d2;const d=Math.sqrt(d2);
          const ux=dx/d,uy=dy/d;a.vx-=ux*f;a.vy-=uy*f;b.vx+=ux*f;b.vy+=uy*f;}}}
    links.forEach(l=>{const a=nodes[l.s],b=nodes[l.t];
      let dx=b.x-a.x,dy=b.y-a.y;let d=Math.sqrt(dx*dx+dy*dy)||1;
      // 目标距离随共同群数增加（关系越强靠得越近）
      const target=70+ (4-Math.min(l.w,4))*26;const f=(d-target)*0.016*Math.min(l.w,4);
      const ux=dx/d,uy=dy/d;a.vx+=ux*f;a.vy+=uy*f;b.vx-=ux*f;b.vy-=uy*f;});
    nodes.forEach(a=>{a.vx+=(W/2-a.x)*0.0022;a.vy+=(H/2-a.y)*0.0022;
      a.vx*=0.85;a.vy*=0.85;a.x+=a.vx;a.y+=a.vy;});
  }
  // 迭代次数随节点数增加（稀疏图需要更多轮才聚拢）
  const ITER = Math.min(600, 200 + nodes.length*5);
  for(let i=0;i<ITER;i++)tick();

  // 绘制
  const gE=document.createElementNS(NS,'g'),gN=document.createElementNS(NS,'g'),gL=document.createElementNS(NS,'g');
  root.appendChild(gE);root.appendChild(gL);root.appendChild(gN);
  const CC=['#ff7a6b','#4ecdc4','#ffd166','#a78bfa','#7dd3fc','#f472b6','#86efac','#fb923c','#c4b5fd','#67e8f9'];
  const ccOf=c=>(c===null||c===undefined)?'#8b93a7':CC[Number(c)%CC.length];
  // 邻接表（点击高亮用）
  const nbr={}; nodes.forEach(n=>nbr[n.d.person_key]=new Set());
  links.forEach(l=>{nbr[nodes[l.s].d.person_key].add(nodes[l.t].d.person_key);
                     nbr[nodes[l.t].d.person_key].add(nodes[l.s].d.person_key);});
  let focusKey=null;   // 当前聚焦的人

  links.forEach(l=>{const a=nodes[l.s],b=nodes[l.t];
    const ln=document.createElementNS(NS,'line');
    ln.setAttribute('stroke','#39404f');
    ln.setAttribute('stroke-width',Math.min(1+l.w*0.9,6));
    // 密集时压低连线透明度：核心区连线太多会糊成一片灰
    ln.setAttribute('opacity',DENSE?0.18:Math.min(0.28+l.w*0.16,0.9));
    gE.appendChild(ln);l.el=ln;});

  // 边标签：共同出现的群数。
  // 只给权重最高的若干条边显示，否则中心区会挤成一团「5群5群5群」。
  const gT=document.createElementNS(NS,'g');
  root.appendChild(gT);
  const labelBudget = DENSE ? 26 : 60;
  const ranked = links.slice().sort((a,b)=>b.w-a.w);
  const labelSet = new Set(ranked.slice(0,labelBudget).map(l=>l));
  links.forEach(l=>{
    if(!labelSet.has(l)) { l.lbl=null; return; }
    const t=document.createElementNS(NS,'text');
    t.setAttribute('fill','#c8cedd');t.setAttribute('font-size','9.5');
    t.setAttribute('text-anchor','middle');t.setAttribute('font-family',FONT);
    t.setAttribute('paint-order','stroke');           // 加描边，压在连线上也看得清
    t.setAttribute('stroke','#0f1115');t.setAttribute('stroke-width','3');
    t.setAttribute('stroke-linejoin','round');
    t.textContent = l.shared!=null && l.shared>0 ? `${l.shared}群` : `${Math.round(l.w)}`;
    t.style.pointerEvents='none';
    gT.appendChild(t);l.lbl=t;
  });

  nodes.forEach(n=>{
    const c=document.createElementNS(NS,'circle');
    const t=(n.d.tier||'').toUpperCase();
    // 有社区时按社区着色（更能看出圈子）；否则回落到分层配色
    const comm=n.d.community;
    c.setAttribute('fill', (comm!==null&&comm!==undefined) ? ccOf(comm)
                    : (t==='AAA'||t==='AAAA')?'#ff7a6b':t==='AA'?'#4ecdc4':'#8b93a7');
    c.setAttribute('r',n.r);c.setAttribute('opacity','.92');
    c.style.cursor='pointer';
    c.__n=n;
    gN.appendChild(c);n.el=c;
    const tx=document.createElementNS(NS,'text');
    tx.setAttribute('fill','#e8eaf0');tx.setAttribute('font-size',DENSE?'10':'12');
    tx.setAttribute('text-anchor','middle');tx.setAttribute('font-family',FONT);
    tx.setAttribute('y',n.r+14);tx.textContent=n.d.display_name;
    // 密集时低关系度节点先不显示标签（hover 仍可看）
    n.labelHidden = DENSE && deg[n.d.person_key] < MIN_DEG_LABEL;
    tx.style.display = n.labelHidden ? 'none' : '';
    gN.appendChild(tx);n.tx=tx;
    c.addEventListener('mousemove',ev=>{
      const r=svg.getBoundingClientRect();
      tip.style.display='block';
      tip.style.left=(ev.clientX-r.left+14)+'px';
      tip.style.top=(ev.clientY-r.top+14)+'px';
      const d=n.d;const tags=(d.tier?d.tier+' · ':'')+(d.circle||'');
      const commTxt = (d.community!==null&&d.community!==undefined)
          ? `<br>圈子 ${d.community+1}（${d.community_size||0} 人）` : '';
      tip.innerHTML=`<div class="tipname">${d.display_name}</div>`+
        `<div class="tipmeta">${tags}${commTxt}<br>消息 ${d.msg_total||0} 条`+
        (d.role?` · ${d.role}`:'')+`</div>`;
    });
    c.addEventListener('mouseleave',()=>tip.style.display='none');
    c.addEventListener('click',ev=>{
      ev.stopPropagation();
      const k=n.d.person_key;
      focusKey = (focusKey===k) ? null : k;   // 再点一次取消
      applyFocus();
    });
  });

  // 点击聚焦：高亮该人的关系圈，淡化无关节点
  function applyFocus(){
    if(!focusKey){
      nodes.forEach(n=>{n.el.setAttribute('opacity','.92');n.el.setAttribute('stroke','none');
                         n.tx.style.opacity='1';});
      links.forEach(l=>{l.el.setAttribute('opacity',Math.min(0.28+l.w*0.16,0.9));
                         l.el.setAttribute('stroke','#39404f');
                         if(l.lbl)l.lbl.style.opacity='1';});
      return;
    }
    const near=nbr[focusKey]||new Set();
    nodes.forEach(n=>{
      const k=n.d.person_key;
      const isF=(k===focusKey), isN=near.has(k);
      n.el.setAttribute('opacity', (isF||isN)?'.95':'.12');
      n.el.setAttribute('stroke', isF?'#ffffff':'none');
      n.el.setAttribute('stroke-width', isF?'2.5':'0');
      n.tx.style.opacity=(isF||isN)?'1':'.12';
    });
    links.forEach(l=>{
      const s=nodes[l.s].d.person_key, t2=nodes[l.t].d.person_key;
      const touch=(s===focusKey||t2===focusKey);
      l.el.setAttribute('opacity', touch?'.95':'.05');
      l.el.setAttribute('stroke', touch?'#ff7a6b':'#39404f');
      if(l.lbl) l.lbl.style.opacity= touch?'1':'0';
    });
  }
  svg.addEventListener('click',()=>{ if(focusKey){focusKey=null;applyFocus();} });

  function paint(){nodes.forEach(n=>{n.el.setAttribute('cx',n.x);n.el.setAttribute('cy',n.y);
    n.tx.setAttribute('x',n.x);n.tx.setAttribute('y',n.y+n.r+14);
    n.tx.style.display=(labelMode==='none'||(labelMode==='auto'&&n.labelHidden))?'none':'';});
    links.forEach(l=>{l.el.setAttribute('x1',nodes[l.s].x);l.el.setAttribute('y1',nodes[l.s].y);
      l.el.setAttribute('x2',nodes[l.t].x);l.el.setAttribute('y2',nodes[l.t].y);
      if(l.lbl){const a=nodes[l.s],b=nodes[l.t];
        l.lbl.setAttribute('x',(a.x+b.x)/2);l.lbl.setAttribute('y',(a.y+b.y)/2-3);}});
    resolveLabelOverlap();}

  // 节点标签碰撞规避：重叠的直接隐藏（优先保留关系度高的）
  // 用真实 getBBox 测量——中文按字符数估算宽度会偏小，导致漏判重叠
  function resolveLabelOverlap(){
    const shown=nodes.filter(n=>n.tx.style.display!=='none')
                     .sort((a,b)=>deg[b.d.person_key]-deg[a.d.person_key]);
    const placed=[];
    for(const n of shown){
      // getBBox 对 display:none 元素返回空盒，测量前先临时置为可见
      const prev=n.tx.style.display;
      n.tx.style.display='';
      let box;
      try{ box=n.tx.getBBox(); }catch(e){ box={x:n.x-30,y:n.y-10,width:60,height:14}; }
      if(!box.width){ box={x:n.x-30,y:n.y-10,width:60,height:14}; }
      let hit=false;
      for(const p of placed){
        if(box.x < p.x+p.w && box.x+box.width > p.x
           && box.y < p.y+p.h && box.y+box.height > p.y){hit=true;break;}
      }
      if(hit){n.tx.style.display='none';}
      else placed.push({x:box.x,y:box.y,w:box.width,h:box.height});
    }
  }

  // 缩放到内容边界 —— 关键：让图撑满画布而不是挤在中间
  let view={s:1,tx:0,ty:0};
  function fit(){
    if(!nodes.length)return;
    const vw=svg.clientWidth, vh=svg.clientHeight;
    if(!vw||!vh) return;                 // 尺寸未就绪，等下一帧
    // 用分位数而不是极值 —— 孤立点会把包围盒拉飞，导致图缩在角落
    const xs=nodes.map(n=>n.x).sort((a,b)=>a-b);
    const ys=nodes.map(n=>n.y).sort((a,b)=>a-b);
    const q=(arr,p)=>arr[Math.min(arr.length-1,Math.max(0,Math.floor(arr.length*p)))];
    const pad=26;
    let minX=q(xs,0.02)-pad, maxX=q(xs,0.98)+pad;
    let minY=q(ys,0.02)-pad, maxY=q(ys,0.98)+pad;
    const cw=Math.max(maxX-minX,60), ch=Math.max(maxY-minY,60);
    const s=Math.min((vw-40)/cw,(vh-40)/ch);
    view.s=Math.max(0.3,Math.min(s,4));
    // 居中到「主群」而非全图（用中位数中心）
    const cx=(q(xs,0.15)+q(xs,0.85))/2, cy=(q(ys,0.15)+q(ys,0.85))/2;
    view.tx=vw/2-cx*view.s;
    view.ty=vh/2-cy*view.s;
    applyView();
  }
  function applyView(){
    root.setAttribute('transform',`translate(${view.tx},${view.ty}) scale(${view.s})`);
  }
  paint();

  // 拖拽（空白处平移整个画布；拖节点只移动该节点）
  let dragNode=null, panning=false, px=0, py=0;
  svg.addEventListener('mousedown',ev=>{
    px=ev.clientX;py=ev.clientY;
    if(ev.target.tagName==='circle'){dragNode=ev.target.__n;} else {panning=true;}
  });
  window.addEventListener('mouseup',()=>{dragNode=null;panning=false;});
  window.addEventListener('mousemove',ev=>{
    if(dragNode){
      const dx=(ev.clientX-px)/view.s, dy=(ev.clientY-py)/view.s;
      dragNode.x+=dx; dragNode.y+=dy; dragNode.vx=0; dragNode.vy=0;
      px=ev.clientX;py=ev.clientY; paint();
    }else if(panning){
      view.tx+=(ev.clientX-px); view.ty+=(ev.clientY-py);
      px=ev.clientX;py=ev.clientY; applyView();
    }
  });
  svg.addEventListener('wheel',ev=>{ev.preventDefault();
    const f=ev.deltaY<0?1.12:0.89;
    const r=svg.getBoundingClientRect();
    const mx=ev.clientX-r.left, my=ev.clientY-r.top;
    const ns=Math.max(0.2,Math.min(6,view.s*f));
    // 以鼠标为锚点缩放
    view.tx=mx-(mx-view.tx)*(ns/view.s);
    view.ty=my-(my-view.ty)*(ns/view.s);
    view.s=ns; applyView();},{passive:false});

  window.restart=function(){nodes.forEach((n,i)=>{
    n.x=W/2+Math.cos(i/nodes.length*6.28)*260;n.y=H/2+Math.sin(i/nodes.length*6.28)*260;
    n.vx=0;n.vy=0;});for(let i=0;i<ITER;i++)tick();paint();fit();};
  window.toggleLabels=function(){
    labelMode = labelMode==='auto' ? 'all' : (labelMode==='all' ? 'none' : 'auto');
    nodes.forEach(n=>{n.tx.style.display=(labelMode==='none'||(labelMode==='auto'&&n.labelHidden))?'none':'';});
    if(labelMode!=='auto') resolveLabelOverlap();
    const btn=document.querySelector('.toolbar button:nth-child(2)');
    if(btn) btn.textContent = labelMode==='auto'?'显示全部标签' : (labelMode==='all'?'隐藏全部标签':'标签：自动');
  };
  window.addEventListener('resize',()=>{fit();});
  // 首帧：布局跑完后再 fit（此时 clientWidth 才是真实值）
  paint();
  // 用 rAF 轮询直到容器拿到真实尺寸（SVG 在 grid 里首帧可能是 0）
  let tries=0;
  (function waitFit(){
    fit();
    if((!svg.clientWidth||!svg.clientHeight)&&tries<30){tries++;requestAnimationFrame(waitFit);}
  })();
  setTimeout(fit,120); setTimeout(fit,400);
})();
</script>
</body>
</html>"""


def write_graph_html(con, out_path, limit=60, min_weight=1.0, tier=None,
                     title="仁谱 · 关系图谱", subtitle="", focus=None, share=False):
    graph = collect_graph(con, limit=limit, min_weight=min_weight, tier=tier,
                          focus=focus, share=share)
    if not subtitle:
        st = core.stats(con)
        subtitle = (f"{st['people']} 人 · {st['edges']} 条关系边 · "
                    f"{st['rooms']} 个群 · 本地生成，数据不出本机")
    html_text = render_html(graph, title=title, subtitle=subtitle)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_text, encoding="utf-8")
    return out, graph
