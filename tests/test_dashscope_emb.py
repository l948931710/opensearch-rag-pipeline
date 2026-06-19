import os
import requests
from dotenv import load_dotenv


def main():
    load_dotenv()

    api_key = os.environ.get("RAG_DASHSCOPE_API_KEY")
    model = os.environ.get("RAG_EMBEDDING_MODEL", "text-embedding-v3")
    base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # 不输出 API key 的任何片段
    print(f"DASHSCOPE_API_KEY configured: {'yes' if api_key else 'no'}")
    print(f"Model: {model}")

    url = f"{base_url}/embeddings"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Test 1: With dimensions
    payload1 = {"model": model, "input": ["测试文本1", "测试文本2"], "dimensions": 768}
    print("\n--- Test 1: With dimensions: 768 ---")
    try:
        resp = requests.post(url, json=payload1, headers=headers)
        print(f"Status Code: {resp.status_code}")
        print(f"Response: {resp.text}")
    except Exception as e:
        print(f"Error: {e}")

    # Test 2: Without dimensions
    payload2 = {"model": model, "input": ["测试文本1", "测试文本2"]}
    print("\n--- Test 2: Without dimensions ---")
    try:
        resp = requests.post(url, json=payload2, headers=headers)
        print(f"Status Code: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            emb_len = len(data["data"][0]["embedding"])
            print(f"Success! Embedding dimension: {emb_len}")
        else:
            print(f"Response: {resp.text}")
    except Exception as e:
        print(f"Error: {e}")


# 手工调试脚本（非 pytest 用例）：所有副作用仅在直接运行时执行，
# import/collection 期不读凭证、不发网络请求、无副作用 → clean env collect-empty。
if __name__ == "__main__":
    main()
