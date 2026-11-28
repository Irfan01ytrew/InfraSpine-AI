"""Step Y slice 1: finance anomaly detection is advisory-only and deterministic.

Pure-function tests (stdlib + pytest); run with ``--noconftest`` so the
database-backed tests/conftest.py is not needed.
"""

from datetime import date, timedelta
from decimal import Decimal

from app.finance.anomaly import (
    AnomalyConfig,
    InvoiceRecord,
    blocks_straight_through,
    config_from_settings,
    detect_anomalies,
    detect_bank_account_change,
    requires_payee_verification,
    validate_config_patch,
)

D = date(2026, 11, 20)


def rec(id_, vendor='Acme Supplies', number='INV-100', amount='100',
        currency='PKR', day=D, bank='PK-001'):
    return InvoiceRecord(id=id_, vendor_name=vendor, invoice_number=number,
                         amount=Decimal(amount) if amount is not None else None,
                         currency=currency, invoice_date=day, bank_account=bank)


def history_of(amounts, vendor='Acme Supplies', start_day=None):
    day = start_day or (D - timedelta(days=60))
    return [rec(f'h{i}', vendor=vendor, number=f'INV-{i:03d}',
                amount=a, day=day + timedelta(days=i * 7)) for i, a in enumerate(amounts)]


def codes(flags):
    return [f.code for f in flags]


def test_exact_duplicate_flagged():
    prior = rec('p1', number='INV-100')
    flags = detect_anomalies(rec('new', number='INV-100'), [prior])
    assert 'exact_duplicate' in codes(flags)


def test_same_number_different_vendor_not_flagged():
    prior = rec('p1', vendor='Other Vendor', number='INV-100')
    # Vendor differs, so no duplicate; the vendor is also first-time for 'new'.
    flags = detect_anomalies(rec('new', vendor='Acme Supplies', number='INV-100'), [prior])
    assert 'exact_duplicate' not in codes(flags)


def test_invoice_number_normalisation_counts_as_duplicate():
    prior = rec('p1', number='inv 100')
    flags = detect_anomalies(rec('new', number='INV-100'), [prior])
    assert 'exact_duplicate' in codes(flags)


def test_near_duplicate_same_amount_and_date_different_number():
    prior = rec('p1', number='INV-101', amount='250', day=D - timedelta(days=2))
    flags = detect_anomalies(rec('new', number='INV-200', amount='250'), [prior])
    assert 'near_duplicate' in codes(flags)


def test_near_duplicate_outside_window_not_flagged():
    prior = rec('p1', number='INV-101', amount='250', day=D - timedelta(days=30))
    flags = detect_anomalies(rec('new', number='INV-200', amount='250'), [prior])
    assert 'near_duplicate' not in codes(flags)


def test_near_duplicate_different_amount_not_flagged():
    prior = rec('p1', number='INV-101', amount='251', day=D)
    flags = detect_anomalies(rec('new', number='INV-200', amount='250'), [prior])
    assert 'near_duplicate' not in codes(flags)


def test_amount_outlier_above_median_plus_mad():
    hist = history_of(['100', '105', '98', '102', '101', '99'])
    flags = detect_anomalies(rec('new', amount='1000'), hist)
    assert 'amount_outlier' in codes(flags)
    flag = next(f for f in flags if f.code == 'amount_outlier')
    assert flag.details['history_count'] == 6
    assert Decimal(flag.details['threshold']) < Decimal('1000')


def test_amount_near_median_not_flagged():
    hist = history_of(['100', '105', '98', '102', '101', '99'])
    flags = detect_anomalies(rec('new', amount='104'), hist)
    assert 'amount_outlier' not in codes(flags)


def test_amount_outlier_needs_minimum_history():
    hist = history_of(['100', '101', '99'])
    flags = detect_anomalies(rec('new', amount='10000'), hist)
    assert 'amount_outlier' not in codes(flags)


def test_amount_outlier_flat_history_uses_ratio():
    hist = history_of(['100', '100', '100', '100', '100'])
    assert 'amount_outlier' not in codes(detect_anomalies(rec('new', amount='250'), hist))
    assert 'amount_outlier' in codes(detect_anomalies(rec('new', amount='350'), hist))


def test_outlier_ignores_other_currency_history():
    hist = history_of(['100', '100', '100', '100', '100']) + \
        [rec('u1', vendor='Acme Supplies', number='INV-900', amount='100000',
             currency='USD', day=D - timedelta(days=90))]
    flags = detect_anomalies(rec('new', amount='120', currency='PKR'), hist)
    assert 'amount_outlier' not in codes(flags)


