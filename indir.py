from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
import random
import re
import time
from typing import Dict, List, Optional, Set, Tuple
from bs4 import BeautifulSoup
from playwright.sync_api import Page, sync_playwright

# ==============================================================================
# 1. YAPILANDIRMA, SORGULAR VE KOTALAR (BUĞRA'NIN İSTERLERİ)
# ==============================================================================
HEDEF_CEKIRDEK_SAYISI = 400
HEDEF_HESAP_DOGRULAMA_SAYISI = 50
SAYFA_BASI_KAYIT = 10
MAKS_SAYFA_DERINLIGI = 15  # Her sorgu için taranacak maksimum sayfa

# Kota Oranları (Madde 5)
MAKS_MAHKEME_ORANI = 0.15  # Tek mahkeme/şehir en fazla %15 (400'de 60 karar)
MAKS_YIL_ORANI = 0.22  # Tek yıl en fazla %22 (400'de 88 karar)

DATA_DIR = "data"
TXT_DIR = os.path.join(DATA_DIR, "txt_dosyalar")
DISLANAN_DIR = os.path.join(DATA_DIR, "dislanan")
JSON_PATH = os.path.join(DATA_DIR, "uyap_emsal_kararlar.json")
DISLANAN_JSON_PATH = os.path.join(DATA_DIR, "dislanan_kararlar.json")

os.makedirs(TXT_DIR, exist_ok=True)
os.makedirs(DISLANAN_DIR, exist_ok=True)

# Madde 1: Çekirdek Yaralanma / Maluliyet Sorguları
CEKIRDEK_SORGULAR = [
    "trafik kazası sürekli iş göremezlik tazminatı bilirkişi",
    "kalıcı maluliyet kusur oranı aktüer hesap",
    "bakıcı gideri trafik kazası",
    "geçici iş göremezlik trafik kazası",
    "progresif rant TRH-2010",
    "progresif rant PMF-1931",
    "pasif dönem iş göremezlik tazminatı",
    "tedavi gideri zorunlu mali sorumluluk sigortası yaralanma",
    "müterafik kusur emniyet kemeri kask",
    "Güvence Hesabı yaralanma",
]

# Madde 6: Hesap Doğrulama Alt Kümesi Sorguları
HESAP_DOGRULAMA_SORGULARI = [
    "trafik kazası aktüer bilirkişi raporu net tutar",
    "sürekli iş göremezlik yıllık ücret bilinen gelir",
    "maluliyet kusur oranı aktüer hesap bilinen gelir",
    "iş göremezlik tazminatı bakiye ömür TRH-2010 net tutar",
]


# ==============================================================================
# 2. METİN TEMİZLEME VE KALİTE KAPISI FONKSİYONLARI (MADDE 3, 4, 5, 6)
# ==============================================================================
def metin_hash_al(metin: str) -> str:
  norm_metin = re.sub(r"\s+", "", metin.lower())
  return hashlib.md5(norm_metin.encode("utf-8")).hexdigest()


def birlesik_anahtar_olustur(daire: str, esas: str, karar: str) -> str:
  ham = f"{daire}_{esas}_{karar}".lower()
  return re.sub(r"\s+", "", ham)


def html_temizle(ham_html: str) -> str:
  if not ham_html:
    return ""
  soup = BeautifulSoup(ham_html, "html.parser")
  for tag in soup(["script", "style", "meta", "head"]):
    tag.decompose()
  metin = soup.get_text(separator="\n")
  satirlar = [
      re.sub(r"\s+", " ", satir).strip() for satir in metin.splitlines()
  ]
  return "\n".join([s for s in satirlar if s])


def yil_cikar(tarih_str: str, karar_no: str, esas_no: str) -> str:
  """Karar tarihinden veya Karar/Esas numarasından 4 haneli yılı ayıklar."""
  for metin in [tarih_str, karar_no, esas_no]:
    if not metin:
      continue
    eslesme = re.search(r"(20[0-2]\d|199\d)", str(metin))
    if eslesme:
      return eslesme.group(1)
  return "Bilinmiyor"


def sehir_veya_birim_koku_al(daire_adi: str) -> str:
  """Kota takibi için mahkemenin şehir/bölge kökünü çıkarır (Örn: 'İstanbul BAM', 'İstanbul Anadolu')."""
  d = daire_adi.strip()
  if not d:
    return "Bilinmiyor"
  if "yargıtay" in d.lower():
    # Yargıtay dairelerini kendi içinde grupla (Örn: Yargıtay 17. Hukuk Dairesi)
    return d
  parcalar = d.split()
  if len(parcalar) >= 2 and parcalar[1].lower() in ["anadolu", "bölge"]:
    return f"{parcalar[0]} {parcalar[1]}"
  return parcalar[0]


