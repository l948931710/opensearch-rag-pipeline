# -*- coding: utf-8 -*-
"""
diag_streaming.py — 钉钉 AI 流式卡片 500 unknownError 诊断脚本

目的：在 content / 模板 / 权限都确认 OK 后仍然 500，逐一隔离剩余变量：
  1) callbackType = STREAM vs HTTP（官方默认 STREAM，我们代码写死 HTTP）
  2) 真实发送的 createAndDeliver / PUT card/streaming 报文与响应（逐字可见）

用法（在能联网 + 有这些环境变量的机器上，本地笔记本即可，api.dingtalk.com 是公网）：
  export DINGTALK_CLIENT_ID=dingdiosbjlkh6ugdgek
  export DINGTALK_CLIENT_SECRET=<你的 AppSecret>
  export DINGTALK_STREAM_CARD_TEMPLATE_ID=<你现在用的流式模板 id>
  export DINGTALK_STAFF_ID=323211422235429212     # 收卡的人（你自己）
  export DINGTALK_STREAM_KEY=content               # 默认 content
  python scratch/diag_streaming.py

会给你自己发 2 张测试卡（STREAM 一张、HTTP 一张），并打印每步的状态码 + body。
看最后的 CONCLUSION：哪种 callbackType 的「stream PUT」返回 200，就是答案。
"""
import os, json, time, uuid, requests

BASE = "https://api.dingtalk.com"
CLIENT_ID = os.environ.get("DINGTALK_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("DINGTALK_CLIENT_SECRET", "")
TEMPLATE_ID = os.environ.get("DINGTALK_STREAM_CARD_TEMPLATE_ID", "")
STAFF_ID = os.environ.get("DINGTALK_STAFF_ID", "")
KEY = os.environ.get("DINGTALK_STREAM_KEY", "content")


def get_token():
    r = requests.post(f"{BASE}/v1.0/oauth2/accessToken",
                      json={"appKey": CLIENT_ID, "appSecret": CLIENT_SECRET}, timeout=10)
    r.raise_for_status()
    return r.json()["accessToken"]


def create_card(token, out_track_id, callback_type):
    payload = {
        "cardTemplateId": TEMPLATE_ID,
        "outTrackId": out_track_id,
        "callbackType": callback_type,
        "userIdType": 1,
        "cardData": {"cardParamMap": {
            "title": "诊断", "question": "诊断", KEY: "",
            "sources_text": "", "meta": "诊断", "feedback_status": "", "is_answer_done": "",
        }},
        "openSpaceId": f"dtv1.card//im_robot.{STAFF_ID}",
        "userId": STAFF_ID,
        "imRobotOpenDeliverModel": {"spaceType": "IM_ROBOT", "robotCode": CLIENT_ID},
        "imRobotOpenSpaceModel": {"supportForward": True},
        "privateData": {STAFF_ID: {"cardParamMap": {"message_id": out_track_id}}},
    }
    r = requests.post(f"{BASE}/v1.0/card/instances/createAndDeliver", json=payload,
                      headers={"x-acs-dingtalk-access-token": token, "Content-Type": "application/json"},
                      timeout=10)
    ok = False
    try:
        ok = all(d.get("success") for d in r.json().get("result", {}).get("deliverResults", []))
    except Exception:
        pass
    return r.status_code, r.text[:300], ok


def stream_update(token, out_track_id, content, finalize=False):
    payload = {"outTrackId": out_track_id, "guid": str(uuid.uuid4()), "key": KEY,
               "content": content, "isFull": True, "isFinalize": finalize, "isError": False}
    r = requests.put(f"{BASE}/v1.0/card/streaming", json=payload,
                     headers={"x-acs-dingtalk-access-token": token, "Content-Type": "application/json"},
                     timeout=10)
    return r.status_code, r.text[:300]


def run(token, callback_type):
    print(f"\n========== callbackType = {callback_type} (key={KEY}) ==========")
    otid = uuid.uuid4().hex
    sc, body, ok = create_card(token, otid, callback_type)
    print(f"[create]  status={sc}  delivered={ok}  body={body}")
    if sc != 200 or not ok:
        print("  (创建就失败，跳过 stream)")
        return callback_type, False
    time.sleep(1.0)
    sc1, b1 = stream_update(token, otid, "正在生成… 第一帧")
    print(f"[stream1] status={sc1}  body={b1}")
    time.sleep(0.6)
    sc2, b2 = stream_update(token, otid, "正在生成… 第一帧\n\n这是定稿全文。", finalize=True)
    print(f"[stream2/finalize] status={sc2}  body={b2}")
    return callback_type, (sc1 == 200 and sc2 == 200)


def main():
    missing = [k for k, v in {"DINGTALK_CLIENT_ID": CLIENT_ID, "DINGTALK_CLIENT_SECRET": CLIENT_SECRET,
                              "DINGTALK_STREAM_CARD_TEMPLATE_ID": TEMPLATE_ID, "DINGTALK_STAFF_ID": STAFF_ID}.items() if not v]
    if missing:
        print("缺少环境变量:", ", ".join(missing)); return
    token = get_token()
    print("token ok:", bool(token), "| template:", TEMPLATE_ID, "| staff:", STAFF_ID, "| key:", KEY)
    results = [run(token, "STREAM"), run(token, "HTTP")]
    print("\n================= CONCLUSION =================")
    for ct, ok in results:
        print(f"  callbackType={ct:6s} → streaming {'✅ 成功' if ok else '❌ 仍失败'}")
    streamed_ok = [ct for ct, ok in results if ok]
    if streamed_ok:
        print(f"\n=> 用 callbackType={streamed_ok[0]} 能流式成功。把代码里流式卡的 callbackType 改成它即可。")
    else:
        print("\n=> 两种都失败 → 不是 callbackType。问题在模板(组件/绑定/isStreaming)或账号/模板归属。"
              "\n   把本次任一 outTrackId 的 create body 和 stream body 发我逐字看。")


if __name__ == "__main__":
    main()
