#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将误放到 raw/it/ 的 admin 文件移回 raw/admin/"""
import os
import oss2

# 从环境变量读取凭证（DataWorks 节点中由调度框架注入）
ENDPOINT = os.environ.get("RAG_OSS_ENDPOINT", "https://oss-cn-hangzhou.aliyuncs.com")
ACCESS_KEY_ID = os.environ["RAG_OSS_ACCESS_KEY_ID"]
ACCESS_KEY_SECRET = os.environ["RAG_OSS_ACCESS_KEY_SECRET"]
BUCKET_NAME = os.environ.get("RAG_OSS_BUCKET_NAME", "fuling-knowledge-base")

auth = oss2.Auth(ACCESS_KEY_ID, ACCESS_KEY_SECRET)
bucket = oss2.Bucket(auth, ENDPOINT, BUCKET_NAME)

FILES_TO_MOVE = [
    "《核实出入库数据》作业指导书.pdf",
    "《现包工资统计》作业指导书.pdf",
    "《纸质外销发票登记汇总》作业指导书.pdf",
    "《计件工资产量核对》作业指导书.pdf",
    "《费用分类》作业指导书.pdf",
    "《辅料和成品报废》作业指导书.pdf",
    "《钉钉审核货代发票》作业指导书.pdf",
]

for fname in FILES_TO_MOVE:
    src = f"raw/it/{fname}"
    dst = f"raw/admin/{fname}"
    
    # 检查源文件是否存在
    if not bucket.object_exists(src):
        print(f"⏭️  源文件不存在，跳过: {src}")
        continue
    
    # 复制
    bucket.copy_object(BUCKET_NAME, src, dst)
    print(f"✅ 复制: {src} → {dst}")
    
    # 验证目标存在后删除源
    if bucket.object_exists(dst):
        bucket.delete_object(src)
        print(f"   🗑️  已删除源: {src}")
    else:
        print(f"   ⚠️  目标验证失败，保留源: {src}")

print("\n✅ 完成！")