def derece_belirle(daire_adi: str) -> str:
  """Mahkeme derecesini sınıflandırır."""
  d_low = daire_adi.lower()
  if "yargıtay" in d_low or "hukuk genel kurulu" in d_low:
    return "Yargıtay"
  if "bölge adliye" in d_low or "bam" in d_low:
    return "BAM (İstinaf)"
  return "İlk Derece Mahkemesi"


def hesap_dogrulama_kontrolu(metin: str) -> bool:
  """Madde 6: Yaş, gelir/ücret, maluliyet, kusur ve TL tutarın birlikte geçtiği kararları tespit eder."""
  m_low = metin.lower()
  yas_var = bool(
      re.search(
          r"(\b\d{1,2}\s*yaş|\byaşında\b|\bdoğumlu\b|\bbakiye\s*ömür)", m_low
      )
  )
  gelir_var = bool(
      re.search(
          r"(gelir|ücret|asgari\s*ücret|kazanç|maaş|bilinen\s*dönem)", m_low
      )
  )
  maluliyet_var = bool(
      re.search(r"(%\s*\d+|\byüzde\s*\d+|maluliyet\s*oran|iş\s*göremezlik)", m_low)
  )
  kusur_var = "kusur" in m_low
  tutar_var = bool(re.search(r"(\d{1,3}(?:\.\d{3})+|\d+)\s*(?:,\d{2})?\s*(?:tl|türk\s*lirası)", m_low))

  return yas_var and gelir_var and maluliyet_var and kusur_var and tutar_var


def kalite_ve_cekirdek_kontrolu(metin: str) -> Tuple[bool, str]:
  """Madde 3 & Madde 4: Kararın çekirdek kriterlerini ve kalite kapısını geçip geçmediğini denetler.

  Dönüş: (Gecti_Mi, Elenme_Sebebi)
  """
  kelimeler = metin.split()
  if len(kelimeler) < 400:
    return False, f"KISA_METIN ({len(kelimeler)} kelime < 400)"

  m_low = metin.lower()

  # Hüküm bölümü kontrolü
  hukum_kaliplari = [
      "hüküm",
      "karar :",
      "karar:",
      "sonuç ve istem",
      "açıklanan nedenlerle",
      "gerekçesiyle",
      "oybirliğiyle",
      "oy çokluğuyla",
  ]
  if not any(k in m_low for k in hukum_kaliplari):
    return False, "HUKUM_BOLUMU_YOK"

  # Kusur kelimesi kontrolü
  if "kusur" not in m_low:
    return False, "KUSUR_KELIMESI_YOK"

  # Maluliyet / İş göremezlik / Beden gücü kontrolü
  maluliyet_kaliplari = [
      "maluliyet",
      "malûliyet",
      "iş göremezlik",
      "işgöremezlik",
      "beden gücü",
      "meslekte kazanma",
  ]
  if not any(k in m_low for k in maluliyet_kaliplari):
    return False, "MALULIYET_ISGOREMEZLIK_YOK"

  # Çekirdek kontrolü: Sadece ölüm / destekten yoksun kalma ağırlıklıysa ve yaralanma/maluliyet tali ise ele
  destek_sayisi = m_low.count("destekten yoksun") + m_low.count("ölüm")
  yaralanma_sayisi = (
      m_low.count("maluliyet")
      + m_low.count("iş göremezlik")
      + m_low.count("işgöremezlik")
      + m_low.count("yaralan")
      + m_low.count("bakıcı")
  )
  if destek_sayisi > (yaralanma_sayisi * 2) and yaralanma_sayisi < 2:
    return False, "DESTEKTEN_YOKSUN_AGIRLIKLI"

  return True, "CEKIRDEK_ONAYLANDI"


