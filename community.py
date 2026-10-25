# -*- coding: utf-8 -*-
"""仁谱 · 社区检测（零依赖）

为什么需要它：
  群共现天然形成社区——同一批人反复出现在同几个群里，就是一个「圈子」。
  没有社区划分时，图上所有节点颜色都一样，读者只能自己盯着线猜分组；
  有了社区，一眼能看出「哪几个人是一伙的」。

算法：标签传播（Label Propagation）
  - 随机序遍历节点，每个节点取邻居里出现最多的标签作为自己的标签
  - 迭代若干轮直到标签稳定
  - 零依赖、O(边数 × 轮数)，对十万级边也够快
  - 社区归属和大小一并给出，便于 UI 排序和着色
"""
from __future__ import annotations

import json
import random
from collections import defaultdict


def detect_communities(links, weights=None, max_iters=20, seed=17):
    """对边列表做社区检测。

    参数
      links   : [{"src": key, "dst": key, "weight": float}, ...]
      weights : 可选，权重字典 {(src,dst): w}；缺省用 link 里的 weight
    返回
      { person_key: {"community": int, "size": int, "rank": int} }
    """
    if not links:
        return {}

    # 无向邻接表
    adj = defaultdict(list)
    for l in links:
        s, t = l["src"], l["dst"]
        if s == t:
            continue
        w = float(l.get("weight") or 1.0)
        adj[s].append((t, w))
        adj[t].append((s, w))

    nodes = list(adj.keys())
    if not nodes:
        return {}

    # 初始标签 = 节点自身
    label = {n: n for n in nodes}
    rng = random.Random(seed)
    order = list(nodes)

    for _ in range(max_iters):
        rng.shuffle(order)
        changed = False
        for n in order:
            neigh = adj.get(n) or []
            if not neigh:
                continue
            # 邻居标签加权票数
            votes = defaultdict(float)
            for m, w in neigh:
                votes[label[m]] += w
            best = max(votes.items(), key=lambda kv: (kv[1], -len(str(kv[0]))))
            if votes[best[0]] > votes.get(label[n], 0.0):
                label[n] = best[0]
                changed = True
        if not changed:
            break

    # 归一化社区 id（按社区大小排序，大社区在前）
    groups = defaultdict(list)
    for n, lab in label.items():
        groups[lab].append(n)
    ordered = sorted(groups.values(), key=lambda g: (-len(g), sorted(g)[0]))
    remap = {}
    for i, g in enumerate(ordered):
        for n in g:
            remap[n] = i

    out = {}
    for i, g in enumerate(ordered):
        for n in g:
            out[n] = {"community": i, "size": len(g), "rank": i}
    return out


def community_of_rooms(room_members):
    """另一种社区定义：按「同群」直接分组（群本身就是天然社区）。

    当群少而大时，标签传播容易把不同群的人混进同一社区；
    这时用「主要群」来定义更符合直觉：一个节点归属它参与最多的那个群。

    room_members: { room_key: [person_key, ...] }
    返回 { person_key: {"community": int, "size": int, "rank": int} }
    """
    if not room_members:
        return {}

    # 统计每人参与的群，按群大小排序（优先归入大群）
    person_rooms = defaultdict(list)
    for rk, members in room_members.items():
        for p in members:
            person_rooms[p].append(rk)

    room_rank = sorted(room_members.keys(), key=lambda r: -len(room_members[r]))
    room_id = {rk: i for i, rk in enumerate(room_rank)}

    assign = {}
    for p, rks in person_rooms.items():
        best = max(rks, key=lambda rk: (len(room_members.get(rk, [])), -room_id.get(rk, 0)))
        assign[p] = best

    groups = defaultdict(list)
    for p, rk in assign.items():
        groups[rk].append(p)

    ordered = sorted(groups.values(), key=lambda g: (-len(g), sorted(g)[0]))
    out = {}
    for i, g in enumerate(ordered):
        for n in g:
            out[n] = {"community": i, "size": len(g), "rank": i}
    return out


def merge_rooms_from_links(links):
    """从边上的 rooms 字段还原「群 → 成员」，用于按群定社区。"""
    room_members = defaultdict(set)
    for l in links:
        raw = l.get("rooms")
        if not raw:
            continue
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                continue
        if not isinstance(raw, (list, tuple)):
            continue
        for rk in raw:
            room_members[rk].add(l["src"])
            room_members[rk].add(l["dst"])
    return {rk: sorted(v) for rk, v in room_members.items()}
