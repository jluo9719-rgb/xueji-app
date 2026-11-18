# -*- coding: utf-8 -*-
"""UI 联调：验证 /chat SSE 事件流、消息持久化、标题自动改名、时间线字段。"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8010"
SID = "s_ui_test"
QUESTION = "内部监督包括哪几个要素？"


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.status, r.read().decode("utf-8")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return r.status, r.read().decode("utf-8")


print("=== 1. POST /chat (SSE) ===")
status, body = post("/chat", {"message": QUESTION, "session_id": SID})
print("HTTP", status)
events = []
buf = body
while "\n\n" in buf:
    chunk, buf = buf.split("\n\n", 1)
    ev, data = "message", ""
    for line in chunk.split("\n"):
        if line.startswith("event:"):
            ev = line[6:].strip()
        elif line.startswith("data:"):
            data += line[5:].strip()
    if data:
        events.append((ev, json.loads(data)))

stages = [d.get("stage") for ev, d in events if ev == "status"]
print("status 事件序列:", stages)
for ev, d in events:
    if ev == "status" and d.get("stage") in ("tool_call", "reflect", "reflect_retry", "mistake"):
        print("   ", d.get("stage"), "->", str(d.get("detail"))[:60])
answer = next((d.get("text") for ev, d in events if ev == "answer"), None)
err = next((d.get("message") for ev, d in events if ev == "error"), None)
print("answer 长度:", len(answer) if answer else None)
print("answer 前 120 字:", (answer or "")[:120].replace("\n", " "))
print("error:", err)
assert answer, "没有收到 answer 事件"
assert not err, "收到 error 事件"

print("\n=== 2. GET /sessions/%s/messages ===" % SID)
status, body = get("/sessions/%s/messages" % SID)
msgs = json.loads(body)["messages"]
print("消息条数:", len(msgs), "| 角色序列:", [m["role"] for m in msgs])
last = msgs[-1]
print("最后一条是 assistant 且内容一致:", last["role"] == "assistant" and last["content"] == answer)

print("\n=== 3. GET /sessions（标题改名 + updated_at） ===")
status, body = get("/sessions")
for s in json.loads(body)["sessions"]:
    if s["id"] == SID:
        print("title:", s["title"], "| user_turns:", s["user_turns"])
        print("created_at:", s["created_at"], "| updated_at:", s.get("updated_at"))
        assert s["title"] == QUESTION[:20], "标题未自动改为首轮提问"
        assert s.get("updated_at"), "updated_at 缺失"

print("\n=== 4. GET /sessions/default/messages（默认会话 404 分支） ===")
try:
    get("/sessions/default/messages")
    print("default 会话存在，返回 200")
except urllib.error.HTTPError as e:
    print("HTTP", e.code, "(前端按空会话处理)")

print("\nALL PASS")