def kota_kontrolu(
    daire: str, yil: str, mevcut_cekirdekler: List[Dict]
) -> Tuple[bool, str]:
  """Madde 5: Tek mahkeme/şehir %15'i, tek yıl %22'yi geçmesin."""
  mahkeme_limiti = int(HEDEF_CEKIRDEK_SAYISI * MAKS_MAHKEME_ORANI)  # 60
  yil_limiti = int(HEDEF_CEKIRDEK_SAYISI * MAKS_YIL_ORANI)  # 88

  # Tam mahkeme adı sayımı
  mahkeme_sayaci = sum(
      1 for k in mevcut_cekirdekler if k.get("daire", "") == daire
  )
  if mahkeme_sayaci >= mahkeme_limiti:
    return False, f"MAHKEME_KOTASI_DOLU ({daire}: {mahkeme_sayaci}/{mahkeme_limiti})"

  # Şehir / Bölge kökü sayımı (Örn: İstanbul BAM, İstanbul Anadolu)
  birim_koku = sehir_veya_birim_koku_al(daire)
  kok_sayaci = sum(
      1
      for k in mevcut_cekirdekler
      if sehir_veya_birim_koku_al(k.get("daire", "")) == birim_koku
  )
  if kok_sayaci >= mahkeme_limiti:
    return False, f"BOLGE_KOTASI_DOLU ({birim_koku}: {kok_sayaci}/{mahkeme_limiti})"

  # Yıl sayımı
  if yil != "Bilinmiyor":
    yil_sayaci = sum(1 for k in mevcut_cekirdekler if k.get("yil", "") == yil)
    if yil_sayaci >= yil_limiti:
      return False, f"YIL_KOTASI_DOLU ({yil}: {yil_sayaci}/{yil_limiti})"

  return True, "KOTA_UYGUN"


# ==============================================================================
# 3. DOSYA YÖNETİMİ VE MEVCUT VERİLERİ GÖÇÜRME (MIGRATION)
# ==============================================================================
def karari_diske_yaz(
    karar_paketi: Dict,
    sira_no: int,
    tum_liste: List[Dict],
    cekirdek_mi: bool = True,
    sadece_txt: bool = False,
):
  klasor = TXT_DIR if cekirdek_mi else DISLANAN_DIR
  on_ek = "karar" if cekirdek_mi else "dislanan"
  json_hedef = JSON_PATH if cekirdek_mi else DISLANAN_JSON_PATH

  txt_yolu = os.path.join(klasor, f"{on_ek}_{sira_no}.txt")
  baslik = (
      f"SIRA NO: {sira_no} ({'ÇEKİRDEK' if cekirdek_mi else 'DIŞLANAN - ' + karar_paketi.get('elenme_sebebi', '')})\n"
      f"DOKÜMAN ID: {karar_paketi.get('id', 'Bilinmiyor')}\n"
      f"DERECE: {karar_paketi.get('derece', 'Belirtilmemiş')}\n"
      f"DAİRE / MAHKEME: {karar_paketi.get('daire', 'Belirtilmemiş')}\n"
      f"ESAS NO: {karar_paketi.get('esas_no', 'Belirtilmemiş')}\n"
      f"KARAR NO: {karar_paketi.get('karar_no', 'Belirtilmemiş')}\n"
      f"KARAR TARİHİ: {karar_paketi.get('tarih', 'Belirtilmemiş')} (Yıl: {karar_paketi.get('yil', '')})\n"
      f"ARAMA KELİMESİ: {karar_paketi.get('arama_kelimesi', ' önceki_çalıştırma')}\n"
      f"SAYFA NO: {karar_paketi.get('sayfa_no', 0)}\n"
      f"İNDİRME ZAMANI: {karar_paketi.get('indirme_zamani', '')}\n"
      f"HESAP DOĞRULAMA ADAYI: {karar_paketi.get('hesap_dogrulama_adayi', False)}\n"
      f"İÇERİK HASH (MD5): {karar_paketi.get('icerik_hash', '')}\n"
      + ("=" * 80)
      + "\n\n"
  )

  with open(txt_yolu, "w", encoding="utf-8") as f:
    f.write(baslik + karar_paketi.get("icerik", ""))

  if not sadece_txt:
    with open(json_hedef, "w", encoding="utf-8") as jf:
      json.dump(tum_liste, jf, ensure_ascii=False, indent=4)


