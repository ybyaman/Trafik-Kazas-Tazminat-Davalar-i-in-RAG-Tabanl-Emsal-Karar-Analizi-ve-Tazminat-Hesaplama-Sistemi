"""Dava masrafları: başvurma harcı, nispi karar/ilam harcı, peşin harç, bilirkişi ve vekâlet ücreti (AAÜT)."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from trafikrag.calculation.parameters import (
    get_param_warnings,
    get_parameter_version,
    round_currency,
    to_decimal,
)


def _compute_aaut_fee(claim_amount: Decimal, aaut_cfg: dict[str, Any]) -> Decimal:
    """AAÜT kademeli tarife dilimlerine göre vekâlet ücretini hesaplar."""
    if claim_amount <= Decimal(0):
        return Decimal("0.00")

    remaining = claim_amount
    fee = Decimal(0)
    dilimler = aaut_cfg.get("dilimler", [])

    for bracket in dilimler:
        if remaining <= Decimal(0):
            break
        rate = to_decimal(bracket["oran"])
        slice_limit = bracket.get("dilim_tutari")
        if slice_limit is None:
            fee += remaining * rate
            remaining = Decimal(0)
        else:
            limit_dec = to_decimal(slice_limit)
            taxable = min(remaining, limit_dec)
            fee += taxable * rate
            remaining -= taxable

    maktu_min = to_decimal(aaut_cfg.get("maktu_alt_sinir", Decimal(0)))
    computed = round_currency(fee)
    if computed < maktu_min:
        computed = min(round_currency(claim_amount), round_currency(maktu_min))
    return computed


def calculate_costs(
    claim_amount: Decimal | int | str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Dava değeri üzerinden mahkeme harçlarını, tahmini bilirkişi ücretini ve AAÜT vekâlet ücretini hesaplar."""
    version = get_parameter_version(params)
    dava_degeri = round_currency(to_decimal(claim_amount))
    warnings: list[str] = []
    used_keys: list[str] = [
        "harc_2026.basvurma",
        "harc_2026.nispi_karar_ilam_binde",
        "harc_2026.pesin_harc",
        "harc_2026.bilirkisi_tahmini",
        "aaut_2026",
    ]

    harc_cfg = params["harc_2026"]
    basvurma_harci = round_currency(to_decimal(harc_cfg["basvurma"]))
    binde_oran = to_decimal(harc_cfg["nispi_karar_ilam_binde"])
    nispi_karar_ilam_harci = round_currency(dava_degeri * binde_oran / Decimal(1000))

    if harc_cfg.get("pesin_harc") is not None:
        pesin_harc_orani = to_decimal(harc_cfg["pesin_harc"])
        pesin_harc = round_currency(nispi_karar_ilam_harci * pesin_harc_orani)
    else:
        varsayim_oran = to_decimal(harc_cfg.get("pesin_harc_orani_varsayim", Decimal("0.25")))
        pesin_harc = round_currency(nispi_karar_ilam_harci * varsayim_oran)
        warnings.append(
            "Peşin harç oranı parametre dosyasında bulunamadı; nispi harcın 1/4'ü varsayımıyla tahmini hesaplandı (doğrulanmamıştır)"
        )

    bilirkisi_raw = params.get("expert_fee_override", harc_cfg.get("bilirkisi_tahmini", Decimal(0)))
    bilirkisi_ucreti = round_currency(to_decimal(bilirkisi_raw))

    aaut_cfg = params.get("aaut_2026")
    if aaut_cfg is None:
        vekalet_ucreti: Decimal | None = None
        warnings.append(
            "AAÜT 2026 değerleri parametre dosyasında henüz boş olduğundan vekâlet ücreti hesaplanmamıştır"
        )
    else:
        vekalet_ucreti = _compute_aaut_fee(dava_degeri, aaut_cfg)

    total_upfront_costs = round_currency(basvurma_harci + pesin_harc + bilirkisi_ucreti)
    total_judgment_costs = round_currency(
        basvurma_harci
        + nispi_karar_ilam_harci
        + bilirkisi_ucreti
        + (vekalet_ucreti if vekalet_ucreti is not None else Decimal("0.00"))
    )

    warnings.extend(get_param_warnings(params, used_keys))

    return {
        "label": "tahmini hesap",
        "claim_amount": dava_degeri,
        "basvurma_harci": basvurma_harci,
        "nispi_karar_ilam_harci": nispi_karar_ilam_harci,
        "pesin_harc": pesin_harc,
        "bilirkisi_ucreti": bilirkisi_ucreti,
        "bilirkisi_etiketi": "tahmini",
        "vekalet_ucreti": vekalet_ucreti,
        "total_upfront_costs": total_upfront_costs,
        "total_judgment_costs": total_judgment_costs,
        "formula": (
            "nispi_harc = dava_degeri * (binde_oran / 1000); "
            "pesin_harc = nispi_harc * pesin_harc_orani; "
            "toplam_acilis_masrafi = basvurma_harci + pesin_harc + bilirkisi_ucreti"
        ),
        "parameter_version": version,
        "warnings": warnings,
    }
