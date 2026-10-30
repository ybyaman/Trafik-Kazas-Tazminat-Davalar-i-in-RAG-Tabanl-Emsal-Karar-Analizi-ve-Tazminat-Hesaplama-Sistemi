"""Tazminat kalemi: permanent_disability (CompensationItem uygular)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from trafikrag.calculation.parameters import (
    compute_annuity_due,
    compute_split_annuity_due,
    get_param_warnings,
    get_parameter_version,
    get_remaining_life_expectancy,
    load_life_table,
    resolve_monthly_net_income,
    round_currency,
    to_decimal,
)
from trafikrag.domain.analysis import CaseInput


def _compute_elapsed_months(accident_date: date | None, calculation_date: date | None) -> Decimal:
    """Kaza tarihi ile hesap tarihi arasındaki süreyi ay (Decimal) olarak hesaplar."""
    if accident_date is None or calculation_date is None:
        return Decimal(0)
    if calculation_date <= accident_date:
        return Decimal(0)
    whole_months = (calculation_date.year - accident_date.year) * 12 + (
        calculation_date.month - accident_date.month
    )
    day_diff = calculation_date.day - accident_date.day
    elapsed = Decimal(whole_months) + (Decimal(day_diff) / Decimal(30))
    return max(Decimal(0), elapsed)


class PermanentDisabilityItem:
    """Sürekli iş göremezlik (maluliyet) tazminatını hesaplar (Genel Şartlar ve Yargıtay yöntemleri)."""

    name: str = "permanent_disability"

    def _calculate_genel_sartlar(
        self,
        calc_age: int,
        gender: str,
        disability_rate: Decimal,
        active_monthly: Decimal,
        passive_monthly: Decimal,
        past_amount: Decimal,
        elapsed_months: Decimal,
        is_retired: bool,
        params: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        gs_cfg = params["genel_sartlar_2021"]
        discount_rate = to_decimal(gs_cfg["iskonto"])
        active_end_age = int(gs_cfg["aktif_donem"][1])
        table_map = gs_cfg["yasam_tablosu"]
        table_name = str(table_map["kadin"] if gender == "K" else table_map["erkek"])
        tables_dir = params.get("_base_dir")

        table = load_life_table(table_name, gender=gender, tables_dir=tables_dir)
        used_keys = [
            "genel_sartlar_2021.iskonto",
            "genel_sartlar_2021.aktif_donem",
            "genel_sartlar_2021.yasam_tablosu",
        ]

        if calc_age < active_end_age and not is_retired:
            active_annuity, passive_annuity, total_annuity = compute_split_annuity_due(
                table, calc_age, active_end_age, discount_rate
            )
            future_active = round_currency(
                active_monthly * Decimal(12) * disability_rate * active_annuity
            )
            future_passive = round_currency(
                passive_monthly * Decimal(12) * disability_rate * passive_annuity
            )
            future_amount = round_currency(future_active + future_passive)
        else:
            total_annuity = compute_annuity_due(table, calc_age, discount_rate)
            active_annuity = Decimal(0)
            passive_annuity = total_annuity
            future_active = Decimal("0.00")
            future_passive = round_currency(
                passive_monthly * Decimal(12) * disability_rate * total_annuity
            )
            future_amount = future_passive

        total_amount = round_currency(past_amount + future_amount)
        detail = {
            "method": "genel_sartlar",
            "life_table": table_name,
            "discount_rate": discount_rate,
            "calc_age": calc_age,
            "elapsed_months": elapsed_months,
            "past_amount": past_amount,
            "future_active_amount": future_active,
            "future_passive_amount": future_passive,
            "future_amount": future_amount,
            "active_annuity_factor": active_annuity,
            "passive_annuity_factor": passive_annuity,
            "total_annuity_factor": total_annuity,
            "amount": total_amount,
            "formula": (
                "T_gecmis = G_aylik * M * n_ay; "
                "T_gelecek = 12 * M * (G_aktif * ä_x_aktif + G_pasif * ä_x_pasif)"
            ),
        }
        return detail, used_keys

    def _calculate_yargitay(
        self,
        calc_age: int,
        gender: str,
        disability_rate: Decimal,
        active_monthly: Decimal,
        passive_monthly: Decimal,
        past_amount: Decimal,
        elapsed_months: Decimal,
        is_retired: bool,
        params: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        gs_cfg = params["genel_sartlar_2021"]
        active_end_age = int(gs_cfg["aktif_donem"][1])
        table_name = "TRH-2010"
        tables_dir = params.get("_base_dir")

        table = load_life_table(table_name, gender=gender, tables_dir=tables_dir)
        used_keys = ["eski_kural.yasam_tablosu", "genel_sartlar_2021.aktif_donem"]

        bakiye_omur = get_remaining_life_expectancy(table, calc_age)
        if calc_age < active_end_age and not is_retired:
            remaining_to_65 = Decimal(active_end_age - calc_age)
            active_years = min(bakiye_omur, max(Decimal(0), remaining_to_65))
        else:
            active_years = Decimal(0)
        passive_years = max(Decimal(0), bakiye_omur - active_years)

        future_active = round_currency(
            active_monthly * Decimal(12) * disability_rate * active_years
        )
        future_passive = round_currency(
            passive_monthly * Decimal(12) * disability_rate * passive_years
        )
        future_amount = round_currency(future_active + future_passive)
        total_amount = round_currency(past_amount + future_amount)

        detail = {
            "method": "yargitay",
            "life_table": table_name,
            "discount_rate": Decimal(0),
            "calc_age": calc_age,
            "elapsed_months": elapsed_months,
            "bakiye_omur_years": bakiye_omur,
            "active_years": active_years,
            "passive_years": passive_years,
            "past_amount": past_amount,
            "future_active_amount": future_active,
            "future_passive_amount": future_passive,
            "future_amount": future_amount,
            "amount": total_amount,
            "formula": (
                "T_gecmis = G_aylik * M * n_ay; "
                "T_gelecek = 12 * M * (G_aktif * Y_aktif + G_pasif * Y_pasif) [TRH-2010 + progresif rant]"
            ),
        }
        return detail, used_keys

    def compute(self, case: CaseInput, params: dict[str, Any]) -> dict[str, Any]:
        version = get_parameter_version(params)
        warnings: list[str] = []

        if case.disability_rate is None:
            return {
                "amount": Decimal("0.00"),
                "formula": "T_gecmis + T_gelecek",
                "inputs": {"disability_rate": None, "parameter_version": version},
                "parameter_version": version,
                "warnings": warnings,
            }

        disability_rate = to_decimal(case.disability_rate)
        if disability_rate <= Decimal(0):
            return {
                "amount": Decimal("0.00"),
                "formula": "T_gecmis + T_gelecek",
                "inputs": {
                    "disability_rate": disability_rate,
                    "parameter_version": version,
                },
                "parameter_version": version,
                "warnings": warnings,
            }

        if case.age is None:
            warnings.append("Yaş girilmedi: sürekli iş göremezlik hesaplanamadı")
            return {
                "amount": Decimal("0.00"),
                "formula": "T_gecmis + T_gelecek",
                "inputs": {
                    "disability_rate": disability_rate,
                    "age": None,
                    "parameter_version": version,
                },
                "parameter_version": version,
                "warnings": warnings,
            }

        gender = case.gender if case.gender in ("E", "K") else "E"
        if case.gender is None:
            warnings.append("Cinsiyet belirtilmediği için yaşam tablosunda 'E' varsayıldı")

        active_monthly, passive_monthly, income_warnings, used_keys = resolve_monthly_net_income(
            case, params
        )
        warnings.extend(income_warnings)

        elapsed_months = _compute_elapsed_months(case.accident_date, case.calculation_date)
        calc_age = min(99, case.age + int(elapsed_months // Decimal(12)))
        active_end_age = int(params["genel_sartlar_2021"]["aktif_donem"][1])
        is_retired = case.employment_status == "emekli"

        past_monthly_base = (
            passive_monthly if (is_retired or case.age >= active_end_age) else active_monthly
        )
        past_amount = round_currency(past_monthly_base * disability_rate * elapsed_months)
        if elapsed_months > Decimal(0):
            warnings.append(
                "Geçmiş (kazanılmış) dönem kaybı kaza tarihindeki tarihsel asgari ücret dilimleri yerine güncel dönem geliri/asgari ücreti ile hesaplanmıştır"
            )

        if case.method == "genel_sartlar":
            gs_detail, gs_keys = self._calculate_genel_sartlar(
                calc_age,
                gender,
                disability_rate,
                active_monthly,
                passive_monthly,
                past_amount,
                elapsed_months,
                is_retired,
                params,
            )
            used_keys.extend(gs_keys)
            primary = gs_detail
            methods_dict = {"genel_sartlar": gs_detail}
        elif case.method == "yargitay":
            yr_detail, yr_keys = self._calculate_yargitay(
                calc_age,
                gender,
                disability_rate,
                active_monthly,
                passive_monthly,
                past_amount,
                elapsed_months,
                is_retired,
                params,
            )
            used_keys.extend(yr_keys)
            primary = yr_detail
            methods_dict = {"yargitay": yr_detail}
            warnings.append(
                "Progresif rant formülü (%10 artırım / %10 iskonto) bilirkişi raporuyla doğrulanmalıdır"
            )
        else:
            gs_detail, gs_keys = self._calculate_genel_sartlar(
                calc_age,
                gender,
                disability_rate,
                active_monthly,
                passive_monthly,
                past_amount,
                elapsed_months,
                is_retired,
                params,
            )
            yr_detail, yr_keys = self._calculate_yargitay(
                calc_age,
                gender,
                disability_rate,
                active_monthly,
                passive_monthly,
                past_amount,
                elapsed_months,
                is_retired,
                params,
            )
            used_keys.extend(gs_keys + yr_keys)
            methods_dict = {
                "genel_sartlar": gs_detail,
                "yargitay": yr_detail,
            }
            if case.policy_date is not None and case.policy_date < date(2021, 12, 4):
                primary = yr_detail
                used_keys.append("genel_sartlar_2021.uygulama")
            else:
                primary = gs_detail
                if case.policy_date is None:
                    warnings.append(
                        "Poliçe tarihi (policy_date) girilmediğinden 04.12.2021 öncesi/sonrası ayrımı yapılamamış ve varsayılan olarak Genel Şartlar yöntemi seçilmiştir"
                    )
            warnings.append(
                "Hesap yöntemi (method) seçilmediğinden Genel Şartlar ve Yargıtay yöntemleri birlikte hesaplandı"
            )
            warnings.append(
                "Progresif rant ve anüite ara değerleri gerçek bilirkişi raporuyla doğrulanmalıdır"
            )

        warnings.extend(get_param_warnings(params, used_keys))

        inputs_payload: dict[str, Any] = {
            "age_at_accident": case.age,
            "calc_age": calc_age,
            "gender": gender,
            "disability_rate": disability_rate,
            "active_monthly_income": active_monthly,
            "passive_monthly_income": passive_monthly,
            "elapsed_months": elapsed_months,
            "past_amount": primary["past_amount"],
            "future_amount": primary["future_amount"],
            "selected_method": primary["method"],
            "methods": methods_dict,
            "parameter_version": version,
        }

        return {
            "amount": primary["amount"],
            "formula": primary["formula"],
            "inputs": inputs_payload,
            "parameter_version": version,
            "warnings": warnings,
        }