def mevcut_verileri_yukle_ve_siniflandir() -> Tuple[List[Dict], List[Dict]]:
  """Önceki çalıştırmadan kalan 300 kararı okur, Buğra'nın yeni kalite ve kota kurallarına göre

  çekirdek olanları korur, elenenleri silmeden 'data/dislanan/' klasörüne taşır.
  """
  cekirdek_liste: List[Dict] = []
  dislanan_liste: List[Dict] = []
  gorulen_hashler: Set[str] = set()
  gorulen_anahtarlar: Set[str] = set()

  # Önce varsa eski dışlananları yükle
  if os.path.exists(DISLANAN_JSON_PATH):
    try:
      with open(DISLANAN_JSON_PATH, "r", encoding="utf-8") as f:
        eski_dislananlar = json.load(f)
        for k in eski_dislananlar:
          h = k.get("icerik_hash") or metin_hash_al(k.get("icerik", ""))
          if h not in gorulen_hashler:
            gorulen_hashler.add(h)
            dislanan_liste.append(k)
    except Exception:
      pass

  # Ana JSON'ı yükle ve yeni filtrelere tabi tut
  if os.path.exists(JSON_PATH):
    try:
      with open(JSON_PATH, "r", encoding="utf-8") as f:
        ham_veriler = json.load(f)

      for k in ham_veriler:
        icerik = k.get("icerik", "")
        if len(icerik) < 50:
          continue
        h = k.get("icerik_hash") or metin_hash_al(icerik)
        daire = k.get("daire", "")
        esas = k.get("esas_no", "")
        karar_no = k.get("karar_no", "")
        tarih = k.get("tarih", "")
        anahtar = birlesik_anahtar_olustur(daire, esas, karar_no)

        if h in gorulen_hashler or anahtar in gorulen_anahtarlar:
          continue

        gorulen_hashler.add(h)
        gorulen_anahtarlar.add(anahtar)

        # Eksik metadata alanlarını tamamla (Madde 7)
        k["icerik_hash"] = h
        k["yil"] = k.get("yil") or yil_cikar(tarih, karar_no, esas)
        k["derece"] = k.get("derece") or derece_belirle(daire)
        k["arama_kelimesi"] = k.get(
            "arama_kelimesi", "trafik kazası maddi manevi tazminat kusur (v3)"
        )
        k["sayfa_no"] = k.get("sayfa_no", 1)
        k["indirme_zamani"] = k.get(
            "indirme_zamani", datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        )
        k["hesap_dogrulama_adayi"] = hesap_dogrulama_kontrolu(icerik)

        kalite_ok, sebep = kalite_ve_cekirdek_kontrolu(icerik)
        if kalite_ok:
          kota_ok, kota_sebep = kota_kontrolu(daire, k["yil"], cekirdek_liste)
          if kota_ok:
            k["cekirdek_mi"] = True
            k["elenme_sebebi"] = ""
            cekirdek_liste.append(k)
          else:
            k["cekirdek_mi"] = False
            k["elenme_sebebi"] = kota_sebep
            dislanan_liste.append(k)
        else:
          k["cekirdek_mi"] = False
          k["elenme_sebebi"] = sebep
          dislanan_liste.append(k)

      # Eski txt dosyalarını temizleyip yeni sınıflandırmaya göre baştan yaz
      for f_adi in os.listdir(TXT_DIR):
        if f_adi.endswith(".txt"):
          os.remove(os.path.join(TXT_DIR, f_adi))

      for idx, c_karar in enumerate(cekirdek_liste, start=1):
        karari_diske_yaz(c_karar, idx, cekirdek_liste, True, sadece_txt=True)

      for idx, d_karar in enumerate(dislanan_liste, start=1):
        karari_diske_yaz(d_karar, idx, dislanan_liste, False, sadece_txt=True)

      with open(JSON_PATH, "w", encoding="utf-8") as jf:
        json.dump(cekirdek_liste, jf, ensure_ascii=False, indent=4)
      with open(DISLANAN_JSON_PATH, "w", encoding="utf-8") as df:
        json.dump(dislanan_liste, df, ensure_ascii=False, indent=4)

      print(
          f"[*] Mevcut veriler yeni kalite/kota kapısından geçirildi:"
          f" {len(cekirdek_liste)} Çekirdek onaylandı, {len(dislanan_liste)}"
          " karar 'data/dislanan/' klasörüne ayrıldı."
      )
    except Exception as e:
      print(f"[!] Mevcut JSON işlenirken uyarı: {e}")

  return cekirdek_liste, dislanan_liste


# ==============================================================================
# 4. ETİK RATE-LIMIT BEKLEME (ÇEREZ SİLMEDEN PASİF SOĞUTMA)
# ==============================================================================
def ekrana_durum_yaz(page: Page, mesaj: str, renk: str = "#10b981"):
  js = """
    ({ mesaj, renk }) => {
        let kutu = document.getElementById('rag-durum-paneli');
        if (!kutu) {
            kutu = document.createElement('div');
            kutu.id = 'rag-durum-paneli';
            kutu.style.cssText = 'position:fixed;top:15px;right:15px;z-index:999999;background:#0f172a;color:#fff;padding:14px 20px;border-radius:8px;font-family:monospace;font-size:13px;box-shadow:0 10px 25px rgba(0,0,0,0.5);border-left:6px solid ' + renk + ';max-width:520px;';
            document.body.appendChild(kutu);
        }
        kutu.style.borderLeftColor = renk;
        kutu.innerHTML = '<b>⚖️ UYAP & Yargıtay RAG Scraper V4</b><br><span style="color:#cbd5e1;margin-top:5px;display:block;">' + mesaj + '</span>';
    }
    """
  try:
    page.evaluate(js, {"mesaj": mesaj, "renk": renk})
  except Exception:
    pass


