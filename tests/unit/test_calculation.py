"""Hesaplama motoru birim testleri (Bölüm 8 beklentileri ve PR inceleme bulguları)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from trafikrag.calculation.costs import calculate_costs
from trafikrag.calculation.engine import (
    CalculationEngine,
    check_statute_of_limitations,
    default_items,
    select_disability_regulation,
)
from trafikrag.calculation.fault import apply_fault_discount
from trafikrag.calculation.items.caregiver import CaregiverItem
from trafikrag.calculation.items.permanent_disability import PermanentDisabilityItem
from trafikrag.calculation.items.temporary_disability import TemporaryDisabilityItem
from trafikrag.calculation.items.treatment import TreatmentItem
from trafikrag.calculation.parameters import (
    compute_annuity_due,
    compute_split_annuity_due,
    get_param_warnings,
    get_parameter_version,
    get_remaining_life_expectancy,
    load_life_table,
    load_parameters,
    round_currency,
)
from trafikrag.domain.analysis import CaseInput

# --- (a) parameters.py ve YAML okuyucu testleri ---


def test_parameters_loaded_as_decimal_and_versioned():
    params = load_parameters()
    assert get_parameter_version(params) == "2026-10-18"
    assert isinstance(params["asgari_ucret"]["net"], Decimal)
    assert params["asgari_ucret"]["net"] == Decimal("28075.50")
    assert isinstance(params["genel_sartlar_2021"]["iskonto"], Decimal)
    assert params["genel_sartlar_2021"]["iskonto"] == Decimal("0.0165")


def test_unverified_parameter_warnings():
    params = load_parameters()
    warnings_unverified = get_param_warnings(params, ["harc_2026.bilirkisi_tahmini"])
    assert len(warnings_unverified) == 1
    assert "parametre harc_2026.bilirkisi_tahmini teyit edilmemiştir" in warnings_unverified[0]

    warnings_verified = get_param_warnings(
        params, ["asgari_ucret.net", "genel_sartlar_2021.iskonto", "aaut_2026"]
    )
    assert warnings_verified == []


def test_round_currency_half_up():
    assert round_currency(Decimal("100.005")) == Decimal("100.01")
    assert round_currency(Decimal("100.004")) == Decimal("100.00")


# --- (b) temporary_disability.py ve treatment.py elle hesaplanmış testler ---


def test_temporary_disability_hand_calculated():
    """Elle hesap: aylık net gelir 30.000 TL, 2.5 ay (75 gün) -> (30000 / 30) * 75 = 75.000,00 TL."""
    params = load_parameters()
    item = TemporaryDisabilityItem()
    case = CaseInput(
        age=30,
        gender="E",
        monthly_income=30000.0,
        income_basis="net",
        temporary_incapacity_months=2.5,
    )
    res = item.compute(case, params)
    assert isinstance(res["amount"], Decimal)
    assert res["amount"] == Decimal("75000.00")
    assert res["inputs"]["days"] == Decimal("75.0")
    assert res["parameter_version"] == "2026-10-18"


def test_temporary_disability_falls_back_to_minimum_wage():
    """Gelir girilmediğinde net asgari ücret (28.075,50 TL) * 2 ay = 56.151,00 TL."""
    params = load_parameters()
    item = TemporaryDisabilityItem()
    case = CaseInput(age=30, temporary_incapacity_months=2.0)
    res = item.compute(case, params)
    assert isinstance(res["amount"], Decimal)
    assert res["amount"] == Decimal("56151.00")
    assert res["inputs"]["monthly_net_income"] == Decimal("28075.50")


def test_treatment_item_hand_calculated_and_sgk_deduction():
    """Elle hesap: 15.250,75 TL belgeli tedavi gideri, SGK payı 5.250,75 TL -> 10.000,00 TL."""
    params = load_parameters()
    item = TreatmentItem()
    case_no_sgk = CaseInput(treatment_costs=15250.75)
    res_no_sgk = item.compute(case_no_sgk, params)
    assert isinstance(res_no_sgk["amount"], Decimal)
    assert res_no_sgk["amount"] == Decimal("15250.75")
    assert any("SGK" in w for w in res_no_sgk["warnings"])

    case_with_sgk = CaseInput(treatment_costs=15250.75, sgk_treatment_covered=5250.75)
    res_with_sgk = item.compute(case_with_sgk, params)
    assert res_with_sgk["amount"] == Decimal("10000.00")
    assert res_with_sgk["inputs"]["sgk_covered"] == Decimal("5250.75")


# --- (c) fault.py testleri ---


def test_fault_discount_hand_calculated():
    """Elle hesap: 200.000 TL toplam zarar, %25 davacı kusuru -> 150.000,00 TL net."""
    res = apply_fault_discount(Decimal("200000.00"), 0.25)
    assert isinstance(res["net_amount"], Decimal)
    assert res["net_amount"] == Decimal("150000.00")
    assert res["discount_amount"] == Decimal("50000.00")
    assert res["warnings"] == []


def test_fault_discount_equal_fault_karar03():
    """karar_03 örneği: %50 kusur oranında tutar tam yarıya iner."""
    res = apply_fault_discount(Decimal("215180.20"), 0.50)
    assert res["net_amount"] == Decimal("107590.10")


def test_fault_discount_missing_fault_warns_and_does_not_discount():
    res = apply_fault_discount(Decimal("100000.00"), None)
    assert res["net_amount"] == Decimal("100000.00")
    assert res["discount_amount"] == Decimal("0.00")
    assert "kusur oranı girilmedi" in res["warnings"]


# --- (d) Yaşam tablosu yükleme ve anüite matematiği testleri ---


def test_life_table_annuity_hand_calculated_small_table():
    """Küçük 3 yıllık yaşam tablosu ile elle anüite hesabı:
    Yaş 97: q_97 = 0.20 -> p_97 = 0.80
    Yaş 98: q_98 = 0.50 -> p_98 = 0.50
    Yaş 99: q_99 = 1.00 -> p_99 = 0.00
    k_p_97:
      k=0: 1
      k=1: 0.80
      k=2: 0.80 * 0.50 = 0.40
    İskonto i = 0.00 iken ä_97 = 1 + 0.80 + 0.40 = 2.200000.
    """
    mini_table = {
        97: {"qx": Decimal("0.20"), "ex": Decimal("1.70")},
        98: {"qx": Decimal("0.50"), "ex": Decimal("1.00")},
        99: {"qx": Decimal("1.00"), "ex": Decimal("0.50")},
    }
    annuity_zero_i = compute_annuity_due(mini_table, 97, Decimal(0))
    assert annuity_zero_i == Decimal("2.200000")

    annuity_ten_pct = compute_annuity_due(mini_table, 97, Decimal("0.10"))
    assert annuity_ten_pct == Decimal("2.057851")


def test_csv_life_tables_load_and_unknown_table_raises():
    trh = load_life_table("TRH-2010", gender="E")
    trfh = load_life_table("TRFH-2020", gender="K")
    assert 0 in trh and 99 in trh
    assert 0 in trfh and 99 in trfh
    assert trh[99]["qx"] == Decimal("1.000000")

    active_a, passive_a, total_a = compute_split_annuity_due(trh, 30, 65, Decimal("0.0165"))
    full_a = compute_annuity_due(trh, 30, Decimal("0.0165"))
    assert total_a == full_a
    assert abs((active_a + passive_a) - full_a) <= Decimal("0.000002")
    assert get_remaining_life_expectancy(trh, 30) == Decimal("44.45")
    assert get_remaining_life_expectancy(trfh, 30) == Decimal("49.00")

    with pytest.raises(ValueError, match="Bilinmeyen yaşam tablosu"):
        load_life_table("PMF-1931")


def test_official_ek7_life_table_actuarial_comparison():
    """04.12.2021 tarih ve 31679 sayılı Resmî Gazete Genel Şartlar Ek-7 (s. 25-28) resmî
    tablo doğrulaması ve komütasyon sütunları (Dx, Nx) ile bağımsız aktüeryal karşılaştırma:
    - Yaş 30 Erkek için resmî Ek-7 tablosunda:
      qx = 0.000973, ex = 44.45, D_30 = 58853.30, N_30 = 1852738.74
      Yaş 65 Erkek için: D_65 = 26650.31, N_65 = 337754.82
    - Komütasyon sütunlarından bağımsız aktüeryal anüite katsayıları:
      ä_30 (toplam) = N_30 / D_30 = 1852738.74 / 58853.30 = 31.480626
      ä_30:35| (aktif) = (N_30 - N_65) / D_30 = 1514983.92 / 58853.30 = 25.741699
      35|ä_30 (pasif) = N_65 / D_30 = 337754.82 / 58853.30 = 5.738927
    """
    trh_male = load_life_table("TRH-2010", gender="E")
    trh_female = load_life_table("TRH-2010", gender="K")

    # 1. İnterpolasyon olmadığının kontrolü (20-29 yaş arasında qx azalış ve artış gösterir)
    assert trh_male[23]["qx"] == Decimal("0.000981")
    assert trh_male[24]["qx"] == Decimal("0.000955")
    assert trh_male[25]["qx"] == Decimal("0.000919")
    assert trh_male[26]["qx"] == Decimal("0.000905")
    assert trh_male[0]["ex"] == Decimal("71.93")
    assert trh_male[1]["ex"] == Decimal("72.35")
    assert trh_female[0]["ex"] == Decimal("78.02")

    # 2. Resmî Ek-7 Dx/Nx komütasyon sütunları ile motorun anüite hesabı karşılaştırması
    d_30 = Decimal("58853.30")
    n_30 = Decimal("1852738.74")
    n_65 = Decimal("337754.82")

    official_active_annuity = (n_30 - n_65) / d_30
    official_passive_annuity = n_65 / d_30
    official_total_annuity = n_30 / d_30

    active_a, passive_a, total_a = compute_split_annuity_due(
        trh_male, start_age=30, active_end_age=65, discount_rate=Decimal("0.0165")
    )
    # Basılı tablodaki tam sayı lx yuvarlaması kaynaklı fark 0.0002'den küçüktür
    assert abs(active_a - official_active_annuity) < Decimal("0.0002")
    assert abs(passive_a - official_passive_annuity) < Decimal("0.0002")
    assert abs(total_a - official_total_annuity) < Decimal("0.0002")


# --- (e) permanent_disability.py elle hesaplanmış testler ---


def test_permanent_disability_hand_calculated_yargitay(tmp_path: Path):
    """Elle hesaplanan Yargıtay (progresif rant) örneği:
    - Kaza anı yaşı: 60, hesap tarihi: kazadan tam 12 ay (1 yıl) sonra -> hesap yaşı = 61
    - Aylık net gelir: 30.000 TL (pasif asgari ücret: 28.075,50 TL)
    - Maluliyet oranı: %10 (0.10)
    - Sabit test tablosunda yaş 61 için bakiye ömür e_61 = 10.00 yıl:
      - Aktif dönem (61 -> 65 yaş): 4.00 yıl
      - Pasif dönem (65 -> 71 yaş): 6.00 yıl
    - Geçmiş (kazanılmış) dönem (12 ay, iskontosuz):
      T_geçmiş = 30.000 * 0.10 * 12 = 36.000,00 TL
    - Gelecek aktif dönem:
      T_gelecek_aktif = (30.000 * 12) * 0.10 * 4 = 144.000,00 TL
    - Gelecek pasif dönem:
      T_gelecek_pasif = (28.075,50 * 12) * 0.10 * 6 = 202.143,60 TL
    - Gelecek toplam = 144.000,00 + 202.143,60 = 346.143,60 TL
    - Genel toplam = 36.000,00 + 346.143,60 = 382.143,60 TL
    """
    csv_file = tmp_path / "trh_2010.csv"
    with open(csv_file, "w", encoding="utf-8") as f:
        f.write("age,qx_male,qx_female,ex_male,ex_female\n")
        for a in range(100):
            qx = "1.000000" if a == 99 else "0.010000"
            ex = "10.00" if a == 61 else "15.00"
            f.write(f"{a},{qx},{qx},{ex},{ex}\n")

    params = load_parameters()
    params["_base_dir"] = str(tmp_path)

    case = CaseInput(
        age=60,
        gender="E",
        employment_status="calisiyor",
        monthly_income=30000.0,
        income_basis="net",
        disability_rate=0.10,
        accident_date=date(2025, 1, 1),
        calculation_date=date(2026, 1, 1),
        method="yargitay",
    )
    item = PermanentDisabilityItem()
    res = item.compute(case, params)

    assert isinstance(res["amount"], Decimal)
    assert res["inputs"]["past_amount"] == Decimal("36000.00")
    assert res["inputs"]["future_amount"] == Decimal("346143.60")
    assert res["amount"] == Decimal("382143.60")
    assert not any("yaklaşık değerlerdir" in w for w in res["warnings"])
    assert any("Geçmiş (kazanılmış) dönem kaybı" in w for w in res["warnings"])


def test_permanent_disability_both_methods_when_method_is_none():
    params = load_parameters()
    case = CaseInput(
        age=35,
        gender="K",
        employment_status="calisiyor",
        monthly_income=35000.0,
        income_basis="net",
        disability_rate=0.15,
        accident_date=date(2024, 6, 1),
        calculation_date=date(2026, 6, 1),
        method=None,
    )
    item = PermanentDisabilityItem()
    res = item.compute(case, params)
    assert isinstance(res["amount"], Decimal)
    methods = res["inputs"]["methods"]
    assert "genel_sartlar" in methods and "yargitay" in methods
    assert methods["genel_sartlar"]["life_table"] == "TRFH-2020"
    assert methods["yargitay"]["life_table"] == "TRH-2010"
    assert any("Poliçe tarihi (policy_date) girilmediğinden" in w for w in res["warnings"])


# --- (f) caregiver.py elle hesaplanmış testler ---


def test_caregiver_hand_calculated_temporary_months():
    """Elle hesap: Belgeli bakıcı yok -> net asgari ücret (28.075,50 TL).
    Kısmi bağımlı (0.50), 6 ay, maluliyet %60:
    28.075,50 * 0.50 * 6 = 84.226,50 TL.
    """
    params = load_parameters()
    item = CaregiverItem()
    case = CaseInput(
        age=40,
        disability_rate=0.60,
        caregiver_months=6.0,
        caregiver_ratio=0.5,
    )
    res = item.compute(case, params)
    assert isinstance(res["amount"], Decimal)
    assert res["amount"] == Decimal("84226.50")


def test_caregiver_does_not_silently_fallback_to_lifelong():
    """caregiver_months boş ve caregiver_lifelong=False iken caregiver_ratio dolu olsa bile
    sessizce ömür boyu anüiteye düşmemeli, 0.00 TL dönüp uyarı vermeli.
    """
    params = load_parameters()
    item = CaregiverItem()
    case = CaseInput(
        age=40,
        gender="E",
        disability_rate=0.80,
        caregiver_months=None,
        caregiver_ratio=1.0,
        caregiver_lifelong=False,
    )
    res = item.compute(case, params)
    assert res["amount"] == Decimal("0.00")
    assert any("caregiver_lifelong=True" in w for w in res["warnings"])


def test_caregiver_lifelong_uses_calc_age():
    """caregiver_lifelong=True seçildiğinde kaza yaşı (30) değil hesap yaşı (32) kullanılmalı."""
    params = load_parameters()
    item = CaregiverItem()
    case = CaseInput(
        age=30,
        gender="E",
        disability_rate=0.80,
        caregiver_ratio=1.0,
        caregiver_lifelong=True,
        accident_date=date(2024, 1, 1),
        calculation_date=date(2026, 1, 1),
    )
    res = item.compute(case, params)
    assert res["amount"] > Decimal("0.00")
    assert res["inputs"]["age_at_accident"] == 30
    assert res["inputs"]["calc_age"] == 32
    assert res["inputs"]["past_amount"] == Decimal("673812.00")  # 28075.50 * 24 ay


def test_caregiver_partial_dependency_low_disability_warns():
    """Kısmi bağımlı bakıcıda maluliyet %50 altındaysa Ek-2 m.8 uyarısı üretilmeli."""
    params = load_parameters()
    item = CaregiverItem()
    case = CaseInput(
        age=40,
        disability_rate=0.30,
        caregiver_months=2.0,
        caregiver_ratio=0.5,
    )
    res = item.compute(case, params)
    assert any("en az %50" in w for w in res["warnings"])


# --- (g) engine.py, uç değerler ve determinizm testleri ---


def test_default_items_function_name():
    items = default_items()
    assert len(items) == 4


def test_engine_edge_cases_zero_and_full_rates():
    engine = CalculationEngine()

    case_zero = CaseInput(
        age=18,
        gender="E",
        monthly_income=20000.0,
        income_basis="net",
        disability_rate=0.0,
        plaintiff_fault=0.0,
        accident_date=date(2025, 5, 1),
        calculation_date=date(2026, 5, 1),
    )
    res_zero = engine.calculate(case_zero)
    assert res_zero.compensation["net_total"] == Decimal("0.00")
    assert any("kaza tarihine uygun limit kullanılmadı" in w for w in res_zero.warnings)

    case_full_fault = CaseInput(
        age=65,
        gender="E",
        disability_rate=1.0,
        treatment_costs=10000.0,
        plaintiff_fault=1.0,
        accident_date=date(2025, 5, 1),
        calculation_date=date(2026, 5, 1),
    )
    res_full_fault = engine.calculate(case_full_fault)
    assert res_full_fault.compensation["gross_total"] > Decimal("0.00")
    assert res_full_fault.compensation["net_total"] == Decimal("0.00")


def test_engine_policy_limit_cap():
    engine = CalculationEngine()
    case = CaseInput(
        age=25,
        gender="E",
        monthly_income=100000.0,
        income_basis="net",
        disability_rate=0.80,
        plaintiff_fault=0.0,
        accident_date=date(2025, 1, 1),
        calculation_date=date(2026, 1, 1),
        policy_limit=500000.0,
        method="genel_sartlar",
    )
    res = engine.calculate(case)
    assert res.compensation["net_total"] > Decimal("500000.00")
    assert res.compensation["insurer_payable"] == Decimal("500000.00")
    assert res.compensation["excess_over_limit"] == (
        res.compensation["net_total"] - Decimal("500000.00")
    )
    assert any("sigorta teminat limitini" in w for w in res.warnings)
    assert not any("kaza tarihine uygun limit kullanılmadı" in w for w in res.warnings)


def test_engine_determinism_and_decimal_types():
    engine = CalculationEngine()
    case = CaseInput(
        age=34,
        gender="E",
        employment_status="calisiyor",
        monthly_income=35000.0,
        income_basis="net",
        disability_rate=0.12,
        temporary_incapacity_months=3.0,
        caregiver_months=1.0,
        caregiver_ratio=1.0,
        treatment_costs=12000.0,
        plaintiff_fault=0.25,
        accident_date=date(2024, 1, 15),
        calculation_date=date(2026, 1, 15),
    )
    res1 = engine.calculate(case)
    res2 = engine.calculate(case)

    assert res1.compensation == res2.compensation
    assert res1.costs == res2.costs
    assert res1.warnings == res2.warnings

    for item_name, item_data in res1.compensation["items"].items():
        assert isinstance(item_data["amount"], Decimal), f"{item_name} Decimal döndürmeli"
    assert isinstance(res1.compensation["gross_total"], Decimal)
    assert isinstance(res1.compensation["net_total"], Decimal)
    assert res1.compensation["label"] == "tahmini hesap"
    assert "method_comparison" in res1.compensation


def test_disability_regulation_selection_by_accident_date():
    assert "Tüzüğü" in select_disability_regulation(date(2008, 1, 1))
    assert "Çalışma Gücü" in select_disability_regulation(date(2010, 5, 1))
    assert "Maluliyet Tespiti" in select_disability_regulation(date(2014, 1, 1))
    assert "Özürlülük Ölçütü" in select_disability_regulation(date(2017, 1, 1))
    assert "Erişkinler İçin Engellilik" in select_disability_regulation(date(2022, 1, 1))


def test_statute_of_limitations_warnings():
    params = load_parameters()
    w_short = check_statute_of_limitations(date(2023, 1, 1), date(2026, 1, 1), params)
    assert any("2 yıldan fazla" in w for w in w_short)

    w_long = check_statute_of_limitations(date(2014, 1, 1), date(2026, 1, 1), params)
    assert any("10 yıllık" in w for w in w_long)


# --- (h) costs.py elle hesaplanmış testler ---


def test_costs_hand_calculated_with_official_tariffs():
    """Elle hesap: Dava değeri 100.000,00 TL
    - Başvurma harcı: 732,00 TL (Harçlar Kanunu Genel Tebliği Seri No: 98)
    - Nispi karar ve ilam harcı: 100.000 * 68,31 / 1000 = 6.831,00 TL (Harçlar Kanunu 1 Sayılı Tarife)
    - Peşin harç (1/4): 6.831,00 * 0.25 = 1.707,75 TL (Harçlar Kanunu m. 28/1-a)
    - Bilirkişi ücreti: 5.000,00 TL (tahmini)
    - AAÜT 2026: 100.000 TL için nispi %16 = 16.000 TL, maktu alt sınır 45.000 TL -> 45.000,00 TL
    """
    params = load_parameters()
    costs = calculate_costs(Decimal("100000.00"), params)

    assert costs["basvurma_harci"] == Decimal("732.00")
    assert costs["nispi_karar_ilam_harci"] == Decimal("6831.00")
    assert costs["pesin_harc"] == Decimal("1707.75")
    assert costs["bilirkisi_ucreti"] == Decimal("5000.00")
    assert costs["bilirkisi_etiketi"] == "tahmini"
    assert costs["vekalet_ucreti"] == Decimal("45000.00")
    assert costs["total_upfront_costs"] == Decimal("7439.75")
    assert costs["total_judgment_costs"] == Decimal("57563.00")
    assert any("parametre harc_2026.bilirkisi_tahmini teyit edilmemiştir" in w for w in costs["warnings"])


def test_costs_fallback_when_aaut_is_none():
    params = load_parameters()
    params["aaut_2026"] = None
    costs = calculate_costs(Decimal("100000.00"), params)
    assert costs["vekalet_ucreti"] is None
    assert any("AAÜT 2026" in w for w in costs["warnings"])


def test_costs_higher_claim_amount_progressive_aaut():
    """1.000.000,00 TL dava değeri için AAÜT 2026 hesabı:
    - İlk 600.000 TL * %16 = 96.000 TL
    - Kalan 400.000 TL * %15 = 60.000 TL
    - Toplam = 156.000,00 TL
    """
    params = load_parameters()
    costs = calculate_costs(Decimal("1000000.00"), params)
    assert costs["vekalet_ucreti"] == Decimal("156000.00")


def test_costs_aaut_above_18_6m_final_brackets():
    """20.000.000,00 TL (> 18,6M TL) dava değeri için AAÜT 2026 tüm dilimlerin elle hesabı:
    1) İlk 600.000 TL * %16       =  96.000,00 TL (kümülatif: 600.000 TL)
    2) Sonraki 600.000 TL * %15   =  90.000,00 TL (kümülatif: 1.200.000 TL)
    3) Sonraki 1.200.000 TL * %14 = 168.000,00 TL (kümülatif: 2.400.000 TL)
    4) Sonraki 1.200.000 TL * %13 = 156.000,00 TL (kümülatif: 3.600.000 TL)
    5) Sonraki 1.800.000 TL * %11 = 198.000,00 TL (kümülatif: 5.400.000 TL)
    6) Sonraki 2.400.000 TL * %8  = 192.000,00 TL (kümülatif: 7.800.000 TL)
    7) Sonraki 3.000.000 TL * %5  = 150.000,00 TL (kümülatif: 10.800.000 TL)
    8) Sonraki 3.600.000 TL * %3  = 108.000,00 TL (kümülatif: 14.400.000 TL)
    9) Sonraki 4.200.000 TL * %2  =  84.000,00 TL (kümülatif: 18.600.000 TL)
    10) 18.600.000 TL üzeri 1.400.000 TL * %1 = 14.000,00 TL (toplam: 20.000.000 TL)
    Toplam vekâlet ücreti = 1.242.000,00 + 14.000,00 = 1.256.000,00 TL
    """
    params = load_parameters()
    costs = calculate_costs(Decimal("20000000.00"), params)
    assert costs["vekalet_ucreti"] == Decimal("1256000.00")
