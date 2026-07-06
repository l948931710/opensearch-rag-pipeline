# -*- coding: utf-8 -*-
"""text_similarity.py — 文档级近重复检测原语（G19，纯 Python 零依赖）。

背景（工业级审计 G19）：摄取去重此前只有字节级精确哈希（canonical_sha256 +
OSS etag MD5）——同一 SOP 重新导出的 PDF（元数据/时间戳变化、内容不变或微变）
会全量二次入索引，检索时同内容文档分裂打分。C4 以降的大规模语料清洗与
RAGFlow/deepdoc 均以 simhash/MinHash-LSH 做近重复；本模块提供 64 位 simhash
（字符 3-gram，中文友好），node_build_canonical 计算并落
document_version.content_simhash（schema/020），Hamming ≤ 阈值（默认 3）时 WARN。

只做告警不拦截：近重复的 ACL 语义比精确副本更微妙（内容微调可能伴随受众变化），
拦截决策留给人工/后续策略；这与既有跨文档精确去重的 WARN-and-process 默认一致。
"""

import hashlib

# 64 位掩码
_MASK64 = (1 << 64) - 1


def simhash64(text: str) -> int:
    """64 位 simhash（字符 3-gram 加权投票；空白归一后计算，中文无需分词）。

    空/超短文本返回 0（调用方应按"无指纹"处理，不参与比对——与跨文档精确
    去重跳过空 canonical 的先例一致，避免空文档互相假命中）。
    """
    s = "".join((text or "").split())
    if len(s) < 3:
        return 0
    grams: dict = {}
    for i in range(len(s) - 2):
        g = s[i:i + 3]
        grams[g] = grams.get(g, 0) + 1
    votes = [0] * 64
    for g, w in grams.items():
        h = int.from_bytes(hashlib.md5(g.encode("utf-8")).digest()[:8], "big")
        for b in range(64):
            if (h >> b) & 1:
                votes[b] += w
            else:
                votes[b] -= w
    out = 0
    for b in range(64):
        if votes[b] > 0:
            out |= (1 << b)
    return out & _MASK64


def hamming64(a: int, b: int) -> int:
    """两个 64 位指纹的 Hamming 距离。"""
    return bin((a ^ b) & _MASK64).count("1")
