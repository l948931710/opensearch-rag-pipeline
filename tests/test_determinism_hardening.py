# -*- coding: utf-8 -*-
"""tests/test_determinism_hardening.py — Phase-1 DET bundle.

Closes the determinism findings beyond the chunk path: pin LLM-classification temperature to 0
(stable category → stable chunk routing), pin the VLM funnel call to temperature 0 (stable
routing/captions), and make both caches atomic (a torn cache write must not force a nondeterministic
full re-eval).
"""
import inspect


def test_classifier_temperature_is_deterministic():
    import opensearch_pipeline.pipeline_nodes as pn
    src = inspect.getsource(pn)
    assert '"temperature": 0.1' not in src, "classifier temperature must be pinned to 0, not 0.1"
    assert src.count('"temperature": 0') >= 2, "both DashScope + Gemini classify branches must pin temp=0"


def test_vlm_funnel_pins_temperature_zero():
    import opensearch_pipeline.image_funnel_processor as ifp
    assert "temperature=0" in inspect.getsource(ifp), "VLM funnel must pin temperature=0 for determinism"


def test_build_image_chat_payload_propagates_temperature():
    from opensearch_pipeline.vlm_endpoint import build_image_chat_payload
    compat = build_image_chat_payload("m", "p", "b64", "image/png", True, temperature=0)
    assert compat.get("temperature") == 0
    native = build_image_chat_payload("m", "p", "b64", "image/png", False, temperature=0)
    assert native.get("parameters", {}).get("temperature") == 0


def test_embedding_cache_writes_atomically():
    from opensearch_pipeline.pipeline_nodes import node_generate_embeddings
    assert "os.replace(_tmp, _cache_file)" in inspect.getsource(node_generate_embeddings), (
        "embedding cache must write atomically (temp + os.replace)"
    )


def test_vlm_cache_writes_atomically():
    from opensearch_pipeline.extraction.unified_extractor import UnifiedExtractor
    assert "os.replace(_tmp, cls._vlm_cache_file)" in inspect.getsource(UnifiedExtractor._save_vlm_cache), (
        "VLM cache must write atomically (temp + os.replace)"
    )
