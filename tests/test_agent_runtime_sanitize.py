# -*- coding: utf-8 -*-
"""sanitize.py（agent 持久化前参数脱敏）单测：掩码/嵌套/非串透传/fail-closed 占位。"""
import json

from opensearch_pipeline.agent_runtime.sanitize import sanitize_args, sanitize_args_json


def test_masks_mobile_and_id_card_in_values():
    out = sanitize_args({"note": "联系电话 13812345678", "who": "身份证 330102199001011234"})
    assert "13812345678" not in json.dumps(out, ensure_ascii=False)
    assert "330102199001011234" not in json.dumps(out, ensure_ascii=False)
    assert out["note"].startswith("联系电话 138")           # 掩码保留前缀（138****5678）


def test_nested_dict_and_list_are_walked():
    out = sanitize_args({"rows": [{"phone": "电话 13812345678"}, "邮箱 someone@fuling.com"],
                         "meta": {"deep": {"v": "手机 13998765432"}}})
    s = json.dumps(out, ensure_ascii=False)
    assert "13812345678" not in s and "13998765432" not in s
    assert "someone@fuling.com" not in s


def test_non_string_values_pass_through():
    out = sanitize_args({"qty": 7, "ok": True, "ratio": 0.5, "none": None})
    assert out == {"qty": 7, "ok": True, "ratio": 0.5, "none": None}


def test_non_dict_input_wrapped():
    assert sanitize_args("裸字符串")["_raw"] == "裸字符串"


def test_sanitize_failure_never_stores_raw(monkeypatch):
    """脱敏异常 → 占位符（fail-closed：绝不回退存原文）。"""
    import opensearch_pipeline.pii_patterns as pp
    def _boom(text):
        raise RuntimeError("regex engine down")
    monkeypatch.setattr(pp, "scrub_image_text", _boom)
    out = sanitize_args({"note": "联系电话 13812345678"})
    assert "13812345678" not in json.dumps(out, ensure_ascii=False)
    assert "_sanitize_error" in out


def test_sanitize_args_json_is_valid_json():
    s = sanitize_args_json({"q": "电话 13812345678"})
    assert "13812345678" not in s
    json.loads(s)                                            # 恒为合法 JSON