def test_first_time_payee_flagged_and_known_vendor_not():
    assert 'first_time_payee' in codes(detect_anomalies(rec('new'), []))
    hist = history_of(['100'])
    assert 'first_time_payee' not in codes(detect_anomalies(rec('new'), hist))


def test_velocity_flagged_at_threshold():
    priors = [rec(f'p{i}', number=f'INV-9{i}', amount='50',
                  day=D - timedelta(days=i)) for i in range(1, 4)]
    flags = detect_anomalies(rec('new', number='INV-200', amount='50'), priors)
    assert 'rapid_repeat_payments' in codes(flags)
    flag = next(f for f in flags if f.code == 'rapid_repeat_payments')
    assert flag.details['prior_count'] == 3


def test_velocity_below_threshold_not_flagged():
    priors = [rec(f'p{i}', number=f'INV-9{i}', amount='50',
                  day=D - timedelta(days=i)) for i in range(1, 3)]
    flags = detect_anomalies(rec('new', number='INV-200', amount='50'), priors)
    assert 'rapid_repeat_payments' not in codes(flags)


def test_velocity_ignores_old_priors_outside_window():
    priors = [rec(f'p{i}', number=f'INV-9{i}', amount='50',
                  day=D - timedelta(days=30 + i)) for i in range(3)]
    flags = detect_anomalies(rec('new', number='INV-200', amount='50'), priors)
    assert 'rapid_repeat_payments' not in codes(flags)


def test_missing_amount_and_date_never_crash_and_never_flag_outlier():
    hist = history_of(['100', '101', '99', '102', '98', '100'])
    flags = detect_anomalies(rec('new', amount=None, day=None), hist)
    assert 'amount_outlier' not in codes(flags)
    assert 'near_duplicate' not in codes(flags)
    assert 'rapid_repeat_payments' not in codes(flags)


def test_all_flags_are_warnings_and_block_straight_through():
    hist = history_of(['100', '105', '98', '102', '101', '99'])
    flags = detect_anomalies(rec('new', amount='1000'), hist)
    assert flags and all(f.severity == 'warning' for f in flags)
    assert blocks_straight_through(flags) is True
    assert blocks_straight_through([]) is False


def test_detection_is_deterministic():
    hist = history_of(['100', '105', '98', '102', '101', '99'])
    record = rec('new', number='INV-200', amount='1000')
    first = detect_anomalies(record, hist)
    second = detect_anomalies(record, hist)
    assert [(f.code, f.message, f.severity) for f in first] == \
           [(f.code, f.message, f.severity) for f in second]
    assert first[0].details == second[0].details


# -------------------------------------------------------------------------------- slice 3: bank-account change

def test_bank_account_change_flagged_when_account_differs():
    prior = rec('p1', bank='PK00ALPHA0001')
    flag = detect_bank_account_change(rec('new', bank='PK99NEW0009'), [prior])
    assert flag is not None and flag.code == 'bank_account_changed'
    assert flag.severity == 'warning'
    assert flag.details['prior_id'] == 'p1'


def test_bank_account_change_not_flagged_when_same_account():
    prior = rec('p1', bank='PK00ALPHA0001')
    assert detect_bank_account_change(rec('new', bank='PK00ALPHA0001'), [prior]) is None


def test_bank_account_change_ignores_formatting():
    prior = rec('p1', bank='PK00ALPHA0001')
    assert detect_bank_account_change(rec('new', bank='pk00 alpha-0001'), [prior]) is None


def test_bank_account_change_not_flagged_without_prior_account():
    assert detect_bank_account_change(rec('new', bank='PK99NEW0009'), []) is None
    assert detect_bank_account_change(rec('new', bank='PK99NEW0009'), [rec('p1', bank=None)]) is None


def test_bank_account_change_not_flagged_without_record_account():
    prior = rec('p1', bank='PK00ALPHA0001')
    assert detect_bank_account_change(rec('new', bank=None), [prior]) is None


def test_bank_account_change_compares_against_most_recent_prior():
    old = rec('p1', bank='PK00OLD0001', day=D - timedelta(days=90))
    recent = rec('p2', bank='PK00ALPHA0001', day=D - timedelta(days=5))
    # The account changed once already; the current invoice matches the latest one.
    assert detect_bank_account_change(rec('new', bank='PK00ALPHA0001'), [old, recent]) is None
    # ... but an account matching only the superseded one is still a change.
    assert detect_bank_account_change(rec('new', bank='PK00OLD0001'), [old, recent]) is not None


def test_bank_account_change_masks_full_account_numbers():
    prior = rec('p1', bank='PK00ALPHA0001')
    flag = detect_bank_account_change(rec('new', bank='PK99NEW0009'), [prior])
    assert flag.details['previous_account'].endswith('0001')
    assert flag.details['new_account'].endswith('0009')
    assert 'PK00ALPHA0001' not in str(flag.details)
    assert 'PK99NEW0009' not in str(flag.details)


