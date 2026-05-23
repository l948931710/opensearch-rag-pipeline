# -*- coding: utf-8 -*-
import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

def test_gemini():
    api_key = os.environ.get("RAG_GEMINI_API_KEY", "")
    model_name = os.environ.get("RAG_LLM_MODEL", "gemini-3.1-flash-lite")
    api_base_url = "https://generativelanguage.googleapis.com/v1beta"
    
    if not api_key:
        print("❌ Error: RAG_GEMINI_API_KEY not found in environment!")
        return

    print(f"Using model: {model_name}")
    print(f"API Key starting with: {api_key[:8]}...")
    
    url = f"{api_base_url}/models/{model_name}:generateContent?key={api_key}"
    
    schema = {
        "type": "OBJECT",
        "properties": {
            "category_l1": {
                "type": "STRING",
                "description": "Must be one of: 'manual', 'sop', 'faq', or 'reference'"
            },
            "category_l2": {
                "type": "STRING",
                "description": "Functional subcategory, e.g., 'finance', 'hr', 'production', 'admin', 'it', 'unknown'"
            },
            "owner_dept": {
                "type": "STRING",
                "description": "Owner department code, e.g., 'it', 'hr', 'admin', 'finance', 'unknown'"
            },
            "permission_level": {
                "type": "STRING",
                "description": "Must be one of: 'public', 'internal', or 'restricted'"
            },
            "kb_type": {
                "type": "STRING",
                "description": "Must be one of: 'public' or 'private'"
            },
            "faq_eligible": {
                "type": "BOOLEAN",
                "description": "Whether the document is fit for automated FAQ extraction"
            },
            "confidence": {
                "type": "NUMBER",
                "description": "Confidence score for the classification between 0.00 and 1.00"
            },
            "llm_risk_level": {
                "type": "STRING",
                "description": "Content-level security risk rating: 'low', 'medium', or 'high'"
            },
            "summary": {
                "type": "STRING",
                "description": "Concise 100-character semantic summary"
            }
        },
        "required": [
            "category_l1", "category_l2", "owner_dept", "permission_level", 
            "kb_type", "faq_eligible", "confidence", "llm_risk_level", "summary"
        ]
    }
    
    sample_text = """
    车间安全生产操作规程 (SOP-PROD-2026)
    本规程规定了机械加车间所有生产操作员的安全守则。
    1. 进入车间必须佩戴安全帽和绝缘手套。
    2. 严禁酒后上岗操作切割机或打磨机。
    3. 发生紧急故障时需立即按下红色急停按钮，并报告生产主管。
    """
    
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": f"Analyze this corporate document and classify its metadata with high precision:\n\n{sample_text}"
                    }
                ]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,
            "temperature": 0.1
        }
    }
    
    headers = {"Content-Type": "application/json"}
    
    print("Sending request to Gemini API...")
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        print(f"Status Code: {resp.status_code}")
        if resp.status_code != 200:
            print(f"Response: {resp.text}")
            return
            
        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            print("❌ Error: No candidates in response.")
            print(data)
            return
            
        text_content = candidates[0]["content"]["parts"][0]["text"]
        result = json.loads(text_content)
        print("✅ Structured Output Parsed Successfully:")
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"❌ Connection or Parsing Error: {e}")

if __name__ == "__main__":
    test_gemini()
