"""Pure reconciler unit tests — no network, no storage I/O."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from redibis.services.catalog.assertions import (
    Assertion,
    AssertionKey,
    Authority,
    CoverageRecord,
    CoverageStatus,
    Facet,
    LedgerEntry,
    Suppression,
    value_hash,
)
from redibis.services.catalog.reconcile import (
    ReconcileMode,
    ReconcilePolicy,
    reconcile,
    render_reconcile_plan,
)

ASSET = "hive_prod.telecom.public.customers"
BACKEND = "openmetadata"
NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)


def _key(
    column: str,
    facet: Facet = Facet.PII_TAG,
    value_key: str = "PII.Sensitive",
) -> AssertionKey:
    return AssertionKey(
        asset_fqn=ASSET,
        column_path=column,
        facet=facet,
        value_key=value_key,
    )


def _desired(key: AssertionKey, value: object = "PII.Sensitive", confidence: float = 0.97) -> Assertion:
    return Assertion(
        key=key,
        value=value,
        authority=Authority.REDIBIS,
        confidence=confidence,
        scan_id="scan-1",
        observed_at=NOW,
    )


def _remote(
    key: AssertionKey,
    value: object = "PII.Sensitive",
    authority: Authority = Authority.REDIBIS,
) -> Assertion:
    return Assertion(
        key=key,
        value=value,
        authority=authority,
        confidence=1.0,
        observed_at=NOW,
    )


def _ledger(
    key: AssertionKey,
    value: object = "PII.Sensitive",
    *,
    state: str = "confirmed",
    negative_streak: int = 0,
) -> LedgerEntry:
    return LedgerEntry(
        backend=BACKEND,
        key=key,
        value_hash=value_hash(value),
        scan_id="scan-0",
        published_at=NOW - timedelta(days=1),
        state=state,
        negative_streak=negative_streak,
    )


def _covered(*keys: AssertionKey) -> list[CoverageRecord]:
    seen: set[tuple] = set()
    out: list[CoverageRecord] = []
    for k in keys:
        trip = (k.asset_fqn, k.column_path, k.facet)
        if trip in seen:
            continue
        seen.add(trip)
        out.append(
            CoverageRecord(
                scan_id="scan-1",
                asset_fqn=k.asset_fqn,
                column_path=k.column_path,
                facet=k.facet,
                status=CoverageStatus.EVALUATED,
            )
        )
    return out


def test_uncovered_column_with_ledger_emits_no_ops():
    """1. Uncovered column with a ledger entry → no ops (R4 regression)."""
    key = _key("blob_payload")
    plan = reconcile(
        backend=BACKEND,
        asset_fqn=ASSET,
        desired=[],
        remote=[_remote(key)],
        ledger=[_ledger(key)],
        coverage=[],  # not evaluated
        suppressions=[],
        now=NOW,
    )
    assert plan.ops == []
    assert plan.skipped_uncovered == [key]
    assert plan.summary["uncovered"] == 1


def test_skipped_coverage_with_ledger_emits_no_ops():
    """SKIPPED coverage + ledger + no desired → no op (C2 gate is load-bearing)."""
    key = _key("msisdn")
    plan = reconcile(
        backend=BACKEND,
        asset_fqn=ASSET,
        desired=[],
        remote=[_remote(key)],
        ledger=[_ledger(key)],
        coverage=[
            CoverageRecord(
                scan_id="s1",
                asset_fqn=ASSET,
                column_path=key.column_path,
                facet=key.facet,
                status=CoverageStatus.SKIPPED,
                reason="column not in sample",
            )
        ],
        suppressions=[],
        now=NOW,
    )
    assert plan.ops == []
    assert plan.skipped_uncovered == [key]
    assert plan.conflicts == []


def test_covered_negative_demotes_confirmed():
    """2. Covered, desired absent, ledger present, confirmed → demote, not remove."""
    key = _key("notes")
    plan = reconcile(
        backend=BACKEND,
        asset_fqn=ASSET,
        desired=[],
        remote=[_remote(key)],
        ledger=[_ledger(key, state="confirmed", negative_streak=0)],
        coverage=_covered(key),
        suppressions=[],
        policy=ReconcilePolicy(delete_after_negatives=2),
        now=NOW,
    )
    assert len(plan.ops) == 1
    assert plan.ops[0].op == "demote"
    assert "negative streak 1" in plan.ops[0].reason
    assert plan.summary["demotes"] == 1
    assert plan.summary["removes"] == 0


def test_second_negative_removes():
    """3. Same, second consecutive negative → remove."""
    key = _key("notes")
    plan = reconcile(
        backend=BACKEND,
        asset_fqn=ASSET,
        desired=[],
        remote=[_remote(key)],
        ledger=[_ledger(key, state="confirmed", negative_streak=1)],
        coverage=_covered(key),
        suppressions=[],
        policy=ReconcilePolicy(delete_after_negatives=2),
        now=NOW,
    )
    assert len(plan.ops) == 1
    assert plan.ops[0].op == "remove"
    assert plan.ops[0].reason == "no longer asserted"
    assert plan.summary["removes"] == 1


def test_suppression_blocks_readd():
    """4. Suppression present, desired present → remove + no re-add."""
    key = _key("email")
    plan = reconcile(
        backend=BACKEND,
        asset_fqn=ASSET,
        desired=[_desired(key)],
        remote=[_remote(key)],
        ledger=[_ledger(key)],
        coverage=_covered(key),
        suppressions=[
            Suppression(
                key=key,
                actor="alice",
                reason="false positive",
                created_at=NOW - timedelta(hours=1),
                source="om_change_event",
            )
        ],
        now=NOW,
    )
    assert len(plan.ops) == 1
    assert plan.ops[0].op == "remove"
    assert plan.ops[0].reason == "suppressed"
    assert not any(op.op == "add" for op in plan.ops)


def test_expired_suppression_allows_add():
    """5. Suppression expired → normal add."""
    key = _key("email")
    plan = reconcile(
        backend=BACKEND,
        asset_fqn=ASSET,
        desired=[_desired(key)],
        remote=[],
        ledger=[],
        coverage=_covered(key),
        suppressions=[
            Suppression(
                key=key,
                actor="alice",
                reason="temporary",
                created_at=NOW - timedelta(days=30),
                expires_at=NOW - timedelta(days=1),
                source="cli",
            )
        ],
        now=NOW,
    )
    assert len(plan.ops) == 1
    assert plan.ops[0].op == "add"
    assert plan.ops[0].state == "confirmed"


def test_human_owned_conflicts_in_normal():
    """6. Remote authority HUMAN, mode NORMAL → conflict, zero writes."""
    key = _key(
        "msisdn",
        facet=Facet.COLUMN_DESCRIPTION,
        value_key="",
    )
    plan = reconcile(
        backend=BACKEND,
        asset_fqn=ASSET,
        desired=[_desired(key, value="Redibis description", confidence=1.0)],
        remote=[_remote(key, value="Steward description", authority=Authority.HUMAN)],
        ledger=[],
        coverage=_covered(key),
        suppressions=[],
        mode=ReconcileMode.NORMAL,
        now=NOW,
    )
    assert plan.ops == []
    assert len(plan.conflicts) == 1
    assert plan.conflicts[0].reason == "human-owned"
    assert plan.summary["conflicts"] == 1


def test_enforce_displaces_human():
    """7. Same, mode ENFORCE → update with displaces=HUMAN."""
    key = _key(
        "msisdn",
        facet=Facet.COLUMN_DESCRIPTION,
        value_key="",
    )
    plan = reconcile(
        backend=BACKEND,
        asset_fqn=ASSET,
        desired=[_desired(key, value="Redibis description", confidence=1.0)],
        remote=[_remote(key, value="Steward description", authority=Authority.HUMAN)],
        ledger=[],
        coverage=_covered(key),
        suppressions=[],
        mode=ReconcileMode.ENFORCE,
        now=NOW,
    )
    assert len(plan.ops) == 1
    assert plan.ops[0].op == "update"
    assert plan.ops[0].displaces is Authority.HUMAN
    assert plan.conflicts == []


def test_ledger_hash_diverge_conflicts():
    """8. Ledger claims ownership but remote hash diverged → conflict, not overwrite."""
    key = _key("notes")
    plan = reconcile(
        backend=BACKEND,
        asset_fqn=ASSET,
        desired=[],  # negative path
        remote=[_remote(key, value="PII.Sensitive — edited by steward", authority=Authority.HUMAN)],
        ledger=[_ledger(key, value="PII.Sensitive")],  # hash of original
        coverage=_covered(key),
        suppressions=[],
        mode=ReconcileMode.NORMAL,
        now=NOW,
    )
    assert plan.ops == []
    assert len(plan.conflicts) == 1
    assert "diverged" in plan.conflicts[0].reason


def test_confidence_thresholds():
    """9. Confidence 0.70 → suggested; 0.90 → confirmed; 0.40 → not published."""
    policy = ReconcilePolicy(publish_threshold=0.60, confirm_threshold=0.85)

    key_lo = _key("a", value_key="PII.NonSensitive")
    key_mid = _key("b", value_key="PII.NonSensitive")
    key_hi = _key("c", value_key="PII.NonSensitive")

    plan_lo = reconcile(
        backend=BACKEND,
        asset_fqn=ASSET,
        desired=[_desired(key_lo, confidence=0.40)],
        remote=[],
        ledger=[],
        coverage=_covered(key_lo),
        suppressions=[],
        policy=policy,
        now=NOW,
    )
    assert plan_lo.ops == []

    plan_mid = reconcile(
        backend=BACKEND,
        asset_fqn=ASSET,
        desired=[_desired(key_mid, confidence=0.70)],
        remote=[],
        ledger=[],
        coverage=_covered(key_mid),
        suppressions=[],
        policy=policy,
        now=NOW,
    )
    assert len(plan_mid.ops) == 1
    assert plan_mid.ops[0].op == "add"
    assert plan_mid.ops[0].state == "suggested"

    plan_hi = reconcile(
        backend=BACKEND,
        asset_fqn=ASSET,
        desired=[_desired(key_hi, confidence=0.90)],
        remote=[],
        ledger=[],
        coverage=_covered(key_hi),
        suppressions=[],
        policy=policy,
        now=NOW,
    )
    assert len(plan_hi.ops) == 1
    assert plan_hi.ops[0].op == "add"
    assert plan_hi.ops[0].state == "confirmed"


def test_glossary_link_additive_no_conflict():
    """10. GLOSSARY_LINK with human-owned remote → additive, no conflict."""
    key = _key(
        "msisdn",
        facet=Facet.GLOSSARY_LINK,
        value_key="Redibis_telecom.msisdn",
    )
    plan = reconcile(
        backend=BACKEND,
        asset_fqn=ASSET,
        desired=[_desired(key, value="Mobile Phone (MSISDN)", confidence=1.0)],
        remote=[
            _remote(
                key,
                value="Legacy term link",
                authority=Authority.HUMAN,
            )
        ],
        ledger=[],
        coverage=_covered(key),
        suppressions=[],
        mode=ReconcileMode.NORMAL,
        now=NOW,
    )
    assert plan.conflicts == []
    assert len(plan.ops) == 1
    assert plan.ops[0].op == "update"


def test_no_datatype_adjacent_facets_in_ops():
    """11. dataType-adjacent facets never appear in any op list."""
    # Facet enum has no dataType; remote-only connector facts are not candidates.
    assert not hasattr(Facet, "DATA_TYPE")
    assert "data_type" not in {f.value for f in Facet}

    key = _key("msisdn")
    plan = reconcile(
        backend=BACKEND,
        asset_fqn=ASSET,
        desired=[_desired(key)],
        remote=[_remote(key)],
        ledger=[_ledger(key)],
        coverage=_covered(key),
        suppressions=[],
        now=NOW,
    )
    for op in plan.ops:
        assert isinstance(op.key.facet, Facet)
        assert op.key.facet.value != "data_type"


def test_empty_desired_empty_ledger_empty_plan():
    """12. Empty desired + empty ledger + full coverage → empty plan."""
    # Coverage for a column we never asserted and never published.
    coverage = [
        CoverageRecord(
            scan_id="scan-1",
            asset_fqn=ASSET,
            column_path="status",
            facet=Facet.PII_TAG,
            status=CoverageStatus.EVALUATED,
        )
    ]
    plan = reconcile(
        backend=BACKEND,
        asset_fqn=ASSET,
        desired=[],
        remote=[],
        ledger=[],
        coverage=coverage,
        suppressions=[],
        now=NOW,
    )
    assert plan.ops == []
    assert plan.conflicts == []
    assert plan.skipped_uncovered == []
    assert plan.summary == {
        "adds": 0,
        "updates": 0,
        "removes": 0,
        "demotes": 0,
        "promotes": 0,
        "conflicts": 0,
        "uncovered": 0,
    }


def test_render_reconcile_plan_smoke():
    key = _key("email", value_key="Redibis.EMAIL")
    plan = reconcile(
        backend=BACKEND,
        asset_fqn=ASSET,
        desired=[_desired(key, value="Redibis.EMAIL", confidence=0.97)],
        remote=[],
        ledger=[],
        coverage=_covered(key),
        suppressions=[],
        now=NOW,
    )
    text = render_reconcile_plan(plan, table="telecom.customers")
    assert "openmetadata ← telecom.customers" in text
    assert "+ " in text
    assert "adds" in text


def test_human_owned_identical_is_noop():
    """Human already holds the desired value → no conflict, no write."""
    key = _key(
        "msisdn",
        facet=Facet.COLUMN_DESCRIPTION,
        value_key="",
    )
    plan = reconcile(
        backend=BACKEND,
        asset_fqn=ASSET,
        desired=[_desired(key, value="Same description", confidence=1.0)],
        remote=[_remote(key, value="Same description", authority=Authority.HUMAN)],
        ledger=[],
        coverage=_covered(key),
        suppressions=[],
        mode=ReconcileMode.NORMAL,
        now=NOW,
    )
    assert plan.ops == []
    assert plan.conflicts == []


def test_human_owned_identical_tag_is_noop():
    """Remote HUMAN tag equal to desired → plan.ops == [] and conflicts == []."""
    key = _key("msisdn", value_key="PII.Sensitive")
    plan = reconcile(
        backend=BACKEND,
        asset_fqn=ASSET,
        desired=[_desired(key, value="PII.Sensitive", confidence=0.97)],
        remote=[_remote(key, value="PII.Sensitive", authority=Authority.HUMAN)],
        ledger=[],
        coverage=_covered(key),
        suppressions=[],
        mode=ReconcileMode.NORMAL,
        now=NOW,
    )
    assert plan.ops == []
    assert plan.conflicts == []


def test_negative_already_absent_drops_ledger_silently():
    """Ledger exists, remote gone, desired none → remove (clear ledger), no conflict."""
    key = _key("msisdn")
    plan = reconcile(
        backend=BACKEND,
        asset_fqn=ASSET,
        desired=[],
        remote=[],
        ledger=[_ledger(key)],
        coverage=_covered(key),
        suppressions=[],
        mode=ReconcileMode.NORMAL,
        now=NOW,
    )
    assert len(plan.ops) == 1
    assert plan.ops[0].op == "remove"
    assert plan.ops[0].reason == "already absent remotely; dropping ledger entry"
    assert plan.conflicts == []
