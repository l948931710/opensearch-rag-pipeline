# -*- coding: utf-8 -*-
import os
import sys
import json
import requests
import numpy as np

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opensearch_pipeline.config import get_config

def get_live_embedding(text: str, api_key: str, model: str, base_url: str) -> list:
    url = f"{base_url}/embeddings"
    payload = {
        "model": model,
        "input": [text],
        "dimensions": 768
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    resp = requests.post(url, json=payload, headers=headers)
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]

def main():
    config = get_config()
    api_key = config.embedding.api_key
    model = config.embedding.model
    base_url = config.embedding.api_base_url
    
    print(f"Model: {model}")
    
    q_text = "新员工试用期满要转正，人事部门和员工本人需要在到期前多少天分别完成什么准备？"
    
    canon_file = "/Users/laijunchen/Downloads/opensearch-rag-pipeline/processing/canonical/hr/eval_hr_manual/v1/content.canonical.json"
    with open(canon_file, "r") as f:
        data = json.load(f)
    
    from opensearch_pipeline.chunker import DocumentChunker
    chunker = DocumentChunker(
        max_chunk_chars=600,
        min_chunk_chars=10,
        overlap_chars=100,
        split_mode="text",
        prepend_title=True,
        prepend_section=True,
        parent_child=True
    )
    metadata = {"title": data.get("title", ""), "owner_dept": "hr", "category_l1": "sop"}
    chunks = chunker.chunk_from_blocks(blocks=data.get("blocks", []), doc_id="eval_hr_manual", version_no=1, metadata=metadata)
    
    target_c = None
    for c in chunks:
        if "试用小结" in c.chunk_text:
            target_c = c
            break
            
    c_text = target_c.chunk_text
    
    print("Calling live DashScope API...")
    q_live_vec = np.array(get_live_embedding(q_text, api_key, model, base_url))
    c_live_vec = np.array(get_live_embedding(c_text, api_key, model, base_url))
    
    sim = np.dot(q_live_vec, c_live_vec) / (np.linalg.norm(q_live_vec) * np.linalg.norm(c_live_vec))
    print(f"\nLive Cosine Similarity: {sim:.6f}")
    
    # Let's compare with cached vectors
    import evaluate_large_corpus_hybrid_sweep as elc
    emb_cache = elc.load_embedding_cache()
    
    import hashlib
    q_hash = hashlib.md5(f"{model}_{q_text}".encode("utf-8")).hexdigest()
    c_hash = hashlib.md5(f"{model}_{c_text}".encode("utf-8")).hexdigest()
    
    q_cached = np.array(emb_cache[q_hash])
    c_cached = np.array(emb_cache[c_hash])
    
    # Are cached vectors identical to live vectors?
    q_diff = np.linalg.norm(q_live_vec - q_cached)
    c_diff = np.linalg.norm(c_live_vec - c_cached)
    print(f"Query vector L2 distance from cache: {q_diff:.6f}")
    print(f"Chunk vector L2 distance from cache: {c_diff:.6f}")
    
    cached_sim = np.dot(q_cached, c_cached) / (np.linalg.norm(q_cached) * np.linalg.norm(c_cached))
    print(f"Cached Cosine Similarity: {cached_sim:.6f}")

if __name__ == "__main__":
    main()
