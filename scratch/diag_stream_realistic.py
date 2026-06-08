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
    # B2：sources/meta 页脚置空，来源+耗时由定稿帧拼进 content（与真机代码一致）
    p={"cardTemplateId":TID,"outTrackId":o,"callbackType":"HTTP","userIdType":1,
       "cardData":{"cardParamMap":{"title":"U8+成品仓库怎么操作","question":"U8+成品仓库怎么操作",KEY:"",
          "sources":"","sources_text":"","meta":"","feedback_status":"","is_answer_done":""}},
       "openSpaceId":f"dtv1.card//im_robot.{STAFF}","userId":STAFF,
       "imRobotOpenDeliverModel":{"spaceType":"IM_ROBOT","robotCode":CID},
       "imRobotOpenSpaceModel":{"supportForward":True},
       "privateData":{STAFF:{"cardParamMap":{"message_id":o}}}}
    r=requests.post(f"{BASE}/v1.0/card/instances/createAndDeliver",json=p,headers=H(t),timeout=10)
    try: return r.status_code, all(d.get("success") for d in r.json()["result"]["deliverResults"])
    except: return r.status_code, False
def stream(t,o,c,fin=False):
    return requests.put(f"{BASE}/v1.0/card/streaming",json={"outTrackId":o,"guid":str(uuid.uuid4()),"key":KEY,"content":c,"isFull":True,"isFinalize":fin,"isError":False},headers=H(t),timeout=10).status_code
def main():
    t=token(); o=uuid.uuid4().hex
    t0=time.time()
    sc,ok=create(t,o); print("create:",sc,"delivered:",ok)
    if not ok: return
    time.sleep(1.0)
    n=len(ANSWER); step=max(1,n//18); i=0; fail=0
    while i<n:
        i=min(n,i+step)
        if stream(t,o,ANSWER[:i])!=200: fail+=1
        time.sleep(0.5)
    # B2（与真实代码一致）：定稿帧把 参考来源 + "模型 ｜ 检索·生成"页脚 按序拼进正文末尾 →
    # 顺序 答案→来源→页脚，落最底下（紧挨按钮）、灰色缩进、不闪不空白。不调 update_card_data。
    # 页脚显示分段耗时（生成=模型输出延迟）；预览里用脚本自身的近似值。
    elapsed=time.time()-t0
    gen=elapsed-0.8  # 模拟：检索≈0.8s，其余算作生成
    SRC="富岭U8+成品仓库操作手册.docx > 1.1主要业务流程（相关度 0.94）"
    final=ANSWER+f"\n\n📚 **参考来源**\n1. {SRC}\n\n> 模型: qwen3.6-plus ｜ 检索 0.8s · 生成 {gen:.1f}s"
    print("finalize:",stream(t,o,final,fin=True),"帧失败:",f"{fail}/18")
if __name__=="__main__": main()
