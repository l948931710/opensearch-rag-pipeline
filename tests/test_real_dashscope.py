import os
import sys

# Ensure project root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from opensearch_pipeline.config import get_config
from opensearch_pipeline.pipeline_nodes import run_gemini_classification

def main():
    print("Loading configuration...")
    config = get_config()
    print(f"API Base URL: {config.llm.api_base_url}")
    print(f"LLM Model: {config.llm.model}")
    # Safe mask for API key printing
    key = config.llm.api_key
    masked_key = f"{key[:6]}...{key[-4:]}" if len(key) > 10 else "invalid"
    print(f"API Key: {masked_key}")
    
    test_text = (
        "车间流水线操作安全规范说明书\n"
        "第一章：基本安全规范\n"
        "1. 所有操作人员在进入车间前必须穿戴整齐劳动保护用品，如安全帽、防护眼镜、工作服 and 安全鞋等。\n"
        "2. 严禁酒后上岗，工作期间不得嬉戏打闹，严格遵守岗位操作规程。"
    )
    
    models_to_test = ["qwen3.6-plus", "qwen-plus", "qwen-turbo", "qwen-max"]
    
    for model in models_to_test:
        print(f"\nCalling DashScope API / {model} model...")
        try:
            result = run_gemini_classification(
                text=test_text,
                model_name=model,
                api_key=config.llm.api_key,
                api_base_url=config.llm.api_base_url
            )
            print(f"Success with {model}! Structured Classification Output:")
            import json
            print(json.dumps(result, indent=2, ensure_ascii=False))
            break
        except Exception as e:
            print(f"Error calling model {model}: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
