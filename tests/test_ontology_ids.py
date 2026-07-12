# -*- coding: utf-8 -*-
"""ontology/ids.py —— ULID 铸造与 canonical_ref 展示号（纯函数，零外部依赖）。"""
import pytest

from opensearch_pipeline.ontology import ids
from opensearch_pipeline.ontology.ids import (
    ULID_LEN,
    format_ref,
    new_ulid,
    ulid_timestamp_ms,
)

_CROCKFORD = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_ulid_shape_and_alphabet():
    u = new_ulid()
    assert len(u) == ULID_LEN
    assert set(u) <= _CROCKFORD
    # 130 位容量装 128 位值 → 首字符只可能 0–7
    assert u[0] in "01234567"


def test_ulid_uniqueness_bulk():
    batch = {new_ulid() for _ in range(10_000)}
    assert len(batch) == 10_000


def test_ulid_timestamp_roundtrip(monkeypatch):
    fixed_ms = 1_800_000_000_123  # 2027-01-15 前后的一个毫秒值
    monkeypatch.setattr(ids.time, "time_ns", lambda: fixed_ms * 1_000_000)
    assert ulid_timestamp_ms(new_ulid()) == fixed_ms


def test_ulid_cross_millisecond_ordering(monkeypatch):
    """跨毫秒字典序有序（同毫秒内不承诺单调，只承诺唯一）。"""
    monkeypatch.setattr(ids.time, "time_ns", lambda: 1_000 * 1_000_000)
    early = new_ulid()
    monkeypatch.setattr(ids.time, "time_ns", lambda: 2_000 * 1_000_000)
    late = new_ulid()
    assert early < late


@pytest.mark.parametrize("bad", ["", "SHORT", "0" * 25, "0" * 27])
def test_ulid_decode_rejects_bad_length(bad):
    with pytest.raises(ValueError, match="长度"):
        ulid_timestamp_ms(bad)


@pytest.mark.parametrize("ch", ["I", "L", "O", "U", "i", "!"])
def test_ulid_decode_rejects_bad_alphabet(ch):
    with pytest.raises(ValueError, match="非法 ULID 字符"):
        ulid_timestamp_ms(ch * ULID_LEN)


@pytest.mark.parametrize(
    "type_code,seq_no,expect",
    [
        ("P", 123, "FLP-P-000123"),
        ("S", 1, "FLP-S-000001"),
        ("MT", 42, "FLP-MT-000042"),
        ("p", 7, "FLP-P-000007"),            # 类型码统一大写
        ("CR", 1_234_567, "FLP-CR-1234567"),  # 超 6 位自然增长不截断
    ],
)
def test_format_ref(type_code, seq_no, expect):
    assert format_ref(type_code, seq_no) == expect


@pytest.mark.parametrize("seq_no", [0, -1])
def test_format_ref_rejects_nonpositive_seq(seq_no):
    with pytest.raises(ValueError, match="正整数"):
        format_ref("P", seq_no)


@pytest.mark.parametrize("type_code", ["", "P-X", "类型"])
def test_format_ref_rejects_bad_type_code(type_code):
    with pytest.raises(ValueError, match="类型码"):
        format_ref(type_code, 1)
