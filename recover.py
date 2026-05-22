import json

base_file = "opensearch_pipeline/pipeline_nodes.py"
with open(base_file, "r") as f:
    content = f.read()

def apply_replace(target_content, replacement_content):
    global content
    if target_content in content:
        content = content.replace(target_content, replacement_content)
        return True
    return False

transcript_path = "/Users/laijunchen/.gemini/antigravity/brain/80f081aa-d154-4ee3-8cd4-75d158dda2db/.system_generated/logs/transcript.jsonl"
with open(transcript_path) as f:
    for line in f:
        try:
            data = json.loads(line)
            if data.get("type") == "PLANNER_RESPONSE":
                for t in data.get("tool_calls", []):
                    args = t.get("args", {})
                    if "pipeline_nodes.py" in args.get("TargetFile", ""):
                        if t["name"] == "replace_file_content":
                            # We don't use lines because whitespace might differ if we replace by string directly, 
                            # but string replacement is exact for TargetContent
                            tc = args["TargetContent"]
                            rc = args["ReplacementContent"]
                            if apply_replace(tc, rc):
                                print("Applied replace_file_content")
                            else:
                                print("FAILED replace_file_content")
                        elif t["name"] == "multi_replace_file_content":
                            for chunk in args.get("ReplacementChunks", []):
                                tc = chunk["TargetContent"]
                                rc = chunk["ReplacementContent"]
                                if apply_replace(tc, rc):
                                    print("Applied multi_replace_file_content chunk")
                                else:
                                    print("FAILED multi_replace_file_content chunk")
        except Exception:
            pass

with open("recovered_pipeline_nodes.py", "w") as f:
    f.write(content)
