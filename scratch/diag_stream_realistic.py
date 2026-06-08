# -*- coding: utf-8 -*-
"""
diag_stream_realistic.py — 模拟"真实生成"预览：逐字流式一段回答 + 定稿 + 反馈按钮。
改完流式模板后，导入拿到新模板 id，跑这个脚本给自己发一张"接近真实"的卡，目测打字机+按钮。

  DINGTALK_CLIENT_ID=... DINGTALK_CLIENT_SECRET=... \
  DINGTALK_STREAM_CARD_TEMPLATE_ID=<模板id> DINGTALK_STAFF_ID=<你的staffId> DINGTALK_STREAM_KEY=content \
  python scratch/diag_stream_realistic.py
"""
import os, time, uuid, requests
BASE="https://api.dingtalk.com"
CID=os.environ["DINGTALK_CLIENT_ID"]; SEC=os.environ["DINGTALK_CLIENT_SECRET"]
TID=os.environ["DINGTALK_STREAM_CARD_TEMPLATE_ID"]; STAFF=os.environ["DINGTALK_STAFF_ID"]
KEY=os.environ.get("DINGTALK_STREAM_KEY","content")
def H(t): return {"x-acs-dingtalk-access-token":t,"Content-Type":"application/json"}
ANSWER=(
"U8+ 成品仓库的主要业务操作流程如下：\n\n**一、生产入库**\n"
"1. 生产完工后，成品仓库按机台号用 PDA 扫码入库，系统生成「待检入库单」。\n"
"2. 品质部对待检库成品进行检验。\n3. 检验合格后，依据合格检验单生成「产成品入库单」。\n"
"   路径：业务工作 → 供应链 → 库存管理 → 产成品入库单。\n\n**二、销售出库**\n"
"1. 业务部下达销售订单，仓库拣货、PDA 扫码出库。\n2. 系统生成「销售出库单」并扣减库存。\n\n"
"**三、库存盘点**\n定期发起盘点任务，PDA 扫码盘点，系统自动生成盈亏调整单。")
def token(): return requests.post(f"{BASE}/v1.0/oauth2/accessToken",json={"appKey":CID,"appSecret":SEC},timeout=10).json()["accessToken"]
def create(t,o):
    p={"cardTemplateId":TID,"outTrackId":o,"callbackType":"HTTP","userIdType":1,
       "cardData":{"cardParamMap":{"title":"U8+成品仓库怎么操作","question":"U8+成品仓库怎么操作",KEY:"",
          "sources":"1. 富岭U8+成品仓库操作手册.docx > 1.1主要业务流程（相关度 0.94）",
          "sources_text":"1. 富岭U8+成品仓库操作手册.docx > 1.1主要业务流程（相关度 0.94）",
          "meta":"模型: qwen3.6-plus","feedback_status":"","is_answer_done":""}},
       "openSpaceId":f"dtv1.card//im_robot.{STAFF}","userId":STAFF,
       "imRobotOpenDeliverModel":{"spaceType":"IM_ROBOT","robotCode":CID},
       "imRobotOpenSpaceModel":{"supportForward":True},
       "privateData":{STAFF:{"cardParamMap":{"message_id":o}}}}
    r=requests.post(f"{BASE}/v1.0/card/instances/createAndDeliver",json=p,headers=H(t),timeout=10)
    try: return r.status_code, all(d.get("success") for d in r.json()["result"]["deliverResults"])
    except: return r.status_code, False
def stream(t,o,c,fin=False):
    return requests.put(f"{BASE}/v1.0/card/streaming",json={"outTrackId":o,"guid":str(uuid.uuid4()),"key":KEY,"content":c,"isFull":True,"isFinalize":fin,"isError":False},headers=H(t),timeout=10).status_code
def upd(t,o,m): return requests.put(f"{BASE}/v1.0/card/instances",json={"outTrackId":o,"cardData":{"cardParamMap":m}},headers=H(t),timeout=10).status_code
def main():
    t=token(); o=uuid.uuid4().hex
    sc,ok=create(t,o); print("create:",sc,"delivered:",ok)
    if not ok: return
    time.sleep(1.0)
    n=len(ANSWER); step=max(1,n//18); i=0; fail=0
    while i<n:
        i=min(n,i+step)
        if stream(t,o,ANSWER[:i])!=200: fail+=1
        time.sleep(0.5)
    # 关键顺序：先 update_card_data（meta 页脚带耗时，全量避免覆盖）→ 再 finalize（content 写回，故不空白）
    SRC="1. 富岭U8+成品仓库操作手册.docx > 1.1主要业务流程（相关度 0.94）"
    upd(t,o,{"title":"U8+成品仓库怎么操作","question":"U8+成品仓库怎么操作",KEY:ANSWER,
             "sources":SRC,"sources_text":SRC,"meta":"模型: qwen3.6-plus | 耗时: 9.5s","feedback_status":"","is_answer_done":""})
    # update 与 finalize 背靠背（与真实代码一致），尽量缩短中间的重渲染闪烁
    print("finalize:",stream(t,o,ANSWER,fin=True),"帧失败:",f"{fail}/18")
if __name__=="__main__": main()
