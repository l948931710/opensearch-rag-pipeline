import os
import requests
from dotenv import load_dotenv

load_dotenv()

api_key = os.environ.get("RAG_DASHSCOPE_API_KEY")
model = os.environ.get("RAG_EMBEDDING_MODEL", "text-embedding-v3")
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

print(f"API Key: {api_key[:6]}...{api_key[-4:] if api_key else 'None'}")
print(f"Model: {model}")

# Test 1: With dimensions
url = f"{base_url}/embeddings"
payload1 = {
    "model": model,
    "input": ["测试文本1", "测试文本2"],
    "dimensions": 768
}
headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json"
}

print("\n--- Test 1: With dimensions: 768 ---")
try:
    resp = requests.post(url, json=payload1, headers=headers)
    print(f"Status Code: {resp.status_code}")
    print(f"Response: {resp.text}")
except Exception as e:
    print(f"Error: {e}")

# Test 2: Without dimensions
payload2 = {
    "model": model,
    "input": ["测试文本1", "测试文本2"]
}
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
