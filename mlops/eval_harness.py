

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mlops.config import API_TABAN_URL, RAPORLAR_DIZINI, URETIM_ETIKETI  
from src.core.database import Neo4jConnection, Neo4jConnectionError  
from src.core.decision_engine import ( 
    DecisionEngine,
    _ingilizce_supheli_mi,
    _turkce_kucuk_harf,
    parse_oneri_maddeleri,
)

ISTEK_ZAMAN_ASIMI_SANIYE = 180.0

TAM_HAT_SENARYOLARI: List[str] = [
    "Gebze Kimyasal Cadde Deposu'nda patlama ve yangın meydana geldi. Bölgede "
    "tahliye gerekiyor, itfaiye ve sağlık ekibi sevk edilmeli.",
    "Antalya Manavgat ilçesinde orman yangını çıktı, yangın hızla yayılıyor ve "
    "Manavgat-Side karayolu trafiğe kapandı.",
    "Mersin Tarsus ilçesinde sel felaketi meydana geldi. Tarsus-Adana karayolu "
    "su baskını nedeniyle trafiğe kapandı.",
    "Elazığ Merkez'de 6.8 büyüklüğünde deprem meydana geldi. Elazığ-Sivrice "
    "karayolu heyelan sebebiyle çöktü, acil arama kurtarma ve sağlık ekibi "
    "sevki gerekiyor.",
]


_BULUNAMADI_DURUM_OZETLERI: List[tuple] = [
    (
        "Deprem/TestA",
        "## KRİTİK/YÜKSEK ŞİDDETLİ OLAYLAR\n"
        "KRİZ NOKTASI: Test Kriz Bölgesi A (OLAY — Deprem, şiddet: Yuksek, etki alanı ~2.0 km).\n"
        "  MÜDAHALE EDECEK BİRLİK: BULUNAMADI — bu KRİZ NOKTASINA, doğru yeteneğe sahip "
        "(bkz. aşağıdaki öncelik) ve AÇIK yol ağı üzerinden ulaşabilen HİÇBİR birlik yok "
        "(kapalı yollar mevcut birlikleri izole ediyor olabilir). Bu net bir ERİŞİM "
        "SORUNUDUR, öneri üretirken açıkça belirt.",
    ),
    (
        "Sel/TestB",
        "## KRİTİK/YÜKSEK ŞİDDETLİ OLAYLAR\n"
        "KRİZ NOKTASI: Test Kriz Bölgesi B (OLAY — Sel, şiddet: Yuksek, etki alanı ~1.5 km).\n"
        "  MÜDAHALE EDECEK BİRLİK: BULUNAMADI — bu KRİZ NOKTASINA, doğru yeteneğe sahip "
        "ve AÇIK yol ağı üzerinden ulaşabilen HİÇBİR birlik yok (kapalı yollar mevcut "
        "birlikleri izole ediyor olabilir). Bu net bir ERİŞİM SORUNUDUR, öneri üretirken "
        "açıkça belirt.",
    ),
    (
        "OrmanYangini/TestC",
        "## KRİTİK/YÜKSEK ŞİDDETLİ OLAYLAR\n"
        "KRİZ NOKTASI: Test Kriz Bölgesi C (OLAY — Orman Yangini, şiddet: Yuksek, "
        "etki alanı ~3.0 km).\n"
        "  MÜDAHALE EDECEK BİRLİK: BULUNAMADI — bu KRİZ NOKTASINA, doğru yeteneğe sahip "
        "ve AÇIK yol ağı üzerinden ulaşabilen HİÇBİR birlik yok (kapalı yollar mevcut "
        "birlikleri izole ediyor olabilir). Bu net bir ERİŞİM SORUNUDUR, öneri üretirken "
        "açıkça belirt.",
    ),
    (
        "KimyasalPatlama/TestD",
        "## KRİTİK/YÜKSEK ŞİDDETLİ OLAYLAR\n"
        "KRİZ NOKTASI: Test Kriz Bölgesi D (OLAY — Kimyasal Patlama, şiddet: Yuksek, "
        "etki alanı ~0.8 km).\n"
        "  MÜDAHALE EDECEK BİRLİK: BULUNAMADI — bu KRİZ NOKTASINA, doğru yeteneğe sahip "
        "ve AÇIK yol ağı üzerinden ulaşabilen HİÇBİR birlik yok (kapalı yollar mevcut "
        "birlikleri izole ediyor olabilir). Bu net bir ERİŞİM SORUNUDUR, öneri üretirken "
        "açıkça belirt.",
    ),
    (
        "Heyelan/TestE",
        "## KRİTİK/YÜKSEK ŞİDDETLİ OLAYLAR\n"
        "KRİZ NOKTASI: Test Kriz Bölgesi E (OLAY — Heyelan, şiddet: Yuksek, etki "
        "alanı ~1.0 km).\n"
        "  MÜDAHALE EDECEK BİRLİK: BULUNAMADI — bu KRİZ NOKTASINA, doğru yeteneğe sahip "
        "ve AÇIK yol ağı üzerinden ulaşabilen HİÇBİR birlik yok (kapalı yollar mevcut "
        "birlikleri izole ediyor olabilir). Bu net bir ERİŞİM SORUNUDUR, öneri üretirken "
        "açıkça belirt.",
    ),
]