def hiz_siniri_pasif_bekleme(page: Page, kademe: int):
  """ÖNEMLİ KURAL: Çerez silmez! IP ban riskine karşı oturumu koruyarak dakikalarca bekler."""
  # Kademe 1: 60 sn, Kademe 2: 120 sn, Kademe 3+: 210 sn
  toplam_saniye = min(60 * kademe, 240) + random.randint(5, 20)
  print(
      f"\n    [⏳] UYAP Hız Sınırı Algılandı (Kademe {kademe}). Çerezler"
      f" silinmeden {toplam_saniye} saniye pasif soğutma yapılıyor..."
  )

  kalan = toplam_saniye
  while kalan > 0:
    ekrana_durum_yaz(
        page,
        f"⏳ Hız Sınırı Koruması (Çerez Silinmiyor)<br>Kalan soğutma süresi:"
        f" <b>{kalan} saniye</b>...",
        "#f59e0b",
    )
    uyku = min(5, kalan)
    time.sleep(uyku)
    kalan -= uyku


# ==============================================================================
# 5. UYAP & YARGITAY REST API MOTORU (MADDE 2 DAHİL)
# ==============================================================================
def api_sayfa_listesi_cek(
    page: Page,
    kelime: str,
    sayfa_no: int,
    ozel_daire: Optional[str] = None,
) -> Optional[List[Dict]]:
  """'/aramalist' ucuna sorgu atar. Yargıtay modunda daire filtresini de gönderir."""
  js_kodu = """
    async ({ kelime, sayfaNo, kayitSayisi, ozelDaire }) => {
        const dataObj = {
            aranan: kelime,
            arananKelime: kelime,
            pageSize: kayitSayisi,
            pageNumber: sayfaNo
        };
        if (ozelDaire) {
            dataObj.birimYrgHukukDaire = ozelDaire;
            dataObj.hukuk = ozelDaire;
        }
        try {
            const res = await fetch('/aramalist', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json; charset=UTF-8',
                    'Accept': 'application/json, text/plain, */*',
                    'X-Requested-With': 'XMLHttpRequest'
                },
                body: JSON.stringify({ data: dataObj })
            });
            if (!res.ok) return { hata: 'HTTP_' + res.status };
            const json = await res.json();
            if (json && json.data) {
                if (Array.isArray(json.data)) return { kayitlar: json.data };
                if (Array.isArray(json.data.data)) return { kayitlar: json.data.data };
            }
            return { hata: 'BOS_VERI' };
        } catch (err) {
            return { hata: err.toString() };
        }
    }
    """
  for deneme in range(1, 4):
    try:
      sonuc = page.evaluate(
          js_kodu,
          {
              "kelime": kelime,
              "sayfaNo": sayfa_no,
              "kayitSayisi": SAYFA_BASI_KAYIT,
              "ozelDaire": ozel_daire,
          },
      )
      if (
          sonuc
          and isinstance(sonuc, dict)
          and "kayitlar" in sonuc
          and len(sonuc["kayitlar"]) > 0
      ):
        return sonuc["kayitlar"]

      if sonuc and sonuc.get("hata") == "BOS_VERI" and sayfa_no > 1:
        # O sorguda sayfa bittiyse boşuna soğutma yapma
        return None

      hiz_siniri_pasif_bekleme(page, deneme)
    except Exception:
      time.sleep(10.0)
  return None


def api_tekil_dokuman_cek(page: Page, karar_id: str, esas_no: str) -> Optional[str]:
  """'/getDokuman?id=' ucundan tam metni alır. Hız sınırında çerez silmeden bekler."""
  js_kodu = """
    async (id) => {
        try {
            const res = await fetch(`/getDokuman?id=${id}`, {
                method: 'GET',
                headers: {
                    'Accept': 'application/json, text/plain, */*',
                    'X-Requested-With': 'XMLHttpRequest'
                }
            });
            if (!res.ok) return { hata: 'HTTP_' + res.status };
            const veri = await res.json();
            if (veri && typeof veri.data === 'string' && veri.data.length > 50) {
                return { html: veri.data };
            }
            return { hata: 'ICERIK_BOS' };
        } catch (e) {
            return { hata: e.toString() };
        }
    }
    """
  for deneme in range(1, 4):
    try:
      yanit = page.evaluate(js_kodu, str(karar_id))
      if yanit and isinstance(yanit, dict) and "html" in yanit:
        return yanit["html"]

      hiz_siniri_pasif_bekleme(page, deneme)
    except Exception:
      time.sleep(10.0)
  return None


