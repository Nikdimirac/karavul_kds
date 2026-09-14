

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mlops.golden_dataset.scenario_generator import (  
    HareketTarziSecenegi,
    OlayTuru,
    RISK_MATRIX,
    _yasakli_eylem_kalibi_var_mi,
    altin_cikti_uret,
    secim,
    senaryo_gecerli_mi,
)

API_TABAN_URL = "http://localhost:8000"



PILOT_SENARYOLAR: List[Dict[str, Any]] = [
    {
        "il": "Van", "olay_turu": OlayTuru.DEPREM, "deprem_mw": 6.8,
        "rapor_metni": (
            "Van Erciş ilçesinde 6.8 büyüklüğünde bir deprem meydana geldi. "
            "Çok sayıda bina ağır hasar gördü, enkaz altında vatandaş olduğu "
            "bildiriliyor. Erciş-Van karayolu heyelan nedeniyle trafiğe kapandı."
        ),
    },
    {
        "il": "Rize", "olay_turu": OlayTuru.SEL, "deprem_mw": None,
        "rapor_metni": (
            "Rize Çayeli ilçesinde son 8 saatte 178 mm'yi bulan aşırı yağış "
            "sonucu ani bir sel felaketi yaşandı. Bölgedeki dereler taştı, "
            "birçok ev su altında kaldı, mahsur kalan vatandaşlar var."
        ),
    },
    {
        "il": "Antalya", "olay_turu": OlayTuru.ORMAN_YANGINI, "deprem_mw": None,
        "rapor_metni": (
            "Antalya Manavgat ilçesinde kırsal kesimde çıkan orman yangını "
            "rüzgarın etkisiyle hızla büyüyerek yaklaşık 400 hektar alana "
            "yayıldı, yerleşim yerlerine yaklaşıyor."
        ),
    },
    {
        "il": "Kocaeli", "olay_turu": OlayTuru.KIMYASAL_PATLAMA, "deprem_mw": None,
        "rapor_metni": (
            "Kocaeli Dilovası'ndaki bir kimya fabrikasında büyük bir patlama "
            "ve ardından yangın meydana geldi. Zehirli duman çevreye "
            "yayılıyor, yakın mahallelerde tahliye ihtiyacı var."
        ),
    },
    {
        "il": "Mersin", "olay_turu": OlayTuru.LIMAN_YANGINI, "deprem_mw": None,
        "rapor_metni": (
            "Mersin Limanı'nda konteyner sahasında başlayan endüstriyel bir "
            "yangın kısa sürede büyüdü. Liman tesisleri ağır hasar aldı, "
            "gemi trafiği durduruldu."
        ),
    },
]



def _sifirla() -> None:
    try:
        requests.post(f"{API_TABAN_URL}/api/reset-scenario", timeout=30)
    except requests.RequestException as exc:
        print(f"  ⚠️ reset-scenario başarısız (devam ediliyor): {exc}")


def _analiz_et(rapor_metni: str) -> Dict[str, Any]:
    yanit = requests.post(
        f"{API_TABAN_URL}/api/analyze-crisis", json={"rapor_metni": rapor_metni}, timeout=180
    )
    yanit.raise_for_status()
    return yanit.json()




_BIRINCIL_BIRLIK_DESENI = re.compile(
    r"MÜDAHALE EDECEK BİRLİK: (?P<isim>[^(]+) \(Tip: (?P<tip>[^,]+), .*?"
    r"(?P<personel>\d+) personel\)\. Bu birlik, KRİZ NOKTASINA (?P<mesafe>[\d.]+) km uzaklıktadır"
)
_IKINCI_PLAN_BIRLIK_DESENI = re.compile(
    r"- (?P<isim>[^(]+) \(Tip: (?P<tip>[^,]+), (?P<personel>\d+) personel\), "
    r"KRİZ NOKTASINA (?P<mesafe>[\d.]+) km uzaklıkta"
)
_BULUNAMADI_DESENI = re.compile(r"MÜDAHALE EDECEK BİRLİK: BULUNAMADI")
_KAPALI_YOL_BASLIK_DESENI = re.compile(r"KRİZ NOKTASI: (?P<isim>[^(]+) \(KAPALI GÜZERGAH")
_ALTERNATIF_ROTA_DESENI = re.compile(r"Yakındaki AÇIK alternatif güzergahlar: (?P<liste>.+)")


def _gercekleri_ayikla(durum_ozeti: str) -> Dict[str, Any]:
  
    birincil = _BIRINCIL_BIRLIK_DESENI.search(durum_ozeti)
    ikinci_plan = _IKINCI_PLAN_BIRLIK_DESENI.search(durum_ozeti)
    bulunamadi = _BULUNAMADI_DESENI.search(durum_ozeti) is not None and birincil is None
    kapali_yollar = _KAPALI_YOL_BASLIK_DESENI.findall(durum_ozeti)
    alternatif_eslesme = _ALTERNATIF_ROTA_DESENI.search(durum_ozeti)

    return {
        "birincil_birlik": birincil.groupdict() if birincil else None,
        "ikinci_plan_birlik": ikinci_plan.groupdict() if ikinci_plan else None,
        "birincil_bulunamadi": bulunamadi,
        "kapali_yollar": kapali_yollar,
        "alternatif_rotalar": alternatif_eslesme.group("liste") if alternatif_eslesme else None,
    }