def test_bank_account_change_flows_through_detect_anomalies():
    prior = rec('p1', bank='PK00ALPHA0001')
    flags = detect_anomalies(rec('new', number='INV-200', bank='PK99NEW0009'), [prior])
    assert 'bank_account_changed' in codes(flags)
    assert blocks_straight_through(flags) is True


def test_requires_payee_verification_only_for_hold_codes():
    prior = rec('p1', bank='PK00ALPHA0001')
    changed = detect_anomalies(rec('new', number='INV-200', bank='PK99NEW0009'), [prior])
    assert requires_payee_verification(changed) is True
    first_time = detect_anomalies(rec('new', vendor='Brand New Vendor'), [])
    assert requires_payee_verification(first_time) is True
    hist = history_of(['100', '105', '98', '102', '101', '99'])
    outlier = detect_anomalies(rec('new', number='INV-200', amount='1000'), hist)
    assert 'amount_outlier' in codes(outlier)
    assert requires_payee_verification(outlier) is False
    assert requires_payee_verification([]) is False


# --- Slice 5: per-department threshold settings (pure) ---

def test_config_from_settings_defaults_when_nothing_stored():
    assert config_from_settings(None) == AnomalyConfig()
    assert config_from_settings({}) == AnomalyConfig()
    assert config_from_settings('not a dict') == AnomalyConfig()


def test_config_from_settings_applies_valid_overrides():
    cfg = config_from_settings({'min_history': 3, 'near_duplicate_days': 14,
                                'outlier_mad_multiplier': '2.5', 'velocity_max_prior': 5})
    assert cfg.min_history == 3
    assert cfg.near_duplicate_days == 14
    assert cfg.outlier_mad_multiplier == Decimal('2.5')
    assert cfg.velocity_max_prior == 5
    assert cfg.velocity_window_days == AnomalyConfig().velocity_window_days  # untouched


def test_config_from_settings_falls_back_per_field_on_bad_values():
    cfg = config_from_settings({'min_history': 0, 'velocity_window_days': 'soon',
                                'outlier_mad_multiplier': 'NaN', 'near_duplicate_days': 400,
                                'unknown_key': 1, 'velocity_max_prior': True})
    defaults = AnomalyConfig()
    assert cfg.min_history == defaults.min_history
    assert cfg.velocity_window_days == defaults.velocity_window_days
    assert cfg.outlier_mad_multiplier == defaults.outlier_mad_multiplier
    assert cfg.near_duplicate_days == defaults.near_duplicate_days
    assert cfg.velocity_max_prior == defaults.velocity_max_prior


def test_config_from_settings_changes_detection():
    hist = history_of(['100', '105', '98', '102', '101', '99'])
    # Median 100.5, MAD 1.5: default k=5 flags above 108; k=50 flags above 175.5.
    outlier = rec('new', number='INV-200', amount='150')
    assert 'amount_outlier' in codes(detect_anomalies(outlier, hist))
    strict = config_from_settings({'outlier_mad_multiplier': '50'})
    assert 'amount_outlier' not in codes(detect_anomalies(outlier, hist, strict))
    loose = config_from_settings({'velocity_max_prior': 10})
    fast = [rec(f'p{n}', number=f'INV-{n}', day=D - timedelta(days=n)) for n in range(1, 5)]
    assert 'rapid_repeat_payments' in codes(detect_anomalies(rec('new', number='INV-200'), fast))
    assert 'rapid_repeat_payments' not in codes(detect_anomalies(rec('new', number='INV-200'), fast, loose))


def test_validate_config_patch_accepts_and_cleans():
    assert validate_config_patch({}) == {}
    assert validate_config_patch({'min_history': 3, 'outlier_mad_multiplier': 2}) == {
        'min_history': 3, 'outlier_mad_multiplier': '2'}
    assert validate_config_patch({'near_duplicate_days': 0}) == {'near_duplicate_days': 0}


def test_validate_config_patch_rejects_bad_values():
    import pytest
    for bad in ({'min_history': 0}, {'min_history': 101}, {'min_history': 'three'},
               {'outlier_mad_multiplier': 0}, {'outlier_mad_multiplier': 'NaN'},
               {'near_duplicate_days': -1}, {'near_duplicate_days': 366},
               {'velocity_window_days': 0}, {'velocity_max_prior': 0},
               {'unknown_key': 1}, {'min_history': True}):
        with pytest.raises(ValueError):
            validate_config_patch(bad)
    with pytest.raises(ValueError):
        validate_config_patch(['not', 'a', 'dict'])
