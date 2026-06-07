"""Validate the PII severity change: phone/email -> medium -> REDACT (mask + keep);
cn_id_card/secret -> high -> QUARANTINE. Pure regex logic, runs offline (simulate_db)."""
import sys
sys.path.insert(0, ".")
from opensearch_pipeline.pipeline_nodes import node_detect_sensitive, node_redact_or_quarantine

def make(doc_id, text):
    return {"doc_id": doc_id, "version_no": 2, "text": text, "llm_risk_level": "low",
            "blocks": [{"block_type": "paragraph", "text": text}]}

docs = [
    make("PHONE", "现场人员请拨打 13666815055 报修，由电工处理。"),
    make("EMAIL", "如有疑问请联系 zhang.san@fuling.com 协助处理。"),
    make("IDCARD", "经办人 110101199003078888 已登记在册。"),
    make("BOTH",  "经办人 110101199003078888，电话 13666815096。"),
    make("CLEAN", "本作业指导书规定了打样流程的标准操作步骤。"),
]
ctx = {"canonicals": docs, "simulate": True, "simulate_db": True, "simulate_api": True}
node_detect_sensitive(ctx)
node_redact_or_quarantine(ctx)

by = {d["doc_id"]: d for d in docs}
for d in docs:
    print(f"  {d['doc_id']:7} risk={d.get('risk_level'):6} action={str(d.get('redaction_action')):10} "
          f"text={ (d.get('redacted_text') or '')[:46] !r}")

# Phone-only -> medium -> REDACTED, masked
assert by["PHONE"]["risk_level"] == "medium", by["PHONE"]["risk_level"]
assert by["PHONE"]["redaction_action"] == "REDACTED", by["PHONE"]["redaction_action"]
assert "13666815055" not in (by["PHONE"]["redacted_text"] or ""), "phone NOT masked!"
# Email-only -> medium -> REDACTED, masked
assert by["EMAIL"]["redaction_action"] == "REDACTED", by["EMAIL"]
assert "zhang.san@fuling.com" not in (by["EMAIL"]["redacted_text"] or ""), "email NOT masked!"
# ID card -> high -> QUARANTINE
assert by["IDCARD"]["risk_level"] == "high", by["IDCARD"]["risk_level"]
assert by["IDCARD"]["redaction_action"] == "QUARANTINE", by["IDCARD"]
# ID card + phone -> high dominates -> QUARANTINE
assert by["BOTH"]["redaction_action"] == "QUARANTINE", by["BOTH"]
# Clean -> not quarantined
assert by["CLEAN"]["redaction_action"] in ("CLEAN", "REDACTED"), by["CLEAN"]
print("\nPII POLICY TEST PASSED ✅  (phone/email → redact+mask+keep; id-card → quarantine)")