# ==============================================================================
# 6. DAĞILIM RAPORU FONKSİYONU (MADDE 8)
# ==============================================================================
def dagilim_raporu_yazdir(cekirdek_liste: List[Dict], dislanan_liste: List[Dict]):
  print("\n" + "=" * 80)
  print(" 📊 ÇALIŞTIRMA SONU VERİ SETİ DAĞILIM RAPORU (MADDE 8)")
  print("=" * 80)
  hesap_sayisi = sum(
      1 for k in cekirdek_liste if k.get("hesap_dogrulama_adayi")
  )
  print(
      f" • Toplam Çekirdek Karar Sayısı        : {len(cekirdek_liste)} /"
      f" {HEDEF_CEKIRDEK_SAYISI}"
  )
  print(
      f" • Hesap Doğrulama Alt Kümesi Sayısı   : {hesap_sayisi} (Hedef: min"
      f" {HEDEF_HESAP_DOGRULAMA_SAYISI})"
  )
  print(
      f" • Dışlanan (data/dislanan/) Sayısı    : {len(dislanan_liste)}"
  )

  # Derece Dağılımı
  derece_sayac = Counter(k.get("derece", "Bilinmiyor") for k in cekirdek_liste)
  print("\n [1] DERECE DAĞILIMI:")
  for der, adet in derece_sayac.most_common():
    oran = (adet / len(cekirdek_liste) * 100) if cekirdek_liste else 0
    print(f"     - {der:<28}: {adet:>3} adet (%{oran:.1f})")

  # Yıl Dağılımı
  yil_sayac = Counter(k.get("yil", "Bilinmiyor") for k in cekirdek_liste)
  print("\n [2] YIL DAĞILIMI (Kota: Maks %22):")
  for yil, adet in sorted(yil_sayac.items(), reverse=True):
    oran = (adet / len(cekirdek_liste) * 100) if cekirdek_liste else 0
    print(f"     - {yil:<28}: {adet:>3} adet (%{oran:.1f})")

  # Mahkeme / Bölge Kökü Dağılımı (İlk 12)
  kok_sayac = Counter(
      sehir_veya_birim_koku_al(k.get("daire", "")) for k in cekirdek_liste
  )
  print("\n [3] BÖLGE / MAHKEME KÖKÜ DAĞILIMI (Kota: Maks %15 - İlk 12):")
  for kok, adet in kok_sayac.most_common(12):
    oran = (adet / len(cekirdek_liste) * 100) if cekirdek_liste else 0
    print(f"     - {kok:<28}: {adet:>3} adet (%{oran:.1f})")

  # Elenme Sebepleri Özeti
  if dislanan_liste:
    sebep_sayac = Counter(
        k.get("elenme_sebebi", "Bilinmiyor").split(" (")[0]
        for k in dislanan_liste
    )
    print("\n [4] DIŞLANAN KARARLARIN ELENME SEBEPLERİ:")
    for sebep, adet in sebep_sayac.most_common():
      print(f"     - {sebep:<28}: {adet:>3} adet")

  print("=" * 80 + "\n")


