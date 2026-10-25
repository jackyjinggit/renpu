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

    # 3b. 时间维度：首次进入时间（用于时间轴滑块）
    fs = {}
    if keys:
        ph2 = ",".join("?" * len(keys))
        for r in con.execute(
                f"SELECT person_key, first_seen FROM person "
                f"WHERE person_key IN ({ph2}) AND first_seen > 0", keys).fetchall():
            fs[r[0]] = r[1]
    tmin = min(fs.values()) if fs else None
    tmax = max(fs.values()) if fs else None
    for p in people:
        t = fs.get(p["person_key"])
        p["first_ts"] = t
        p["t_norm"] = round((t - tmin) / max((tmax - tmin), 1), 4) if (t and tmin is not None) else None

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
    return {"nodes": people, "links": uniq, "rooms": rooms,
            "time_axis": {"enabled": bool(fs), "min": tmin, "max": tmax,
                          "count": len(fs)}}


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
  .wrap{display:grid;grid-template-columns:1fr 300px;gap:0;height:calc(100vh - 108px);
     min-height:0}
  @media(max-width:900px){.wrap{grid-template-columns:1fr;height:auto}}
  #stage{position:relative;overflow:hidden;min-height:0}
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
  /* 时间轴 */
  .timebar{position:absolute;left:50%;transform:translateX(-50%);bottom:16px;z-index:12;
     background:rgba(23,26,33,.96);border:1px solid var(--line);border-radius:10px;
     padding:11px 16px 13px;min-width:430px;max-width:78vw;
     box-shadow:0 8px 28px rgba(0,0,0,.45)}
  .timebar .row{display:flex;align-items:center;gap:12px}
  .timebar .lab{font-size:11px;color:var(--dim);white-space:nowrap}
  .timebar .val{font-size:12px;color:var(--text);font-variant-numeric:tabular-nums;
     white-space:nowrap;min-width:76px}
  input[type=range]{-webkit-appearance:none;appearance:none;flex:1;height:4px;border-radius:2px;
     background:linear-gradient(90deg,#4ecdc4 var(--p,100%),#2a3040 var(--p,100%));outline:none}
  input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;
     width:15px;height:15px;border-radius:50%;background:#ff7a6b;cursor:pointer;
     border:2px solid #0f1115;box-shadow:0 0 8px rgba(255,122,107,.6)}
  .timebar .hint{font-size:10.5px;color:var(--dim);margin-top:7px;line-height:1.5}
  .tbtn{background:transparent;border:1px solid var(--line);color:var(--dim);
     border-radius:6px;padding:3px 9px;font-size:11px;cursor:pointer;font-family:inherit}
  .tbtn:hover{border-color:var(--accent);color:var(--accent)}
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
      <button id="btn3d" onclick="make3d()" style="display:none">生成 3D 圈</button>
    </div>
    <div class="timebar" id="timebar" style="display:none">
      <div class="row">
        <span class="lab">时间轴</span>
        <input type="range" id="tslider" min="0" max="1000" value="1000" oninput="onTime(this.value)">
        <span class="val" id="tval"></span>
        <button class="tbtn" onclick="resetTime()">全部</button>
      </div>
      <div class="hint" id="thint">拖动查看圈子随时间扩张 —— 只显示当时已进入你人脉的人</div>
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
  let timeRelaxed=false;    // 时间过滤后候选集变小 → 放开密度压制

  // 统计条
  const deg={}; G.nodes.forEach(n=>deg[n.person_key]=0);
  // 时间轴状态提前声明（paint() 在初始化末尾会调用 applyFocus，
  // 若 TA/时间轴变量在使用后才声明会触发 TDZ 报错）
  const TA = G.time_axis || {enabled:false};
  let timeCut = 1.0;
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
    // 时间过滤时，被排除的节点不参与布局（否则它们会把可见节点顶开、占位）
    const filtering = (TA.enabled && timeCut<0.999);
    const act = filtering ? nodes.filter(n=>!n.timeHidden) : nodes;
    for(let i=0;i<act.length;i++){const a=act[i];
      for(let j=i+1;j<act.length;j++){const b=act[j];
        let dx=b.x-a.x,dy=b.y-a.y;let d2=dx*dx+dy*dy||1;
        if(d2<98000){const f=REPULSE/d2;const d=Math.sqrt(d2);
          const ux=dx/d,uy=dy/d;a.vx-=ux*f;a.vy-=uy*f;b.vx+=ux*f;b.vy+=uy*f;}}}
    links.forEach(l=>{const a=nodes[l.s],b=nodes[l.t];
      if(filtering && (a.timeHidden||b.timeHidden)) return;  // 隐藏边的两端不参与
      let dx=b.x-a.x,dy=b.y-a.y;let d=Math.sqrt(dx*dx+dy*dy)||1;
      // 目标距离随共同群数增加（关系越强靠得越近）
      const target=70+ (4-Math.min(l.w,4))*26;const f=(d-target)*0.016*Math.min(l.w,4);
      const ux=dx/d,uy=dy/d;a.vx+=ux*f;a.vy+=uy*f;b.vx-=ux*f;b.vy-=uy*f;});
    act.forEach(a=>{a.vx+=(W/2-a.x)*0.0022;a.vy+=(H/2-a.y)*0.0022;
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
    t.setAttribute('class','elabel');
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
    tx.setAttribute('class','nlabel');
    // 字号按名字长度自适应：微信昵称可能很长（还带 emoji），
    // 固定字号会让长名糊成一团 —— 宁可缩小，也要看清是谁
    const nm=String(n.d.display_name||'');
    const nmLen=[...nm].length;           // 按码点算，emoji 占 1
    const baseFs=DENSE?10:12;
    const fs = nmLen>16 ? 8 : (nmLen>11 ? 9 : baseFs);
    tx.setAttribute('fill','#e8eaf0');tx.setAttribute('font-size',fs);
    // 加深色描边：即使与其他标签轻微交叠，也能读清（排版层兜底，不依赖避让时序）
    tx.setAttribute('paint-order','stroke');
    tx.setAttribute('stroke','#0f1115');tx.setAttribute('stroke-width','3.5');
    tx.setAttribute('stroke-linejoin','round');
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
      sync3dButton();
    });
  });

  // 点击聚焦：高亮该人的关系圈，淡化无关节点
  // 与时间轴叠加：焦点优先，其次时间过滤（被时间排除的一律保持淡化）
  function applyFocus(){
    const timeOn = (typeof TA!=='undefined') && TA.enabled;
    if(!focusKey){
    nodes.forEach(n=>{
      const hidden = timeOn && n.timeHidden;
      // 时间排除的节点完全隐藏（淡出态会占位并干扰聚焦感）
      n.el.setAttribute('opacity', hidden ? '0' : (n.el.getAttribute('data-op')||'.92'));
      n.el.setAttribute('stroke','none');
      n.tx.style.display = (hidden || !labelCandidate(n)
        || (labelMode==='auto'&&n.overlapHidden)) ? 'none' : '';
      n.tx.style.opacity = '1';
    });
    links.forEach(l=>{
      const hidden = timeOn && (nodes[l.s].timeHidden||nodes[l.t].timeHidden);
      l.el.setAttribute('opacity', hidden ? '0' : (l.baseOp!==undefined?l.baseOp:Math.min(0.28+l.w*0.16,0.9)));
      l.el.setAttribute('stroke','#39404f');
      if(l.lbl)l.lbl.style.opacity= hidden?'0':'1';
    });
    return;
    }
    const near=nbr[focusKey]||new Set();
    nodes.forEach(n=>{
      const k=n.d.person_key;
      const isF=(k===focusKey), isN=near.has(k);
      const timeHidden = timeOn && n.timeHidden;
      const on = (isF||isN) && !timeHidden;
      n.el.setAttribute('opacity', on?'.95':(timeHidden?'0':'.28'));
      n.el.setAttribute('stroke', isF?'#ffffff':'none');
      n.el.setAttribute('stroke-width', isF?'2.5':'0');
      const showLbl = (on && labelCandidate(n)
                       && !(labelMode==='auto'&&n.overlapHidden));
      n.tx.style.display = showLbl ? '' : 'none';
      n.tx.style.opacity = on?'1':'.30';
    });
    links.forEach(l=>{
      const s=nodes[l.s].d.person_key, t2=nodes[l.t].d.person_key;
      const touch=(s===focusKey||t2===focusKey);
      const timeHidden = timeOn && (nodes[l.s].timeHidden||nodes[l.t].timeHidden);
      const on = touch && !timeHidden;
      l.el.setAttribute('opacity', on?'.95':(timeHidden?'0':'.12'));
      l.el.setAttribute('stroke', touch?'#ff7a6b':'#39404f');
      if(l.lbl) l.lbl.style.opacity= on?'1':'0';
    });
  }
  svg.addEventListener('click',()=>{ if(focusKey){focusKey=null;applyFocus();sync3dButton();} });

  function paint(){nodes.forEach(n=>{n.el.setAttribute('cx',n.x);n.el.setAttribute('cy',n.y);
    n.tx.setAttribute('x',n.x);n.tx.setAttribute('y',n.y+n.r+14);});   // 标签显隐交给 applyFocus
    links.forEach(l=>{l.el.setAttribute('x1',nodes[l.s].x);l.el.setAttribute('y1',nodes[l.s].y);
      l.el.setAttribute('x2',nodes[l.t].x);l.el.setAttribute('y2',nodes[l.t].y);
      if(l.lbl){const a=nodes[l.s],b=nodes[l.t];
        l.lbl.setAttribute('x',(a.x+b.x)/2);l.lbl.setAttribute('y',(a.y+b.y)/2-3);}});
    // 三遍：定「谁该有标签」→ 算节点标签重叠 → 落地 → 边标签避让
    updateDensity();
    applyFocus(); resolveLabelOverlap(); applyFocus();
    resolveEdgeLabelOverlap();
  }

  // 节点标签碰撞规避：重叠的直接隐藏（优先保留关系度高的）
  // 用真实 getBBox 测量——中文按字符数估算宽度会偏小，导致漏判重叠
  // 标签候选集判定：纯状态，不依赖上一轮 display（否则会连锁永久隐藏）
  function labelCandidate(n){
    if(labelMode==='none') return false;
    if(TA.enabled && n.timeHidden) return false;
    // DENSE 的低-degree 压制只在画面仍然拥挤时才生效；
    // 时间过滤后可见节点变少 → 空间充裕 → 解禁，让标签补齐
    if(labelMode==='auto' && n.labelHidden && !timeRelaxed) return false;
    return true;
  }
  // 依据当前可见节点数更新密度状态
  function updateDensity(){
    if(!TA.enabled){ timeRelaxed = nodes.length<=28; return; }
    const vis=nodes.filter(n=>!n.timeHidden).length;
    timeRelaxed = vis<=28;
  }
  function resolveLabelOverlap(){
    // 所有节点都要重置标记，否则上轮被隐藏的节点会永远出局
    nodes.forEach(n=>{n.overlapHidden=false;});
    const shown=nodes.filter(n=>labelCandidate(n))
                     .sort((a,b)=>deg[b.d.person_key]-deg[a.d.person_key]);
    const placed=[];
    for(const n of shown){
      // getBBox 对 display:none 元素返回空盒，测量前先临时置为可见，测完还原
      const before=n.tx.style.display;
      n.tx.style.display='';
      let box;
      try{ box=n.tx.getBBox(); }catch(e){ box=null; }
      if(!box || !box.width){ box={x:n.x-30,y:n.y-10,width:60,height:14}; }
      let hit=false;
      // 容差 4px：覆盖描边宽度（3.5/2≈1.75px）+ 安全间隙，否则描边看起来仍像叠字
      const PAD=4;
      for(const p of placed){
        if(box.x-PAD < p.x+p.w && box.x+box.width+PAD > p.x
           && box.y-PAD < p.y+p.h && box.y+box.height+PAD > p.y){hit=true;break;}
      }
      if(hit){n.overlapHidden=true;}      // 只做标记，最终显隐交给 applyFocus
      else placed.push({x:box.x,y:box.y,w:box.width,h:box.height});
      n.tx.style.display=before;          // 还原
    }
  }

  // 边标签避让：必须同时避开已显示的节点标签，否则中心区会叠成一团
  function resolveEdgeLabelOverlap(){
    const boxes=[];
    nodes.forEach(n=>{
      if(n.tx.style.display==='none') return;
      try{ const b=n.tx.getBBox(); if(b.width) boxes.push({x:b.x,y:b.y,w:b.width,h:b.height,t:n.tx.textContent}); }catch(e){}
    });
    // 候选 = 两端节点都可见的边（不看边的 opacity —— 边的 opacity 会被聚焦逻辑改写，
    // 用它做筛选会漏掉本该避让的边）
    const cand=links.filter(l=>{
      if(!l.lbl) return false;
      const a=nodes[l.s], b=nodes[l.t];
      return !(TA.enabled && (a.timeHidden||b.timeHidden));
    });
    // 聚焦态下，与焦点无关的边标签应当隐藏 —— 记下来，别被避让逻辑放出来
    const focusHidden=new Set();
    if(focusKey){
      cand.forEach(l=>{
        const sk=nodes[l.s].d.person_key, tk=nodes[l.t].d.person_key;
        if(sk!==focusKey && tk!==focusKey) focusHidden.add(l.lbl);
      });
    }
    cand.sort((a,b)=>b.w-a.w);   // 权重高的优先保留
    const keep=[];
    const hidden=[];
    // 边标签自身的可见状态也要重置：不能沿用上一轮的隐藏，否则永久消失
    cand.forEach(l=>{ l.lbl.style.display=''; });
    cand.forEach(l=>{
      const t=l.lbl;
      let box; try{ box=t.getBBox(); }catch(e){ box=null; }
      if(!box||!box.width){ box={x:(nodes[l.s].x+nodes[l.t].x)/2-12,y:(nodes[l.s].y+nodes[l.t].y)/2-6,width:24,height:12}; }
      // 容差 4px：覆盖描边宽度（3/2=1.5px）+ 安全间隙
      const PAD=4;
      let hit=false;
      for(const b of boxes){ if(box.x-PAD<b.x+b.w&&box.x+box.width+PAD>b.x&&box.y-PAD<b.y+b.h&&box.y+box.height+PAD>b.y){hit=true;break;} }
      for(const b of keep){ if(box.x-PAD<b.x+b.w&&box.x+box.width+PAD>b.x&&box.y-PAD<b.y+b.h&&box.y+box.height+PAD>b.y){hit=true;break;} }
      if(hit){ hidden.push(t); }
      else { keep.push({x:box.x,y:box.y,w:box.width,h:box.height}); }
    });
    // 本轮撞上的才隐藏；没撞的保持可见
    hidden.forEach(t=>{t.style.display='none';});
    // 聚焦态下无关的边标签重新藏起来
    focusHidden.forEach(t=>{t.style.display='none';});
  }

  // 缩放到内容边界 —— 关键：让图撑满画布而不是挤在中间
  let view={s:1,tx:0,ty:0};
  function fit(){
    if(!nodes.length)return;
    const vw=svg.clientWidth, vh=svg.clientHeight;
    if(!vw||!vh) return;                 // 尺寸未就绪，等下一帧
    // 时间过滤时只按「可见节点」算边界 —— 否则被排除的暗点仍占位，画面永远是满的
    const pool = (TA.enabled && nodes.some(n=>n.timeHidden))
      ? nodes.filter(n=>!n.timeHidden) : nodes;
    if(!pool.length) return;
    const xs=pool.map(n=>n.x).sort((a,b)=>a-b);
    const ys=pool.map(n=>n.y).sort((a,b)=>a-b);
    const q=(arr,p)=>arr[Math.min(arr.length-1,Math.max(0,Math.floor(arr.length*p)))];
    // 节点少时用极值（分位数会把稀疏图缩在角落）；节点多时用分位数（孤立点会把包围盒拉飞）
    const sparse = pool.length<=25;
    const lo = sparse?0:0.02, hi = sparse?1:0.98;
    const pad=26;
    let minX=q(xs,lo)-pad, maxX=q(xs,hi)+pad;
    let minY=q(ys,lo)-pad, maxY=q(ys,hi)+pad;
    const cw=Math.max(maxX-minX,60), ch=Math.max(maxY-minY,60);
    const s=Math.min((vw-40)/cw,(vh-40)/ch);
    view.s=Math.max(0.3,Math.min(s,4));
    // 居中到「主群」而非全图（用中位数中心）
    const cx=sparse?(q(xs,0)+q(xs,1))/2:(q(xs,0.15)+q(xs,0.85))/2;
    const cy=sparse?(q(ys,0)+q(ys,1))/2:(q(ys,0.15)+q(ys,0.85))/2;
    view.tx=vw/2-cx*view.s;
    view.ty=vh/2-cy*view.s;
    applyView();
  }
  function applyView(){
    root.setAttribute('transform',`translate(${view.tx},${view.ty}) scale(${view.s})`);
  }
  paint();

  // 时间轴：拖动看圈子随时间扩张
  const slider = document.getElementById('tslider');
  const tval = document.getElementById('tval');
  const thint = document.getElementById('thint');
  const timebar = document.getElementById('timebar');

  function fmtTime(ts){
    const d=new Date(ts*1000);
    return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');
  }
  function applyTime(){
    if(!TA.enabled){return;}
    const cut = timeCut;
    nodes.forEach(n=>{
      const t=n.d.t_norm;
      let vis;
      if(t===null||t===undefined) vis=true;       // 无时间数据：保留
      else vis = (t<=cut+0.0001);
      n.timeHidden = !vis;
      n.hasTime = (t!==null&&t!==undefined);
    });
    links.forEach(l=>{
      const a=nodes[l.s], b=nodes[l.t];
      l.timeHidden = !!(a.timeHidden||b.timeHidden);
    });
    // 统计与文案
    if(TA.max&&TA.min){
      const ts = TA.min + (TA.max-TA.min)*cut;
      tval.textContent = cut>=0.999 ? '全部' : fmtTime(ts);
    }
    const cnt = nodes.filter(n=>!n.timeHidden && n.hasTime).length;
    thint.textContent = (cut>=0.999)
      ? '拖动查看圈子随时间扩张 —— 只显示当时已进入你人脉的人'
      : `此时已进入你人脉的有 ${cnt} 人（其余淡出）`;
    // 交给聚焦逻辑统一渲染（两者叠加）
    // 注意：也要重算标签重叠，否则被时间排除的节点会留下孤儿标签
    updateDensity();
    applyFocus(); resolveLabelOverlap(); applyFocus();
    resolveEdgeLabelOverlap();
    // 可见集合变了 → 重新布局（隐藏节点不参与），让稀疏的早期圈子聚拢
    relayoutVisible();
    fit();   // 重新缩放，让「已进入人脉的人」占满画布
  }
  // 只对可见节点重跑力导向（隐藏节点原地不动，回来时位置还在）
  function relayoutVisible(){
    const filtering = (TA.enabled && timeCut<0.999);
    if(!filtering) return;
    const act = nodes.filter(n=>!n.timeHidden);
    if(act.length<2) return;
    // 从圆环起步，避免沿用旧布局的惯性
    act.forEach((n,i)=>{
      const a=i/act.length*6.283;
      n.x=W/2+Math.cos(a)*180; n.y=H/2+Math.sin(a)*180; n.vx=0; n.vy=0;});
    for(let i=0;i<Math.min(400,120+act.length*8);i++)tick();
    paint();
  }
  window.onTime=function(v){
    timeCut = Number(v)/1000;
    slider.style.setProperty('--p', (timeCut*100).toFixed(1)+'%');
    applyTime();
  };
  window.resetTime=function(){
    timeCut=1.0; slider.value=1000;
    slider.style.setProperty('--p','100%');
    applyTime();
  };
  if(TA.enabled){
    timebar.style.display='block';
    slider.style.setProperty('--p','100%');
    tval.textContent='全部';
    // 记录基础透明度，供时间过滤时还原（必须早于任何 applyTime 调用）
    nodes.forEach(n=>{n.el.setAttribute('data-op', n.el.getAttribute('opacity')||'.92');});
    links.forEach(l=>{l.baseOp=l.el.getAttribute('opacity')||'.3';});
  }

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

  // 3D 聚光：静态 HTML 不能直接跑命令，所以给出可复制的命令
  function sync3dButton(){
    const btn=document.getElementById('btn3d');
    if(!btn) return;
    btn.style.display = focusKey? 'inline-block':'none';
  }
  window.make3d=function(){
    if(!focusKey) return;
    const n=nodes.find(x=>x.d.person_key===focusKey);
    if(!n) return;
    const nm=String(n.d.display_name||'').replace(/"/g,'');
    const cmd='renpu graph3d --person "'+nm+'" --share';
    const tip=document.getElementById('tip');
    const done=(ok)=>{
      tip.style.display='block';
      tip.style.left='50%'; tip.style.top='76px';
      tip.innerHTML='<div class="tipname">'+(ok?'✅ 命令已复制':'命令：')+'</div>'+
        '<div class="tipmeta">'+cmd+'</div>';
      clearTimeout(window.__t3);
      window.__t3=setTimeout(()=>{tip.style.display='none';},4200);
    };
    if(navigator.clipboard&&navigator.clipboard.writeText){
      navigator.clipboard.writeText(cmd).then(()=>done(true),()=>done(false));
    } else { done(false); }
  };
  window.restart=function(){nodes.forEach((n,i)=>{
    n.x=W/2+Math.cos(i/nodes.length*6.28)*260;n.y=H/2+Math.sin(i/nodes.length*6.28)*260;
    n.vx=0;n.vy=0;});for(let i=0;i<ITER;i++)tick();paint();fit();};
  window.toggleLabels=function(){
    labelMode = labelMode==='auto' ? 'all' : (labelMode==='all' ? 'none' : 'auto');
    // 统一走 applyFocus → resolveLabelOverlap → applyFocus，保证与时间过滤/聚焦一致
    updateDensity(); applyFocus(); resolveLabelOverlap(); applyFocus();
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
