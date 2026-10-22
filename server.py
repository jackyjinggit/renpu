# -*- coding: utf-8 -*-
"""仁谱 Renpu 本地 API —— agent 接入点（仅绑 127.0.0.1，不对外暴露）

设计取舍（为什么不裸开 HTTP 给局域网）：
  人际数据是 PII 红线区。终端 = 蒋平本人；API = 本机 agent（WorkBuddy / 脚本 / 其他窗）。
  绑 127.0.0.1 + 无鉴权是刻意的：同机同用户，不构成新的暴露面；
  跨机访问 = 先过防火墙/SSH，是明确的动作而非默认状态。

用法：
  python server.py --port 8787
  curl http://127.0.0.1:8787/api/people?days=30
  curl http://127.0.0.1:8787/api/who?name=xxx
  curl http://127.0.0.1:8787/api/search?q=黑客松
  curl -X POST http://127.0.0.1:8787/api/note  -d '{"text":"...","name":"xxx","kind":"promise"}'
"""
import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import core
from config import DEFAULT_PORT


def _person_rows(con, limit):
    rows = [dict(r) for r in con.execute(
        "SELECT person_key, display_name, tier, circle, org, msg_total, msg_d30, last_active "
        "FROM person ORDER BY last_active DESC LIMIT ?", (limit,)).fetchall()]
    for r in rows:
        r["last_seen"] = time.strftime("%Y-%m-%d", time.localtime(r["last_active"])) if r["last_active"] else None
        r.pop("last_active")
    return rows


def api_stats(con):
    return core.stats(con)


def api_people(con, days=None, q=None, limit=30):
    where, vals = [], []
    if days:
        where.append("last_active > ?"); vals.append(time.time() - days * 86400)
    if q:
        where.append("(display_name LIKE ? OR real_name LIKE ?)"); vals += [f"%{q}%", f"%{q}%"]
    sql = ("SELECT person_key, display_name, tier, circle, org, msg_total, msg_d30, last_active "
           "FROM person" + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY last_active DESC LIMIT ?")
    rows = [dict(r) for r in con.execute(sql, vals + [limit]).fetchall()]
    for r in rows:
        r["last_seen"] = time.strftime("%Y-%m-%d", time.localtime(r["last_active"])) if r["last_active"] else None
        r["tags"] = core.person_tags(con, r["person_key"])
        r.pop("last_active")
    return {"count": len(rows), "rows": rows}


def api_who(con, name, limit=30):
    p = core.get_person(con, name)
    if not p:
        return {"found": False, "query": name,
                "candidates": [r[0] for r in con.execute(
                    "SELECT display_name FROM person WHERE display_name LIKE ? LIMIT 8", (f"%{name}%",))]}
    pk = p["person_key"]
    ident = con.execute("SELECT * FROM identity WHERE person_key=?", (pk,)).fetchone()
    msgs = [dict(r) for r in con.execute(
        "SELECT ts, dir, snippet FROM msg_index WHERE conv_key=? ORDER BY ts DESC LIMIT ?", (pk, limit)).fetchall()]
    for m in msgs:
        m["when"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["ts"]))
        m["who"] = "我" if m["dir"] else (con.execute(
            "SELECT display_name FROM person WHERE person_key=?", (m["sender"],)).fetchone() or ["?"])[0]
        m.pop("ts"); m.pop("dir"); m.pop("sender")
    return {"found": True, "profile": dict(p), "tags": core.person_tags(con, pk),
            "identity": dict(ident) if ident else None, "recent": msgs,
            "rooms": [r[0].split(":", 1)[-1] for r in con.execute(
                "SELECT room_key FROM room_member WHERE person_key=?", (pk,)).fetchall()],
            "edges": [dict(r) for r in con.execute(
                "SELECT dst AS to, kind, weight FROM edge WHERE src=? ORDER BY weight DESC LIMIT 15", (pk,)).fetchall()],
            "todos": [dict(r) for r in con.execute(
                "SELECT todo_id, kind, title, due FROM todo WHERE person_key=? AND status='open'", (pk,)).fetchall()],
            "events": [dict(r) for r in con.execute(
                "SELECT kind, ts, title, detail FROM event WHERE person_key=? ORDER BY ts DESC LIMIT 10", (pk,)).fetchall()]}


