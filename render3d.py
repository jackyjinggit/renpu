# -*- coding: utf-8 -*-
"""仁谱 · 关系圈 3D 聚光视图（零第三方依赖）

为什么不用 CSS 3D transform：
    纯 CSS 3D 没有真正的 3D 渲染管线（无深度缓冲、无正确投影），
    连线靠 rotateY + rotateZ 拼接，886 条边实测 884 条横穿侧边栏、
    节点被拉成斜线阵列、UI 层被吃掉。CSS 3D 撑不住仁谱规模。

这里用 SVG + 手写透视投影：
    - 自己算相机与投影，一次算完整张图
    - 画家算法：按深度排序绘制，天然处理遮挡
    - 节点仍是 SVG 元素 → 标签能用 getBBox 精确测量、可加描边、可避让
    - 单文件 HTML、零外链、离线可用

用途：
    2D 图负责全景（「谁跟谁是一伙的」）；
    本模块负责聚焦（「这个人的人脉是个什么结构」——几十个节点，3D 完全够）。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

# ── 调色（与 2D 图一致：深底 + 珊瑚/青强调） ──
BG = "#0f1115"
TEXT = "#e8eaf0"
DIM = "#8b93a7"
ACCENT = "#ff7a6b"
ACCENT2 = "#4ecdc4"
EDGE = "#39404f"
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif"

CC = ['#ff7a6b', '#4ecdc4', '#ffd166', '#a78bfa', '#7dd3fc',
      '#f472b6', '#86efac', '#fb923c', '#c4b5fd', '#67e8f9']


def community_color(c):
    if c is None:
        return "#8b93a7"
    return CC[int(c) % len(CC)]


def collect_neighbourhood(con, center_key, max_people=36, share=False):
    """取「中心人 + 其关系邻居」，作为 3D 聚光的节点集。

    只取真实有关系的人 —— 这是 3D 能成立的前提：
    几十个节点用球面布局，结构清晰；几百个就会糊。
    """
    rows = con.execute(
        "SELECT src, dst, weight FROM edge "
        "WHERE src=? OR dst=? ORDER BY weight DESC LIMIT 600",
        (center_key, center_key)).fetchall()
    seen = set()
    order = []
    for r in rows:
        a, b = r["src"], r["dst"]
        other = b if a == center_key else a
        if other == center_key or other in seen:
            continue
        seen.add(other)
        order.append(other)

    keys = [center_key] + order[:max_people - 1]
    if not keys:
        return {"nodes": [], "links": [], "center": None}

    ph = ",".join("?" * len(keys))
    people = [dict(r) for r in con.execute(
        f"SELECT person_key, display_name, tier, circle, role, msg_total "
        f"FROM person WHERE person_key IN ({ph})", keys).fetchall()]
    by_key = {p["person_key"]: p for p in people}

    ordered = [by_key[k] for k in keys if k in by_key]

    kset = set(keys)
    uniq = {}
    for r in con.execute(
        f"SELECT src, dst, weight FROM edge WHERE src IN ({ph}) AND dst IN ({ph})",
        keys + keys).fetchall():
        if r["src"] in kset and r["dst"] in kset and r["src"] != r["dst"]:
            pair = tuple(sorted((r["src"], r["dst"])))
            uniq[pair] = max(uniq.get(pair, 0), r["weight"])
    links = [{"src": a, "dst": b, "weight": w} for (a, b), w in uniq.items()]

    # 这些人共同出现的群（用于头部统计）
    rooms = [dict(r) for r in con.execute(
        f"SELECT room.room_key AS room_key, room.name AS name, COUNT(*) AS mine "
        f"FROM room_member rm JOIN room ON room.room_key = rm.room_key "
        f"WHERE rm.person_key IN ({ph}) "
        f"GROUP BY room.room_key, room.name ORDER BY mine DESC LIMIT 12", keys).fetchall()]

    nodes = []
    for i, p in enumerate(ordered):
        nodes.append({
            "person_key": p["person_key"],
            "display_name": p["display_name"],
            "tier": p["tier"],
            "circle": p["circle"],
            "role": p["role"],
            "msg_total": p["msg_total"],
            "community": None,
            "is_center": i == 0,
        })
    if share:
        for n in nodes:
            n["tier"] = None
            n["circle"] = None
            n["role"] = None
            n["msg_total"] = None
        for r in rooms:
            r["name"] = "群（已脱敏）"
    return {"nodes": nodes, "links": links, "rooms": rooms, "center": center_key}


_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>仁谱 · 关系圈 3D 聚光</title>
<style>
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:#0f1115;color:#e8eaf0;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif}
.wrap{display:flex;flex-direction:column;height:100vh;overflow:hidden}
header{padding:16px 22px 14px;border-bottom:1px solid #232732;flex:0 0 auto}
h1{margin:0;font-size:17px;font-weight:600;letter-spacing:.4px}
.sub{color:#8b93a7;font-size:11.5px;margin-top:5px;line-height:1.7}
.sub b{color:#e8eaf0;font-weight:500}
.stats{display:flex;gap:20px;margin-top:10px;flex-wrap:wrap}
.st{font-size:11px;color:#8b93a7}
.st b{display:block;color:#e8eaf0;font-size:18px;font-weight:600;
  font-variant-numeric:tabular-nums;margin-top:2px}
main{flex:1;display:flex;min-height:0}
#stage{flex:1;position:relative;overflow:hidden;min-width:0}
svg{width:100%;height:100%;display:block;cursor:grab}
svg.drag{cursor:grabbing}
aside{width:296px;flex:0 0 296px;border-left:1px solid #232732;padding:18px;
  overflow-y:auto;background:#12151b}
h2{font-size:12px;color:#8b93a7;font-weight:500;margin:0 0 12px;
  text-transform:uppercase;letter-spacing:.8px}
.toolbar{position:absolute;left:16px;top:16px;z-index:5;display:flex;gap:8px;flex-wrap:wrap}
button{background:#1c2029;color:#c8cedd;border:1px solid #2a2f3b;border-radius:7px;
  padding:6px 12px;font-size:12px;cursor:pointer;font-family:inherit}
button:hover{background:#232833}
button.on{background:#ff7a6b;border-color:#ff7a6b;color:#12151b}
.note{color:#8b93a7;font-size:11.5px;line-height:1.9;margin-top:18px;
  border-top:1px solid #232732;padding-top:14px}
.note b{color:#e8eaf0;font-weight:500}
.tip{position:absolute;display:none;background:rgba(23,26,33,.97);border:1px solid #2a2f3b;
  border-radius:7px;padding:8px 11px;font-size:12px;pointer-events:none;z-index:20;
  max-width:240px;box-shadow:0 6px 20px rgba(0,0,0,.5)}
.tipname{font-weight:600;margin-bottom:3px}
.tipmeta{color:#8b93a7;font-size:11px;line-height:1.65}
.legend{margin-bottom:16px}
.lg{display:flex;align-items:center;gap:9px;font-size:11.5px;color:#c8cedd;
  margin-bottom:7px;line-height:1.5}
.dot{width:9px;height:9px;border-radius:50%;flex:0 0 9px}
.empty{color:#8b93a7;font-size:13px;padding:30px;text-align:center}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>仁谱 · 关系圈 3D 聚光</h1>
    <div class="sub" id="sub"></div>
    <div class="stats" id="stats"></div>
  </header>
  <main>
    <div id="stage">
      <div class="toolbar">
        <button id="btnRot" onclick="toggleSpin()">暂停旋转</button>
        <button id="btnLabels" onclick="toggleLabels()">隐藏标签</button>
        <button onclick="resetView()">复位视角</button>
      </div>
      <svg id="svg"></svg>
      <div class="tip" id="tip"></div>
    </div>
    <aside>
      <h2>怎么看</h2>
      <div class="legend">
        <div class="lg"><span class="dot" style="background:#ff7a6b"></span>中心人物（你选的人）</div>
        <div class="lg"><span class="dot" style="background:#4ecdc4"></span>关系人</div>
      </div>
      <div class="note">
        <b>球面结构</b>：越靠近中心 = 关系越强<br>
        <b>连线粗细</b> = 共同出现的群数<br>
        <b>标签</b> = 名字（长名自动缩小）<br><br>
        拖动平移 · 滚轮缩放 · 自动旋转<br>
        悬停看详情 · 点空白停止旋转
      </div>
      <h2 style="margin-top:26px">这个人在你的圈子里</h2>
      <div class="note" id="who"></div>
    </aside>
  </main>
</div>
<script id='g3d' type='application/json'>__DATA__</script>
<script>
(function(){
'use strict';
const D = JSON.parse(document.getElementById('g3d').textContent);
const NS='http://www.w3.org/2000/svg';
const FONT="-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif";
const svg=document.getElementById('svg');
const tip=document.getElementById('tip');
const root=document.createElementNS(NS,'g');
svg.appendChild(root);

let spin=true, showLabels=true;
let yaw=-0.6, pitch=0.35, zoom=1, panX=0, panY=0;

// ── 节点布局：黄金角螺旋球面 ──
const nodes=D.nodes.map(n=>Object.assign({},n));
const N=nodes.length;
if(!N){ document.getElementById('stage').innerHTML='<div class="empty">没有可展示的关系</div>'; return; }
const GOLDEN=Math.PI*(3-Math.sqrt(5));
for(let i=0;i<N;i++){
  if(i===0){ nodes[i].x=0;nodes[i].y=0;nodes[i].z=0; continue; }
  const k=i-1, m=Math.max(1,N-1), t=k/m;
  const z=1-2*t, r=Math.sqrt(Math.max(0,1-z*z)), th=GOLDEN*k;
  const rad=250*(0.58+0.42*(1-t));
  nodes[i].x=Math.cos(th)*r*rad;
  nodes[i].y=Math.sin(th)*r*rad;
  nodes[i].z=z*rad*0.60;
}
const byKey={}; nodes.forEach(n=>byKey[n.person_key]=n);
const links=D.links.map(l=>({a:byKey[l.src],b:byKey[l.dst],w:l.weight}))
                   .filter(l=>l.a&&l.b);

// ── 相机（手写透视投影） ──
function project(x,y,z){
  const cy=Math.cos(yaw),sy=Math.sin(yaw),cp=Math.cos(pitch),sp=Math.sin(pitch);
  const x1=x*cy+z*sy, z1=-x*sy+z*cy;
  const y2=y*cp-z1*sp, z2=y*sp+z1*cp;
  const zc=z2+900; if(zc<=1) zc=1;
  const s=820/zc;
  return {x:x1*s, y:y2*s, z:zc, s:s};
}

const gE=document.createElementNS(NS,'g'), gN=document.createElementNS(NS,'g');
root.appendChild(gE); root.appendChild(gN);
const drawn=[];

function render(){
  const vw=svg.clientWidth||900, vh=svg.clientHeight||600;
  const ox=vw/2+panX, oy=vh/2+panY;

  // 自动适配：把球的投影边界缩放到画布（留边距），保证图填满可用空间
  let bx0=1e9,bx1=-1e9,by0=1e9,by1=-1e9;
  nodes.forEach(n=>{
    const p=project(n.x,n.y,n.z);
    n._p=p;
    bx0=Math.min(bx0,p.x); bx1=Math.max(bx1,p.x);
    by0=Math.min(by0,p.y); by1=Math.max(by1,p.y);
  });
  const spanX=Math.max(1,bx1-bx0), spanY=Math.max(1,by1-by0);
  const fitZ=Math.min((vw-70)/spanX,(vh-90)/spanY);
  const effZoom=Math.max(0.3,Math.min(2.2,zoom*fitZ));

  nodes.forEach(n=>{
    const p=n._p;
    n._sx=ox+p.x*effZoom; n._sy=oy+p.y*effZoom;
    // 深度：越小越近；缩放系数随深度变化
    n._depth=Math.max(0,Math.min(1,1-(p.z-200)/1500));
    n._r=Math.max(3,(n.is_center?14:7.5)*(0.55+0.75*n._depth))*effZoom;
  });

  // ── 连线 ──
  gE.textContent='';
  const ranked=links.slice().sort((a,b)=>b.w-a.w);
  const top=new Set(ranked.slice(0, Math.min(14, ranked.length)));
  links.forEach(l=>{
    const a=l.a,b=l.b;
    const ln=document.createElementNS(NS,'line');
    ln.setAttribute('x1',a._sx); ln.setAttribute('y1',a._sy);
    ln.setAttribute('x2',b._sx); ln.setAttribute('y2',b._sy);
    const near=(a._depth+b._depth)/2;
    // 只有「最粗的前 N 条」用珊瑚色高亮（真正的核心关系），其余一律灰淡。
    // 否则枢纽节点的 372 条边会全部染红，织成一张网盖住节点。
    const isTop=top.has(l);
    ln.setAttribute('stroke', isTop? '#ff7a6b' : '#39404f');
    ln.setAttribute('stroke-width', isTop? Math.max(1.2, Math.min(2.8, l.w*0.4))
                                          : Math.max(0.4, Math.min(1.4, l.w*0.26)));
    ln.setAttribute('opacity', isTop? Math.max(0.30, Math.min(0.85, near))
                                    : Math.max(0.03, Math.min(0.24, 0.05+l.w*0.035)*near));
    ln.setAttribute('stroke-linecap','round');
    gE.appendChild(ln);
  });

  // ── 节点（画家算法：远的先画） ──
  gN.textContent='';
  drawn.length=0;
  nodes.slice().sort((p,q)=>q._p.z-p._p.z).forEach(n=>{
    const g=document.createElementNS(NS,'g');
    const c=document.createElementNS(NS,'circle');
    c.setAttribute('cx',n._sx); c.setAttribute('cy',n._sy); c.setAttribute('r',n._r);
    c.setAttribute('fill', n.is_center? '#ff7a6b' : '#4ecdc4');
    c.setAttribute('opacity', Math.max(0.22, Math.min(1, 0.35+0.65*n._depth)));
    if(n.is_center){ c.setAttribute('stroke','#ffffff'); c.setAttribute('stroke-width','2'); }
    c.style.cursor='pointer';
    g.appendChild(c);

    const nm=String(n.display_name||'');
    const len=[...nm].length;
    const fs=len>16?8:(len>11?9:11);
    const t=document.createElementNS(NS,'text');
    t.setAttribute('x',n._sx); t.setAttribute('y',n._sy+n._r+11);
    t.setAttribute('text-anchor','middle'); t.setAttribute('font-family',FONT);
    t.setAttribute('font-size',fs); t.setAttribute('fill','#e8eaf0');
    t.setAttribute('paint-order','stroke'); t.setAttribute('stroke','#0f1115');
    t.setAttribute('stroke-width','3.5'); t.setAttribute('stroke-linejoin','round');
    t.setAttribute('opacity', Math.max(0.12, Math.min(1, 0.30+0.70*n._depth)));
    t.textContent=nm;
    t.style.display = showLabels? '' : 'none';
    t.style.pointerEvents='none';
    g.appendChild(t);

    c.addEventListener('mousemove',ev=>{
      const r=svg.getBoundingClientRect();
      tip.style.display='block';
      tip.style.left=(ev.clientX-r.left+14)+'px';
      tip.style.top=(ev.clientY-r.top+14)+'px';
      const meta=[];
      if(n.tier) meta.push('层 '+n.tier);
      if(n.circle) meta.push(n.circle);
      if(n.msg_total) meta.push('消息 '+n.msg_total+' 条');
      tip.innerHTML='<div class="tipname">'+esc(nm)+'</div>'+
        (meta.length?'<div class="tipmeta">'+esc(meta.join(' · '))+'</div>':'');
    });
    c.addEventListener('mouseleave',()=>{tip.style.display='none';});
    gN.appendChild(g);
    drawn.push({n:n,t:t});
  });

  // ── 标签避让：按深度从近到远，重叠的隐藏远的 ──
  if(showLabels){
    const boxes=[];
    drawn.slice().sort((a,b)=>a.n._p.z-b.n._p.z).forEach(d=>{
      if(d.t.style.display==='none') return;
      let bb; try{ bb=d.t.getBBox(); }catch(e){ return; }
      if(!bb.width) return;
      let hit=false;
      for(const q of boxes){ if(bb.x<q.x+q.w&&bb.x+bb.width>q.x&&bb.y<q.y+q.h&&bb.y+bb.height>q.y){hit=true;break;} }
      if(hit){ d.t.style.display='none'; }
      else boxes.push({x:bb.x,y:bb.y,w:bb.width,h:bb.height});
    });
  }
}
function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

// ── 交互 ──
let dragging=false,lastX=0,lastY=0;
svg.addEventListener('mousedown',ev=>{dragging=true;lastX=ev.clientX;lastY=ev.clientY;svg.classList.add('drag');});
window.addEventListener('mouseup',()=>{dragging=false;svg.classList.remove('drag');});
window.addEventListener('mousemove',ev=>{
  if(!dragging) return;
  panX+=ev.clientX-lastX; panY+=ev.clientY-lastY;
  lastX=ev.clientX;lastY=ev.clientY; render();
});
svg.addEventListener('wheel',ev=>{
  ev.preventDefault();
  zoom=Math.max(0.3,Math.min(3,zoom*(ev.deltaY<0?1.12:0.89)));
  render();
},{passive:false});
svg.addEventListener('click',ev=>{ if(ev.target.tagName!=='circle'){ spin=false; updSpin(); } });
window.toggleSpin=function(){ spin=!spin; updSpin(); };
function updSpin(){ document.getElementById('btnRot').textContent = spin?'暂停旋转':'继续旋转';
                     document.getElementById('btnRot').classList.toggle('on', !spin); }
window.toggleLabels=function(){ showLabels=!showLabels;
  document.getElementById('btnLabels').textContent = showLabels?'隐藏标签':'显示标签';
  render(); };
window.resetView=function(){ yaw=-0.6;pitch=0.35;zoom=1;panX=0;panY=0;render(); };

function loop(){
  if(spin && !dragging){ yaw+=0.0045; render(); }
  requestAnimationFrame(loop);
}

// ── 头部 / 侧栏 ──
document.getElementById('sub').innerHTML = D.subtitle||'';
document.getElementById('stats').innerHTML =
  '<div class="st"><b>'+D.nodes.length+'</b>人</div>'+
  '<div class="st"><b>'+D.links.length+'</b>关系边</div>'+
  '<div class="st"><b>'+(D.rooms||[]).length+'</b>共同群</div>';
document.getElementById('who').innerHTML = D.who_html||'';

window.addEventListener('resize',()=>render());
updSpin();
render();
requestAnimationFrame(loop);
})();
</script>
</body>
</html>
"""


