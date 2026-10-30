"""Tazminat kalemi: caregiver (CompensationItem uygular)."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from trafikrag.calculation.items.permanent_disability import _compute_elapsed_months
from trafikrag.calculation.parameters import (
    compute_annuity_due,
    get_param_warnings,
    get_parameter_version,
    load_life_table,
    round_currency,
    to_decimal,
)
from trafikrag.domain.analysis import CaseInput


class CaregiverItem:
    """Bakıcı gideri tazminatını hesaplar."""

    name: str = "caregiver"

    def compute(self, case: CaseInput, params: dict[str, Any]) -> dict[str, Any]:
        version = get_parameter_version(params)
        warnings: list[str] = []
        used_keys: list[str] = []

        has_months = (
            case.caregiver_months is not None
            and to_decimal(case.caregiver_months) > Decimal(0)
        )
        is_lifelong = bool(
            getattr(case, "caregiver_lifelong", False)
            or params.get("caregiver_lifelong", False)
        )

        if not has_months and not is_lifelong:
            if case.caregiver_ratio is not None:
                warnings.append(
                    "Bakıcı bağımlılık oranı girilmiş ancak bakıcı süresi (caregiver_months) veya ömür boyu bakıcı durumu (caregiver_lifelong=True) belirtilmediğinden bakıcı gideri hesaplanmadı"
                )
            return {
                "amount": Decimal("0.00"),
                "formula": "aylik_bakici_ucreti * bagimlilik_orani * sure_ay",
                "inputs": {
                    "monthly_caregiver_wage": Decimal("0.00"),
                    "caregiver_ratio": case.caregiver_ratio,
                    "caregiver_months": Decimal(0),
                    "caregiver_lifelong": False,
                    "parameter_version": version,
                },
                "parameter_version": version,
                "warnings": warnings,
            }

        if is_lifelong and not has_months and case.age is None:
            warnings.append("Yaş girilmediğinden ömür boyu bakıcı gideri hesaplanamadı")
            return {
                "amount": Decimal("0.00"),
                "formula": "T_gecmis + 12 * aylik_bakici_ucreti * bagimlilik_orani * ä_x",
                "inputs": {
                    "monthly_caregiver_wage": Decimal("0.00"),
                    "caregiver_ratio": case.caregiver_ratio,
                    "caregiver_lifelong": True,
                    "age": None,
                    "parameter_version": version,
                },
                "parameter_version": version,
                "warnings": warnings,
            }

        is_documented = bool(params.get("caregiver_documented", False))
        if is_documented:
            monthly_wage = to_decimal(params["asgari_ucret"]["brut"])
            used_keys.append("asgari_ucret.brut")
        else:
            monthly_wage = to_decimal(params["asgari_ucret"]["net"])
            used_keys.append("asgari_ucret.net")

        gs_cfg = params["genel_sartlar_2021"]
        used_keys.append("genel_sartlar_2021.bakici_orani")
        if case.caregiver_ratio is not None:
            ratio = to_decimal(case.caregiver_ratio)
        else:
            ratio = to_decimal(gs_cfg["bakici_orani"]["tam"])
            warnings.append("Bakıcı bağımlılık oranı girilmediğinden tam bağımlı (1.0) varsayıldı")

        min_disability_for_partial = to_decimal(
            gs_cfg.get("kismi_bakici_alt_maluliyet", Decimal("0.50"))
        )
        if (
            ratio == to_decimal(gs_cfg["bakici_orani"]["kismi"])
            and case.disability_rate is not None
            and to_decimal(case.disability_rate) < min_disability_for_partial
        ):
            warnings.append(
                "Genel Şartlar Ek-2 m.8 uyarınca kısmi bağımlı bakıcı gideri için maluliyet oranı en az %50 olmalıdır"
            )

        if has_months:
            months = to_decimal(case.caregiver_months)  # type: ignore[arg-type]
            amount = round_currency(monthly_wage * ratio * months)
            warnings.extend(get_param_warnings(params, used_keys))
            return {
                "amount": amount,
                "formula": "aylik_bakici_ucreti * bagimlilik_orani * sure_ay",
                "inputs": {
                    "monthly_caregiver_wage": monthly_wage,
                    "caregiver_ratio": ratio,
                    "caregiver_months": months,
                    "caregiver_lifelong": False,
                    "is_documented": is_documented,
                    "parameter_version": version,
                },
                "parameter_version": version,
                "warnings": warnings,
            }

        # Açıkça caregiver_lifelong=True seçilmişse: geçmiş dönem + hesap yaşındaki (calc_age) ömür boyu anüite
        elapsed_months = _compute_elapsed_months(case.accident_date, case.calculation_date)
        calc_age = min(99, int(case.age) + int(elapsed_months // Decimal(12)))  # type: ignore[arg-type]
        past_amount = round_currency(monthly_wage * ratio * elapsed_months)
        if elapsed_months > Decimal(0):
            warnings.append(
                "Geçmiş (kazanılmış) dönem bakıcı gideri kaza tarihindeki tarihsel asgari ücret yerine güncel asgari ücret üzerinden hesaplanmıştır"
            )

        gender = case.gender if case.gender in ("E", "K") else "E"
        table_map = gs_cfg["yasam_tablosu"]
        table_name = str(table_map["kadin"] if gender == "K" else table_map["erkek"])
        discount_rate = to_decimal(gs_cfg["iskonto"])
        used_keys.extend(["genel_sartlar_2021.iskonto", "genel_sartlar_2021.yasam_tablosu"])

        table = load_life_table(table_name, gender=gender, tables_dir=params.get("_base_dir"))
        annuity_factor = compute_annuity_due(table, calc_age, discount_rate)
        annual_wage = monthly_wage * Decimal(12)
        future_amount = round_currency(annual_wage * ratio * annuity_factor)
        amount = round_currency(past_amount + future_amount)

        warnings.extend(get_param_warnings(params, used_keys))

        return {
            "amount": amount,
            "formula": "T_gecmis = aylik_bakici_ucreti * bagimlilik_orani * n_ay; T_gelecek = 12 * aylik_bakici_ucreti * bagimlilik_orani * ä_x",
            "inputs": {
                "age_at_accident": case.age,
                "calc_age": calc_age,
                "elapsed_months": elapsed_months,
                "past_amount": past_amount,
                "future_amount": future_amount,
                "monthly_caregiver_wage": monthly_wage,
                "caregiver_ratio": ratio,
                "caregiver_lifelong": True,
                "annuity_factor": annuity_factor,
                "life_table": table_name,
                "is_documented": is_documented,
                "parameter_version": version,
            },
            "parameter_version": version,
            "warnings": warnings,
        }