# ==============================================================================
# 7. PORTAL TARAMA DÖNGÜSÜ VE MAIN PIPELINE
# ==============================================================================
def portal_uzerinden_sorgulari_tara(
    page: Page,
    portal_url: str,
    sorgu_listesi: List[Tuple[str, Optional[str]]],
    cekirdek_liste: List[Dict],
    dislanan_liste: List[Dict],
    gorulen_anahtarlar: Set[str],
    gorulen_hashler: Set[str],
    gorulen_idler: Set[str],
):
  print(f"\n[*] Portal bağlantısı kuruluyor: {portal_url}")
  try:
    page.goto(portal_url, wait_until="domcontentloaded", timeout=60000)
    time.sleep(4.0)
  except Exception as e:
    print(f"[-] {portal_url} açılamadı, geçiliyor: {e}")
    return

  for sorgu_kelimesi, ozel_daire in sorgu_listesi:
    if len(cekirdek_liste) >= HEDEF_CEKIRDEK_SAYISI:
      break

    etiket = (
        f"{sorgu_kelimesi} [{ozel_daire}]" if ozel_daire else sorgu_kelimesi
    )
    print(f"\n🔍 SORGU BAŞLATILDI: '{etiket}'")

    for sayfa_no in range(1, MAKS_SAYFA_DERINLIGI + 1):
      if len(cekirdek_liste) >= HEDEF_CEKIRDEK_SAYISI:
        break

      ekrana_durum_yaz(
          page,
          f"Sorgu: <b>{etiket}</b> (Sayfa {sayfa_no})<br>Çekirdek:"
          f" <b>{len(cekirdek_liste)}/{HEDEF_CEKIRDEK_SAYISI}</b> | Dışlanan:"
          f" {len(dislanan_liste)}",
      )

      kayitlar = api_sayfa_listesi_cek(
          page, sorgu_kelimesi, sayfa_no, ozel_daire
      )
      if not kayitlar:
        print(
            f"    [*] '{etiket}' için Sayfa {sayfa_no} boş döndü, sonraki"
            " sorguya geçiliyor."
        )
        break

      yeni_dokuman_denendi = False

      for kayit in kayitlar:
        if len(cekirdek_liste) >= HEDEF_CEKIRDEK_SAYISI:
          break

        karar_id = str(kayit.get("id", ""))
        daire = kayit.get("birimAdi") or kayit.get("daire") or (ozel_daire or "")
        if "yargitay.gov.tr" in portal_url and "yargıtay" not in daire.lower():
          daire = f"Yargıtay {daire}".strip()

        esas_no = kayit.get("esasNo", "")
        karar_no = kayit.get("kararNo", "")
        tarih = kayit.get("kararTarihi", "")
        durum = kayit.get("kararDurumu", "")

        ortak_anahtar = birlesik_anahtar_olustur(daire, esas_no, karar_no)
        if (
            not karar_id
            or karar_id in gorulen_idler
            or ortak_anahtar in gorulen_anahtarlar
        ):
          continue

        # Ön Kota Kontrolü: Eğer bu mahkemenin veya yılın kotası zaten doluysa,
        # sunucuya boşuna getDokuman isteği atıp rate-limit harcama!
        aday_yil = yil_cikar(tarih, karar_no, esas_no)
        on_kota_ok, _ = kota_kontrolu(daire, aday_yil, cekirdek_liste)
        if not on_kota_ok:
          continue

        yeni_dokuman_denendi = True
        ham_html = api_tekil_dokuman_cek(page, karar_id, esas_no)
        if not ham_html:
          print(f"    [-] ID:{karar_id} (E.{esas_no}) alınamadı, atlanıyor.")
          continue

        temiz_metin = html_temizle(ham_html)
        yeni_hash = metin_hash_al(temiz_metin)

        gorulen_idler.add(karar_id)
        gorulen_anahtarlar.add(ortak_anahtar)

        if yeni_hash in gorulen_hashler:
          continue
        gorulen_hashler.add(yeni_hash)

        hesap_adayi = hesap_dogrulama_kontrolu(temiz_metin)
        kalite_ok, kalite_sebep = kalite_ve_cekirdek_kontrolu(temiz_metin)

        paket = {
            "id": karar_id,
            "derece": derece_belirle(daire),
            "daire": daire,
            "esas_no": esas_no,
            "karar_no": karar_no,
            "tarih": tarih,
            "yil": aday_yil,
            "durum": durum,
            "arama_kelimesi": etiket,
            "sayfa_no": sayfa_no,
            "indirme_zamani": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            "hesap_dogrulama_adayi": hesap_adayi,
            "icerik_hash": yeni_hash,
            "icerik": temiz_metin,
        }

        if kalite_ok:
          kota_ok, kota_sebep = kota_kontrolu(daire, aday_yil, cekirdek_liste)
          if kota_ok:
            paket["cekirdek_mi"] = True
            paket["elenme_sebebi"] = ""
            cekirdek_liste.append(paket)
            karari_diske_yaz(paket, len(cekirdek_liste), cekirdek_liste, True)
            hesap_etiket = " [🧮 HESAP ADAYI]" if hesap_adayi else ""
            print(
                f"    [+] ({len(cekirdek_liste)}/{HEDEF_CEKIRDEK_SAYISI})"
                f" ÇEKİRDEK: {daire} | E.{esas_no} ({aday_yil}){hesap_etiket}"
            )
          else:
            paket["cekirdek_mi"] = False
            paket["elenme_sebebi"] = kota_sebep
            dislanan_liste.append(paket)
            karari_diske_yaz(
                paket, len(dislanan_liste), dislanan_liste, False
            )
            print(
                f"    [↪] Dışlananlara ayrıldı ({kota_sebep}): {daire} |"
                f" E.{esas_no}"
            )
        else:
          paket["cekirdek_mi"] = False
          paket["elenme_sebebi"] = kalite_sebep
          dislanan_liste.append(paket)
          karari_diske_yaz(paket, len(dislanan_liste), dislanan_liste, False)
          print(
              f"    [↪] Dışlananlara ayrıldı ({kalite_sebep}): {daire} |"
              f" E.{esas_no}"
          )

        # İnsani bekleme süresi (Rate limit dostu: 2.5 - 4.2 saniye arası)
        time.sleep(random.uniform(2.5, 4.2))

      if yeni_dokuman_denendi:
        time.sleep(random.uniform(2.0, 3.5))