_DURUST_BULUNAMADI_IFADELERI = (
    "bulunamadı",     
    "bulunmadı",          
    "bulunmamaktadır",  
    "tespit edilemedi",   
    "bulamadı",           
    "birlik yok",         
_ZORUNLU_FALLBACK_IFADESI = _DURUST_BULUNAMADI_IFADELERI[0]  
_BEKLENEN_TAM_CUMLE = "Uygun birlik bulunamadı, çevre illerden destek talep edilmelidir."


@dataclass
class KontrolSonucu:
    ad: str
    gecti: bool
    detay: str = ""


@dataclass
class SenaryoSonucu:
    senaryo: str
    metin: Optional[str] = None
    kontroller: List[KontrolSonucu] = field(default_factory=list)
    hata: Optional[str] = None

    @property
    def tamamen_gecti(self) -> bool:
        return self.hata is None and all(k.gecti for k in self.kontroller)



def _kontrol_bos_degil(oneriler: List[str]) -> KontrolSonucu:
    if oneriler and all(str(m).strip() for m in oneriler):
        return KontrolSonucu("Boş değil", True)
    return KontrolSonucu("Boş değil", False, "taktiksel_oneriler boş/eksik döndü")


_TEKRAR_DESENI = re.compile(r"\b(\w+)\b(?:\s+\1\b){5,}", re.IGNORECASE | re.UNICODE)


def _kontrol_tekrar_dongusu_yok(tum_metin: str) -> KontrolSonucu:
   
    eslesme = _TEKRAR_DESENI.search(tum_metin)
    if eslesme:
        return KontrolSonucu("Tekrar döngüsü yok", False, f"şüpheli tekrar: \"{eslesme.group(0)[:80]}...\"")
    return KontrolSonucu("Tekrar döngüsü yok", True)


def _kontrol_turkce_kilit(tum_metin: str) -> KontrolSonucu:
  
    if _ingilizce_supheli_mi(tum_metin):
        return KontrolSonucu("Türkçe kilidi", False, "çıktı İngilizce içeriyor gibi görünüyor")
    return KontrolSonucu("Türkçe kilidi", True)


def _kontrol_bulunamadi_sadakati(metin: str) -> KontrolSonucu:
   
    kucuk = _turkce_kucuk_harf(metin)
    if any(ifade in kucuk for ifade in _DURUST_BULUNAMADI_IFADELERI):
        return KontrolSonucu("BULUNAMADI sadakati", True)
    return KontrolSonucu(
        "BULUNAMADI sadakati", False,
        f"model, birlik yokken beklenen \"{_BEKLENEN_TAM_CUMLE}\" (veya eş anlamlı bir "
        f"ifade) yerine başka bir şey üretti (muhtemel HALÜSİNASYON): \"{metin[:160]}...\"",
    )




def _tam_hat_senaryosunu_calistir(db: Neo4jConnection, model_etiketi: str, rapor_metni: str) -> SenaryoSonucu:

    try:
        requests.post(f"{API_TABAN_URL}/api/reset-scenario", timeout=30).raise_for_status()
        yanit = requests.post(
            f"{API_TABAN_URL}/api/analyze-crisis",
            json={"rapor_metni": rapor_metni},
            timeout=ISTEK_ZAMAN_ASIMI_SANIYE,
        )
        yanit.raise_for_status()
    except requests.RequestException as exc:
        return SenaryoSonucu(senaryo=rapor_metni, hata=f"API isteği başarısız: {exc}")

    try:
        motor = DecisionEngine(db, model=model_etiketi)
        sonuc = motor.generate_recommendations()
        
        oneriler = parse_oneri_maddeleri(sonuc.get("oneriler_metni") or "")
    except Exception as exc:  
        requests.post(f"{API_TABAN_URL}/api/reset-scenario", timeout=30)
        return SenaryoSonucu(senaryo=rapor_metni, hata=f"DecisionEngine çağrısı başarısız: {exc}")
    finally:
        try:
            requests.post(f"{API_TABAN_URL}/api/reset-scenario", timeout=30).raise_for_status()
        except requests.RequestException:
            pass

    tum_metin = " ".join(str(m) for m in oneriler)
    kontroller = [
        _kontrol_bos_degil(oneriler),
        _kontrol_tekrar_dongusu_yok(tum_metin),
        _kontrol_turkce_kilit(tum_metin),
    ]
    return SenaryoSonucu(senaryo=rapor_metni, metin=tum_metin, kontroller=kontroller)


def main() -> int:
    ap = argparse.ArgumentParser(description="KARAVUL MLOps — model değerlendirme (eval) koşucusu")
    ap.add_argument("--etiket", required=True, help="Değerlendirilecek Ollama model etiketi (ör. aday)")
    ap.add_argument("--karsilastir", default=URETIM_ETIKETI, help="Yan yana karşılaştırılacak ikinci etiket (varsayılan: üretim)")
    ap.add_argument("--rapor-adi", default=None, help="Çıktı raporunun dosya adı (varsayılan: zaman damgalı)")
    args = ap.parse_args()

    try:
        yanit = requests.get(f"{API_TABAN_URL}/health", timeout=10)
        yanit.raise_for_status()
    except requests.RequestException as exc:
        print(f"✗ API ({API_TABAN_URL}) ayakta değil — önce `uvicorn api:app` başlatın: {exc}")
        return 1

    try:
        db = Neo4jConnection()
        db.connect()
    except Neo4jConnectionError as exc:
        print(f"✗ Neo4j'e bağlanılamadı: {exc}")
        return 1

    etiketler = [args.etiket, args.karsilastir]
    tum_sonuclar: Dict[str, List[SenaryoSonucu]] = {e: [] for e in etiketler}

    print("=" * 78)
    print(f"KARAVUL MLOps — DEĞERLENDİRME: '{args.etiket}' vs '{args.karsilastir}'")
    print("=" * 78)

    for etiket in etiketler:
        print(f"\n--- {etiket} ---")
        for i, rapor in enumerate(TAM_HAT_SENARYOLARI, 1):
            print(f"  [{i}/{len(TAM_HAT_SENARYOLARI)}] {rapor[:70]}...")
            sonuc = _tam_hat_senaryosunu_calistir(db, etiket, rapor)
            durum = "✅" if sonuc.tamamen_gecti else ("🛑" if sonuc.hata else "⚠️")
            print(f"      {durum} {'HATA: ' + sonuc.hata if sonuc.hata else ''}")
            tum_sonuclar[etiket].append(sonuc)


        print(f"  [BULUNAMADI sadakati — {len(_BULUNAMADI_DURUM_OZETLERI)} varyant × 3 mikro-görev]")
        motor = DecisionEngine(db, model=etiket)
        for varyant_adi, durum_ozeti in _BULUNAMADI_DURUM_OZETLERI:
            for gorev, zincir in (
                ("Lojistik", motor._lojistik_chain),
                ("Tahliye", motor._tahliye_chain),
                ("Sevk", motor._sevk_chain),
            ):
                etiket_adi = f"BULUNAMADI/{varyant_adi}/{gorev}"
                try:
                    cikti = zincir.invoke({"durum_ozeti": durum_ozeti, "cevresel_durum": "açık, sakin, yağış yok"})
                except Exception as exc: 
                    sonuc = SenaryoSonucu(senaryo=etiket_adi, hata=str(exc))
                else:
                    kontrol = _kontrol_bulunamadi_sadakati(cikti)
                    sonuc = SenaryoSonucu(senaryo=etiket_adi, metin=cikti, kontroller=[kontrol])
                durum = "✅" if sonuc.tamamen_gecti else ("🛑" if sonuc.hata else "⚠️")
                print(f"      {durum} {varyant_adi}/{gorev}")
                tum_sonuclar[etiket].append(sonuc)

    db.close()

    rapor_metni = _raporu_olustur(args.etiket, args.karsilastir, tum_sonuclar)
    dosya_adi = args.rapor_adi or f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{args.etiket}_vs_{args.karsilastir}.md"
    rapor_yolu = RAPORLAR_DIZINI / dosya_adi
    rapor_yolu.write_text(rapor_metni, encoding="utf-8")

    toplam_basarisiz = sum(1 for s in tum_sonuclar[args.etiket] if not s.tamamen_gecti)
    print("\n" + "=" * 78)
    print(f"Rapor yazıldı: {rapor_yolu}")
    print(f"'{args.etiket}': {len(tum_sonuclar[args.etiket]) - toplam_basarisiz}/{len(tum_sonuclar[args.etiket])} kontrol geçti.")
    print("=" * 78)
    return 0 if toplam_basarisiz == 0 else 2


def _raporu_olustur(etiket_a: str, etiket_b: str, sonuclar: Dict[str, List[SenaryoSonucu]]) -> str:
    satirlar = [
        f"# KARAVUL MLOps Değerlendirme Raporu",
        "",
        f"- Zaman (UTC): {datetime.now(timezone.utc).isoformat()}",
        f"- Aday/hedef etiket: `{etiket_a}`",
        f"- Karşılaştırma etiketi: `{etiket_b}`",
        "",
        "⚠️ Bu rapor SADECE mekanik kontrolleri listeler — NİHAİ 'canlıya "
        "alınsın mı' kararı İNSANA aittir (bkz. `mlops/promote_candidate.py`).",
        "",
    ]
    for etiket in (etiket_a, etiket_b):
        satirlar.append(f"## `{etiket}`")
        satirlar.append("")
        for s in sonuclar[etiket]:
            baslik = s.senaryo if s.senaryo.startswith("BULUNAMADI/") else s.senaryo[:90]
            if s.hata:
                satirlar.append(f"- 🛑 **{baslik}** — HATA: {s.hata}")
                continue
            ikon = "✅" if s.tamamen_gecti else "⚠️"
            satirlar.append(f"- {ikon} **{baslik}**")
            for k in s.kontroller:
                kikon = "✅" if k.gecti else "❌"
                satirlar.append(f"  - {kikon} {k.ad}" + (f" — {k.detay}" if k.detay else ""))
            if s.metin:
                satirlar.append(f"  - Çıktı: `{s.metin[:400]}`")
        satirlar.append("")
    return "\n".join(satirlar)


if __name__ == "__main__":
    sys.exit(main())