def api_search(con, q, limit=30):
    rows = [dict(r) for r in con.execute(
        "SELECT snippet, person_key, ts FROM msg_fts WHERE msg_fts MATCH ? ORDER BY ts DESC LIMIT ?",
        (q.replace('"', '""'), limit)).fetchall()]
    for r in rows:
        r["when"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"]))
        nm = con.execute("SELECT display_name FROM person WHERE person_key=?", (r["person_key"],)).fetchone()
        r["who"] = nm[0] if nm else r["person_key"]
        r.pop("ts"); r.pop("person_key")
    return {"query": q, "count": len(rows), "rows": rows}


def api_feed(con, days=7, limit=50):
    rows = [dict(r) for r in con.execute(
        "SELECT m.ts, m.dir, m.snippet, m.sender FROM msg_index m WHERE m.ts>? ORDER BY m.ts DESC LIMIT ?",
        (time.time() - days * 86400, limit)).fetchall()]
    for r in rows:
        r["when"] = time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))
        nm = con.execute("SELECT display_name FROM person WHERE person_key=?", (r["sender"],)).fetchone()
        r["who"] = "我" if r["dir"] else (nm[0] if nm else r["sender"])
        r.pop("ts"); r.pop("dir"); r.pop("sender")
    return {"days": days, "count": len(rows), "rows": rows}


def api_rooms(con, limit=30):
    rows = [dict(r) for r in con.execute(
        "SELECT room_key, name, member_cnt, msg_total, last_active FROM room ORDER BY msg_total DESC LIMIT ?",
        (limit,)).fetchall()]
    for r in rows:
        r["last_seen"] = time.strftime("%Y-%m-%d", time.localtime(r["last_active"])) if r["last_active"] else None
        r.pop("last_active")
    return {"count": len(rows), "rows": rows}


def api_edges(con, limit=40):
    return {"count": limit, "rows": [dict(r) for r in con.execute(
        "SELECT src, dst, kind, weight, src_name, dst_name FROM ("
        "SELECT e.src, e.dst, e.kind, e.weight, s.display_name AS src_name, d.display_name AS dst_name "
        "FROM edge e LEFT JOIN person s ON s.person_key=e.src LEFT JOIN person d ON d.person_key=e.dst) "
        "ORDER BY weight DESC LIMIT ?", (limit,)).fetchall()]}


def api_todos(con):
    return {"rows": [dict(r) for r in con.execute(
        "SELECT t.todo_id, t.title, t.kind, t.due, p.display_name FROM todo t "
        "LEFT JOIN person p ON p.person_key=t.person_key WHERE t.status='open' ORDER BY t.due").fetchall()]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        con = core.connect()
        try:
            if u.path in ("/", "/api"):
                self._send({"service": "renpu-仁谱", "version": "0.1",
                            "endpoints": ["stats", "people", "who", "search", "feed", "rooms", "edges", "todos"]})
            elif u.path == "/api/stats":
                self._send(api_stats(con))
            elif u.path == "/api/people":
                self._send(api_people(con, days=int(q["days"]) if "days" in q else None,
                                      q=q.get("q"), limit=int(q.get("limit", 30))))
            elif u.path == "/api/who":
                self._send(api_who(con, q.get("name", ""), int(q.get("limit", 30))))
            elif u.path == "/api/search":
                self._send(api_search(con, q.get("q", ""), int(q.get("limit", 30))))
            elif u.path == "/api/feed":
                self._send(api_feed(con, int(q.get("days", 7)), int(q.get("limit", 50))))
            elif u.path == "/api/rooms":
                self._send(api_rooms(con, int(q.get("limit", 30))))
            elif u.path == "/api/edges":
                self._send(api_edges(con, int(q.get("limit", 40))))
            elif u.path == "/api/todos":
                self._send(api_todos(con))
            else:
                self._send({"error": "no such endpoint", "path": u.path}, 404)
        except Exception as e:
            self._send({"error": str(e)}, 500)
        finally:
            con.close()

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n) or b"{}") if n else {}
        con = core.connect()
        try:
            if u.path == "/api/note":
                p = core.get_person(con, payload.get("name", "")) if payload.get("name") else None
                if payload.get("todo"):
                    core.add_todo(con, payload["text"], p["person_key"] if p else None,
                                  kind=payload.get("kind", "followup"))
                else:
                    core.add_event(con, p["person_key"] if p else None,
                                   payload.get("kind", "ai_note"), int(time.time()),
                                   title=payload.get("text"), source="agent")
                con.commit()
                self._send({"ok": True, "kind": "todo" if payload.get("todo") else "event"})
            elif u.path == "/api/todo/close":
                con.execute("UPDATE todo SET status='done', closed_at=? WHERE todo_id=?",
                            (time.time(), payload.get("todo_id")))
                con.commit()
                self._send({"ok": True})
            else:
                self._send({"error": "no such endpoint"}, 404)
        except Exception as e:
            self._send({"error": str(e)}, 500)
        finally:
            con.close()


def serve(port=DEFAULT_PORT):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"仁谱 Renpu API 已启动 → http://127.0.0.1:{port}/api  （仅本机 · Ctrl+C 停）")
    srv.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve(ap.parse_args().port)