def main():
  print("=" * 80)
  print(" UYAP & YARGITAY RAG SCRAPER V4 (400 ÇEKİRDEK + KALİTE & KOTA KAPILI)")
  print("=" * 80)

  cekirdek_liste, dislanan_liste = mevcut_verileri_yukle_ve_siniflandir()

  tum_kayitlar = cekirdek_liste + dislanan_liste
  gorulen_anahtarlar: Set[str] = {
      birlesik_anahtar_olustur(
          k.get("daire", ""), k.get("esas_no", ""), k.get("karar_no", "")
      )
      for k in tum_kayitlar
  }
  gorulen_hashler: Set[str] = {
      k.get("icerik_hash", "") for k in tum_kayitlar if k.get("icerik_hash")
  }
  gorulen_idler: Set[str] = {
      str(k.get("id", "")) for k in tum_kayitlar if k.get("id")
  }

  if len(cekirdek_liste) >= HEDEF_CEKIRDEK_SAYISI:
    print(
        f"[+] Hedeflenen {HEDEF_CEKIRDEK_SAYISI} çekirdek karar zaten tamam!"
    )
    dagilim_raporu_yazdir(cekirdek_liste, dislanan_liste)
    return

  # UYAP Emsal sorgu kuyruğu (Önce Hesap Doğrulama + Çekirdek Sorgular)
  uyap_sorgu_kuyrugu: List[Tuple[str, Optional[str]]] = []
  for s in HESAP_DOGRULAMA_SORGULARI + CEKIRDEK_SORGULAR:
    uyap_sorgu_kuyrugu.append((s, None))

  # Yargıtay (17. HD ve 4. HD öncelikli) sorgu kuyruğu (Madde 2)
  yargitay_sorgu_kuyrugu: List[Tuple[str, Optional[str]]] = []
  for s in CEKIRDEK_SORGULAR[:5] + HESAP_DOGRULAMA_SORGULARI[:2]:
    yargitay_sorgu_kuyrugu.append((s, "17. Hukuk Dairesi"))
    yargitay_sorgu_kuyrugu.append((s, "4. Hukuk Dairesi"))

  with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,
        args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
    )
    context = browser.new_context(
        viewport=None,
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
    )
    page = context.new_page()

    try:
      # 1. ADIM: UYAP Emsal Üzerinden Çoklu Sorgu Taraması
      portal_uzerinden_sorgulari_tara(
          page,
          "https://emsal.uyap.gov.tr",
          uyap_sorgu_kuyrugu,
          cekirdek_liste,
          dislanan_liste,
          gorulen_anahtarlar,
          gorulen_hashler,
          gorulen_idler,
      )

      # 2. ADIM: Yargıtay Karar Arama (17. ve 4. Hukuk Dairesi) Taraması
      if len(cekirdek_liste) < HEDEF_CEKIRDEK_SAYISI:
        portal_uzerinden_sorgulari_tara(
            page,
            "https://karararama.yargitay.gov.tr",
            yargitay_sorgu_kuyrugu,
            cekirdek_liste,
            dislanan_liste,
            gorulen_anahtarlar,
            gorulen_hashler,
            gorulen_idler,
        )

    except KeyboardInterrupt:
      print(
          "\n[!] Kullanıcı tarafından durduruldu. Tüm veriler kaydedildi,"
          " dağılım raporu hazırlanıyor..."
      )
    except Exception as e:
      print(f"\n[-] Beklenmeyen Hata: {e}")
    finally:
      browser.close()

  # Madde 8: Çalıştırma sonu detaylı rapor
  dagilim_raporu_yazdir(cekirdek_liste, dislanan_liste)


if __name__ == "__main__":
  main()