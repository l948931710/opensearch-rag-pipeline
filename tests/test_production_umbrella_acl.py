# -*- coding: utf-8 -*-
"""Production umbrella ACL: 'production' user-group grants all approved production* sublines.

owner_dept keeps subline granularity (never rewritten); 'production' is the umbrella
USER group that expands — on the FILTER side only — to {production, production_mold,
production_paper_cup, production_thermoforming, ...}. Every other group stays exact-match.
Covers the full requirement matrix incl. fail-closed unknown sublines + a synthetic
dept_internal ACL simulation across each subline.
"""
from opensearch_pipeline import retriever as R

# the APPROVED + live umbrella owners (matches _PRODUCTION_UMBRELLA_OWNERS)
SUBLINES = ["production", "production_mold", "production_paper_cup",
            "production_thermoforming"]
# production_injection / production_papercup are NOT yet approved → must fail closed
UNAPPROVED = ["production_injection", "production_papercup", "production_secret", "production_"]


# ── _expand_groups_to_owners (the single expansion point) ────────────────────
def test_production_umbrella_expands_to_all_sublines():
    owners = set(R._expand_groups_to_owners(["production"]))
    for s in SUBLINES:
        assert s in owners, f"umbrella missing subline {s}"


def test_non_production_group_is_exact_self():
    assert R._expand_groups_to_owners(["quality"]) == ["quality"]
    assert R._expand_groups_to_owners(["hr"]) == ["hr"]


def test_multi_group_ors_umbrella_with_exact():
    owners = set(R._expand_groups_to_owners(["production", "quality"]))
    assert owners == set(R._PRODUCTION_UMBRELLA_OWNERS) | {"quality"}
    assert "hr" not in owners and "finance" not in owners


def test_expand_empty():
    assert R._expand_groups_to_owners([]) == []


def test_unknown_subline_not_in_umbrella_fail_closed():
    owners = set(R._expand_groups_to_owners(["production"]))
    for unknown in UNAPPROVED + ["production_unknown", "productionx"]:
        assert unknown not in owners, f"fail-closed breach: {unknown} granted"


def test_umbrella_owner_set_is_exactly_approved_four():
    assert set(R._PRODUCTION_UMBRELLA_OWNERS) == set(SUBLINES)


# ── _build_permission_filter (HA3 boundary) ──────────────────────────────────
def test_filter_production_contains_every_subline_clause():
    f = R._build_permission_filter(["production"])
    assert '(permission_level="public")' in f
    for s in SUBLINES:
        assert f'owner_dept="{s}"' in f


def test_legacy_dept_string_same_as_list_umbrella():
    # req#8: legacy dept="production" (string) gets the same umbrella as ["production"]
    assert R._build_permission_filter("production") == R._build_permission_filter(["production"])
    assert 'owner_dept="production_mold"' in R._build_permission_filter("production")


def test_non_production_filter_exact_match_unchanged():
    # req#6: quality (and others) stay exact-match — byte-identical to the pre-change form
    f = R._build_permission_filter(["quality"])
    assert f == ('(permission_level="public")'
                 ' OR (permission_level="dept_internal" AND (owner_dept="quality"))')
    assert "production" not in f


def test_multi_group_filter_production_plus_quality():
    f = R._build_permission_filter(["production", "quality"])
    for s in SUBLINES:
        assert f'owner_dept="{s}"' in f
    assert 'owner_dept="quality"' in f
    assert 'owner_dept="hr"' not in f


def test_anonymous_public_only():
    pub = '(permission_level="public")'
    assert R._build_permission_filter(None) == pub
    assert R._build_permission_filter([]) == pub
    assert R._build_permission_filter("") == pub


def test_unrelated_and_malformed_groups_no_production():
    # productionx / preproduction / unknown sublines are not valid USER groups -> public only
    for bad in (["productionx"], ["preproduction"], ["production_mold"], ["production_secret"]):
        f = R._build_permission_filter(bad)
        assert f == '(permission_level="public")', f"{bad} should be public-only, got {f}"


def test_injection_attempt_sanitized_to_public():
    f = R._build_permission_filter(['production" OR "1"="1'])
    # sanitized -> not a valid group -> public only; no raw injection survives
    assert f == '(permission_level="public")'


def test_public_clause_always_present():
    for ud in (None, [], ["production"], ["quality"], ["production", "hr"]):
        assert '(permission_level="public")' in R._build_permission_filter(ud)


# ── audit (fail-closed reporting of unknown production-like owners) ───────────
def test_audit_flags_unknown_production_owners():
    sus = R.audit_production_owner_taxonomy(
        ["production", "production_mold", "production_unknown", "production_injection",
         "productionx", "hr", "quality"])
    assert "production_unknown" in sus and "productionx" in sus
    assert "production_injection" in sus  # not-yet-approved subline → flagged, fail-closed
    assert "production" not in sus and "production_mold" not in sus
    assert "hr" not in sus and "quality" not in sus


def test_audit_clean_taxonomy_returns_empty():
    assert R.audit_production_owner_taxonomy(SUBLINES) == []


# ── synthetic dept_internal ACL simulation (one chunk per subline) ───────────
def _visible(chunk, user_dept):
    """Mirror the HA3 permission predicate using the single expansion source."""
    if chunk["permission_level"] == "public":
        return True
    owners = set(R._expand_groups_to_owners(R._normalize_acl_groups(user_dept)))
    return chunk["permission_level"] == "dept_internal" and chunk["owner_dept"] in owners


def _corpus():
    chunks = [{"permission_level": "dept_internal", "owner_dept": o, "id": o} for o in SUBLINES]
    chunks += [
        {"permission_level": "dept_internal", "owner_dept": "quality", "id": "q"},
        {"permission_level": "dept_internal", "owner_dept": "hr", "id": "h"},
        {"permission_level": "dept_internal", "owner_dept": "production_secret", "id": "x"},  # unknown
        {"permission_level": "public", "owner_dept": "production", "id": "pub"},
    ]
    return chunks


def test_sim_production_user_sees_all_sublines_not_others():
    vis = {c["id"] for c in _corpus() if _visible(c, ["production"])}
    for s in SUBLINES:
        assert s in vis
    assert "pub" in vis
    assert "q" not in vis and "h" not in vis
    assert "production_secret" not in vis  # unknown subline fail-closed


def test_sim_quality_user_sees_only_quality_and_public():
    vis = {c["id"] for c in _corpus() if _visible(c, ["quality"])}
    assert vis == {"q", "pub"}


def test_sim_multi_group_production_quality():
    vis = {c["id"] for c in _corpus() if _visible(c, ["production", "quality"])}
    assert set(SUBLINES) <= vis and "q" in vis and "pub" in vis
    assert "h" not in vis and "production_secret" not in vis


def test_sim_anonymous_only_public():
    vis = {c["id"] for c in _corpus() if _visible(c, None)}
    assert vis == {"pub"}


def test_sim_legacy_string_production():
    vis = {c["id"] for c in _corpus() if _visible(c, "production")}
    assert set(SUBLINES) <= vis and "q" not in vis


def test_sim_hr_cannot_see_production():
    vis = {c["id"] for c in _corpus() if _visible(c, ["hr"])}
    assert vis == {"h", "pub"}
