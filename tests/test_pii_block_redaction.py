# -*- coding: utf-8 -*-
import pytest
from opensearch_pipeline.pipeline_nodes import node_redact_or_quarantine
from opensearch_pipeline.extraction.schema import ExtractionResult, ExtractedBlock

def test_block_pii_redaction():
    # Construct a document context with medium risk and sensitive data
    block1 = ExtractedBlock(
        block_type="paragraph",
        text="我的手机号是 13812345678，邮箱是 user@example.com",
        page_num=1,
    )
    block2 = ExtractedBlock(
        block_type="table",
        text="| 姓名 | 电话 | 身份证 |\n| 张三 | 13812345678 | 110101199003072345 |",
        page_num=2,
    )
    
    doc = {
        "doc_id": "TEST_PII_001",
        "version_no": 1,
        "text": "我的手机号是 13812345678，邮箱是 user@example.com，身份证号 110101199003072345",
        "blocks": [block1.to_dict(), block2.to_dict()],
        "risk_level": "medium",
        "sensitive_detected": True,
    }
    
    ctx = {
        "canonicals": [doc]
    }
    
    # Run redaction node
    node_redact_or_quarantine(ctx)
    
    redacted_doc = ctx["canonicals"][0]
    
    # Verify flat text is redacted
    assert "13812345678" not in redacted_doc["redacted_text"]
    assert "user@example.com" not in redacted_doc["redacted_text"]
    assert "110101199003072345" not in redacted_doc["redacted_text"]
    
    # Verify blocks text are also redacted
    redacted_blocks = redacted_doc["blocks"]
    assert "13812345678" not in redacted_blocks[0]["text"]
    assert "user@example.com" not in redacted_blocks[0]["text"]
    assert "13812345678" not in redacted_blocks[1]["text"]
    assert "110101199003072345" not in redacted_blocks[1]["text"]
    
    print("Block PII Redaction test passed successfully!")


def test_secret_like_redaction():
    # Test cases for secret_like with various keys, separators, spacing, and lengths
    test_cases = [
        # Separator: colon, no spaces, short key
        ("pwd:abcdefgh", "pwd:****"),
        # Separator: colon, with spaces, short key
        ("pwd : abcdefgh", "pwd : ****"),
        # Separator: colon, no spaces, medium key
        ("passwd:abcdefgh", "passwd:****"),
        # Separator: colon, with spaces, medium key
        ("passwd : abcdefgh", "passwd : ****"),
        # Separator: colon, no spaces, long key
        ("password:abcdefgh", "password:****"),
        # Separator: colon, with spaces, long key
        ("password  :  abcdefgh", "password  :  ****"),
        # Separator: equals, no spaces
        ("token=abcdefgh", "token=****"),
        # Separator: equals, with spaces
        ("token  =  abcdefgh", "token  =  ****"),
        # Separator: equals, api_key
        ("api-key=abcdefgh", "api-key=****"),
    ]

    for raw, expected in test_cases:
        doc = {
            "doc_id": "TEST_SECRET_001",
            "version_no": 1,
            "text": f"Here is the credential: {raw}",
            "blocks": [],
            "risk_level": "medium",
            "sensitive_detected": True,
        }
        ctx = {"canonicals": [doc]}
        node_redact_or_quarantine(ctx)
        redacted_text = ctx["canonicals"][0]["redacted_text"]
        
        # Verify the credential is redacted exactly as expected without partial leakage
        assert expected in redacted_text, f"Failed for raw='{raw}', got redacted_text='{redacted_text}'"
        assert "abcdefgh" not in redacted_text, f"Vulnerability detected! Secret leaked for raw='{raw}', got redacted_text='{redacted_text}'"