def resolve_center(con, who):
    """把「名字或 person_key」解析成 person_key。

    用户手里通常只有显示名（微信昵称），不是 wxid —— 所以名字必须能直接用。
    重名时取关系边最多的那个（更可能是他要找的枢纽）。
    """
    if not who:
        return None
    row = con.execute(
        "SELECT person_key FROM person WHERE person_key = ?", (who,)).fetchone()
    if row:
        return row[0]
    rows = con.execute(
        "SELECT person_key, display_name FROM person WHERE display_name = ? "
        "OR display_name LIKE ?", (who, f"%{who}%")).fetchall()
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0][0]
    # 重名 → 取关系边最多的
    best, bestn = None, -1
    for pk, _ in rows:
        n = con.execute(
            "SELECT COUNT(*) FROM edge WHERE src = ? OR dst = ?", (pk, pk)).fetchone()[0]
        if n > bestn:
            best, bestn = pk, n
    return best


def write_graph_3d(con, out_path, center_key=None, limit=36, share=False):
    """生成 3D 聚光 HTML。

    center_key 可以是 person_key，也可以是显示名（名字会自动解析）。
    为空时，自动选「关系边数最多的人」作为中心。
    """
    if center_key:
        who = center_key
        center_key = resolve_center(con, who)
        if not center_key:
            raise ValueError(f"库里找不到这个人：{who}")
    if not center_key:
        row = con.execute(
            "SELECT src FROM edge GROUP BY src ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
        if not row:
            raise ValueError("库里没有关系边，无法生成 3D 视图")
        center_key = row[0]

    g = collect_neighbourhood(con, center_key, max_people=limit, share=share)
    if not g["nodes"]:
        raise ValueError("中心人物没有关系数据")

    center = next((n for n in g["nodes"] if n["is_center"]), None)
    center_name = center["display_name"] if center else ""

    sub = (f"中心人物：<b>{center_name}</b> · 关系圈 3D 展开 · "
           f"数据在本机生成，不出本机")

    nbr = [n for n in g["nodes"] if not n["is_center"]]
    lines = [f"<b>{center_name}</b> 在你的圈子里有 <b>{len(nbr)}</b> 个直接关系人。", ""]
    strong = [n for n in nbr if (n.get("msg_total") or 0) > 0]
    if strong:
        lines.append("聊得最多的几个：")
        for n in sorted(strong, key=lambda x: -(x.get("msg_total") or 0))[:5]:
            lines.append(f"· {n['display_name']}（{n['msg_total']} 条）")
        lines.append("")
    lines.append(f"球面上：越靠中心 = 与「{center_name}」的关系越强。")
    who_html = "<br>".join(lines)

    data = {
        "nodes": g["nodes"],
        "links": g["links"],
        "rooms": [{"name": r["name"], "mine": r["mine"]} for r in g.get("rooms", [])],
        "subtitle": sub,
        "who_html": who_html,
    }

    html = _HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False))
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(html, encoding="utf-8")
    return str(p), g


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import core
    con = core.connect()
    path, g = write_graph_3d(con, r"F:\Temp\renpu-3d-focus.html", limit=36, share=True)
    print(f"生成: {path}")
    print(f"  节点 {len(g['nodes'])} / 边 {len(g['links'])}")
    con.close()