def _altin_yanit_insa_et(il: str, rapor_metni: str, durum_ozeti: str) -> str:

    gercekler = _gercekleri_ayikla(durum_ozeti)
    darbogazlar: List[str] = []

    if gercekler["kapali_yollar"]:
        for yol_ismi in gercekler["kapali_yollar"]:
            darbogazlar.append(secim("darbogaz_kapali_yol", yol=yol_ismi.strip()))
    if gercekler["alternatif_rotalar"]:
        darbogazlar.append(secim("darbogaz_alternatif", liste=gercekler["alternatif_rotalar"]))
    if not darbogazlar:
        darbogazlar.append(secim("darbogaz_yok"))

    if gercekler["birincil_birlik"]:
        b = gercekler["birincil_birlik"]
        alpha = HareketTarziSecenegi(
            kod_adi="ALPHA",
            ozet=secim("alpha_bulundu", isim=b["isim"].strip(), tip=b["tip"].strip()),
            kaynaklar=[b["isim"].strip()],
            tahmini_sure_dk=None,
            riskler=[secim("alpha_bulundu_risk", mesafe=b["mesafe"], personel=b["personel"])],
        )
    else:
        alpha = HareketTarziSecenegi(
            kod_adi="ALPHA",
            ozet=secim("alpha_bulunamadi"),
            kaynaklar=[],
            tahmini_sure_dk=None,
            riskler=[secim("alpha_bulunamadi_risk")],
        )

    if gercekler["ikinci_plan_birlik"]:
        g = gercekler["ikinci_plan_birlik"]
        bravo = HareketTarziSecenegi(
            kod_adi="BRAVO",
            ozet=secim("bravo_2plan", isim=g["isim"].strip(), tip=g["tip"].strip()),
            kaynaklar=[g["isim"].strip()],
            tahmini_sure_dk=None,
            riskler=[secim("bravo_2plan_risk", mesafe=g["mesafe"])],
        )
    else:
        bravo = HareketTarziSecenegi(
            kod_adi="BRAVO",
            ozet=secim("bravo_komsu_il"),
            kaynaklar=[],
            tahmini_sure_dk=None,
            riskler=[secim("bravo_komsu_il_risk")],
        )

    durum_sentezi = rapor_metni.strip()
    karar_sorusu = secim("karar_sorusu")
    return altin_cikti_uret(durum_sentezi, alpha, bravo, darbogazlar, karar_sorusu)



def main() -> int:
    print("=" * 90)
    print("KARAVUL — ALTIN VERİ SETİ — PİLOT TEST (5 örnek, HITL onayı bekleniyor)")
    print("=" * 90)

    for s in PILOT_SENARYOLAR:
        gecerli, sebep = senaryo_gecerli_mi(s["il"], s["olay_turu"], s.get("deprem_mw"))
        if not gecerli:
            print(f"🛑 PİLOT SENARYO GEÇERSİZ ({s['il']}/{s['olay_turu'].value}): {sebep}")
            return 1
    print(f"✅ 5 pilot senaryonun TAMAMI RISK_MATRIX doğrulamasından geçti.\n")

    uretilenler: List[str] = []
    for i, senaryo in enumerate(PILOT_SENARYOLAR, start=1):
        il = senaryo["il"]
        print(f"\n{'#' * 90}")
        print(f"# ÖRNEK {i}/5 — İl: {il}  |  Olay: {senaryo['olay_turu'].value}")
        print(f"{'#' * 90}")

        _sifirla()
        try:
            sonuc = _analiz_et(senaryo["rapor_metni"])
        except requests.RequestException as exc:
            print(f"  🛑 API çağrısı başarısız: {exc} — bu örnek atlanıyor.")
            continue

        durum_ozeti = sonuc.get("durum_ozeti") or ""
        if not durum_ozeti.strip():
            print("  🛑 durum_ozeti BOŞ döndü (rapor Bilgi Grafına işlenemedi olabilir) — bu örnek atlanıyor.")
            _sifirla()
            continue

        altin_yanit = _altin_yanit_insa_et(il, senaryo["rapor_metni"], durum_ozeti)
        ihlal = _yasakli_eylem_kalibi_var_mi(altin_yanit)

        print("\n--- SORU (rapor_metni) -------------------------------------------------")
        print(senaryo["rapor_metni"])

        print("\n--- CANLI VERİ BAĞLAMI (durum_ozeti — GERÇEK Neo4j/GraphRAG çıktısı) ---")
        print(durum_ozeti)

        print("\n--- KUSURSUZ YANIT (Altın Çıktı — DETERMİNİSTİK, gerçek verilerden inşa edildi) ---")
        print(altin_yanit)

        if ihlal:
            print(f"\n  🛑 YASAKLI EYLEM KALIBI TESPİT EDİLDİ: '{ihlal}' — BU ÖRNEK ALTIN VERİ SETİNE UYGUN DEĞİL.")
        else:
            print("\n  ✅ Yasaklı-eylem denetimi: temiz.")
            uretilenler.append(il)

        _sifirla()

    print(f"\n\n{'=' * 90}")
    print(f"PİLOT TEST TAMAMLANDI — {len(uretilenler)}/5 örnek temiz üretildi: {uretilenler}")
    print("Bu betik 1000'lik asıl döngüye OTOMATİK GEÇMEZ — sıradaki adım kullanıcının HITL onayı.")
    print("=" * 90)
    return 0


if __name__ == "__main__":
    sys.exit(main())
