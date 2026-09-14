

from __future__ import annotations

import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from src.core.database import Neo4jConnection, Neo4jConnectionError

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)




API_TABAN_URL = os.getenv("KARAVUL_API_BASE_URL", "http://localhost:8000")
VERI_SETI_DOSYASI = os.getenv("KARAVUL_DATASET_DOSYASI", "karavul_dataset.jsonl")

MAX_SUCCESSFUL_SCENARIOS = int(os.getenv("KARAVUL_MAX_BASARILI_SENARYO", "150"))


MAX_TOTAL_DENEME = int(os.getenv("KARAVUL_MAX_TOPLAM_DENEME", "1000"))


ISTEK_ZAMAN_ASIMI_SANIYE = float(os.getenv("KARAVUL_ISTEK_ZAMAN_ASIMI_SANIYE", "180"))


DONGU_BEKLEME_SANIYE = 4.0





_HEDEF_HAVUZU_LIMITI = 3000



def _yerlesim_temiz_ad(yerlesim: Dict[str, Any]) -> str:
    
    isim = yerlesim["isim"]
    if yerlesim.get("yerlesim_tipi") == "Ilce Merkezi":
        parantez_konumu = isim.rfind(" (")
        if parantez_konumu != -1:
            return isim[:parantez_konumu]
    return isim


def _yerlesim_konum_ifadesi(yerlesim: Dict[str, Any]) -> str:
    
    if yerlesim.get("yerlesim_tipi") == "Il Merkezi":
        return f"{yerlesim['il']} il merkezinde"
    return f"{yerlesim['il']} ili {_yerlesim_temiz_ad(yerlesim)} ilçesinde"




def _sel_senaryosu(yerlesim: Dict[str, Any]) -> str:
    yagis_mm = random.randint(70, 220)
    saat = random.randint(4, 24)
    return (
        f"{_yerlesim_konum_ifadesi(yerlesim)} son {saat} saatte {yagis_mm} mm'yi bulan aşırı yağış "
        f"sonucu ani bir sel felaketi yaşandı. Bölgedeki çok sayıda ev ve iş yeri su altında kaldı, "
        f"tahliye çalışmaları sürüyor."
    )


def _deprem_senaryosu(yerlesim: Dict[str, Any]) -> str:
    buyukluk = round(random.uniform(4.5, 7.8), 1)
    return (
        f"{_yerlesim_konum_ifadesi(yerlesim)} {buyukluk} büyüklüğünde bir deprem meydana geldi. "
        f"Çok sayıda bina ağır hasar gördü, bölgede arama kurtarma çalışmaları başlatıldı."
    )


def _orman_yangini_senaryosu(yerlesim: Dict[str, Any]) -> str:
    hektar = random.randint(50, 900)
    return (
        f"{_yerlesim_konum_ifadesi(yerlesim)} kırsal kesiminde çıkan orman yangını rüzgarın etkisiyle "
        f"hızla büyüyerek yaklaşık {hektar} hektarlık alanı etkisi altına aldı, yerleşim yerlerine "
        f"yaklaşıyor."
    )


def _zincirleme_kaza_senaryosu(otoyol: Dict[str, Any]) -> str:
    arac_sayisi = random.randint(8, 45)
    sebep = random.choice(["yoğun sis", "aşırı yağış", "buzlanma", "ani sürücü hatası"])
    return (
        f"{otoyol['isim']} üzerinde {sebep} nedeniyle {arac_sayisi} aracın karıştığı zincirleme "
        f"trafik kazası meydana geldi. Otoyol çift yönlü olarak trafiğe kapatıldı, çok sayıda yaralı "
        f"bulunuyor."
    )


def _liman_yangin_senaryosu(liman: Dict[str, Any]) -> str:
    varyant = random.choice(["depolama", "konteyner", "yakıt istasyonu", "gemi bakım"])
    return (
        f"{liman['isim']}'nda {varyant} alanında başlayan endüstriyel bir yangın kısa sürede büyüdü. "
        f"Liman tesisleri ağır hasar aldı, gemi trafiği durduruldu, zehirli duman bölgeye yayılıyor."
    )


def _hastane_senaryosu(hastane: Dict[str, Any]) -> str:
    varyant = random.choice(["yangin", "deprem", "teknik_ariza"])
    if varyant == "yangin":
        return (
            f"{hastane['isim']} bünyesinde jeneratör dairesinde başlayan bir yangın nedeniyle "
            f"hastanenin bir bölümü tahliye ediliyor, hasta nakli sürüyor."
        )
    if varyant == "deprem":
        buyukluk = round(random.uniform(4.8, 7.2), 1)
        return (
            f"Bölgede meydana gelen {buyukluk} büyüklüğündeki deprem {hastane['isim']}'ne ağır hasar "
            f"verdi, hastane hizmet veremiyor durumda."
        )
    return (
        f"{hastane['isim']}'nde ana elektrik sisteminde meydana gelen teknik arıza sonucu yoğun "
        f"bakım üniteleri risk altında, acil müdahale gerekiyor."
    )




_SENARYO_TANIMLARI: List[Tuple[str, str, Callable[[Dict[str, Any]], str]]] = [
    ("Sel", "Settlement", _sel_senaryosu),
    ("Deprem", "Settlement", _deprem_senaryosu),
    ("Orman Yangini", "Settlement", _orman_yangini_senaryosu),
    ("Zincirleme Kaza", "Otoyol", _zincirleme_kaza_senaryosu),
    ("Liman Yangini", "Liman", _liman_yangin_senaryosu),
    ("Hastane Krizi", "Hastane", _hastane_senaryosu),
]


class ProceduralInfrastructureCrisis:
    

    def __init__(self, db: Neo4jConnection) -> None:
        self._db = db
        self._havuzlar: Dict[str, List[Dict[str, Any]]] = {}
        self._havuzlari_yukle()

    def _havuzlari_yukle(self) -> None:
        print("[Hedef Havuzu] Neo4j'den GERÇEK Settlement/Otoyol/Liman/Hastane varlıkları okunuyor...")

        self._havuzlar["Settlement"] = self._db.execute_query(
            "MATCH (s:Settlement) "
            "RETURN s.isim AS isim, s.il AS il, s.yerlesim_tipi AS yerlesim_tipi, "
            "s.enlem AS enlem, s.boylam AS boylam "
            "LIMIT $limit",
            {"limit": _HEDEF_HAVUZU_LIMITI},
        )

        self._havuzlar["Otoyol"] = self._db.execute_query(
            "MATCH (i:Infrastructure) WHERE i.highway_tipi IN ['motorway','trunk'] AND i.aciklama IS NOT NULL "
            "WITH i.aciklama AS isim, head(collect(i)) AS ornek "
            "RETURN isim, ornek.enlem AS enlem, ornek.boylam AS boylam "
            "LIMIT $limit",
            {"limit": _HEDEF_HAVUZU_LIMITI},
        )

        self._havuzlar["Liman"] = self._db.execute_query(
            "MATCH (f:Facility {facility_type: 'Liman'}) "
            "RETURN COALESCE(f.aciklama, f.isim) AS isim, f.enlem AS enlem, f.boylam AS boylam "
            "LIMIT $limit",
            {"limit": _HEDEF_HAVUZU_LIMITI},
        )

        self._havuzlar["Hastane"] = self._db.execute_query(
            "MATCH (f:Facility {facility_type: 'Hastane'}) "
            "RETURN COALESCE(f.aciklama, f.isim) AS isim, f.enlem AS enlem, f.boylam AS boylam "
            "LIMIT $limit",
            {"limit": _HEDEF_HAVUZU_LIMITI},
        )

        for havuz_adi, kayitlar in self._havuzlar.items():
            print(f"      {havuz_adi}: {len(kayitlar)} gerçek varlık bulundu.")

        if not any(self._havuzlar.values()):
            raise RuntimeError(
                "Hiçbir hedef havuzu (Settlement/Otoyol/Liman/Hastane) dolu değil — "
                "Neo4j'de gerçek veri var mı kontrol edin (bkz. `seed_db.py`/`add_settlements.py`)."
            )

    def uret(self) -> Tuple[str, str]:
       
        uygun_tanimlar = [
            (tur, havuz_adi, sablon_fn)
            for (tur, havuz_adi, sablon_fn) in _SENARYO_TANIMLARI
            if self._havuzlar.get(havuz_adi)
        ]
        if not uygun_tanimlar:
            raise RuntimeError("Hiçbir kriz kategorisi için uygun hedef bulunamadı.")

        senaryo_turu, havuz_adi, sablon_fn = random.choice(uygun_tanimlar)
        hedef = random.choice(self._havuzlar[havuz_adi])
        return senaryo_turu, sablon_fn(hedef)



_KAPALI_ROTA_DESENI = re.compile(
    r"(rota|g[uü]zerg[aâ]h|yol)\w*[^.!?]{0,40}\bkapal[ıi]\b|\bkapal[ıi]\b[^.!?]{0,40}(rota|g[uü]zerg[aâ]h|yol)",
    re.IGNORECASE,
)

_ACIK_IFADESI_DESENI = re.compile(r"a[çc][ıi]k", re.IGNORECASE)


def gecerli_mi(sonuc: Dict[str, Any]) -> Tuple[bool, str]:
    

    Returns:
        `(gecerli, sebep)` — `gecerli=False` ise `sebep` REDDİN gerekçesini
        (terminale basılacak, insan-okur) bir cümle olarak taşır.
    """
    oneriler = sonuc.get("taktiksel_oneriler")
    if not isinstance(oneriler, list) or not oneriler:
        return False, "taktiksel_oneriler boş/eksik (motor bir öneri üretemedi)"

    tum_metin = " ".join(str(madde) for madde in oneriler)

    kapali_eslesme = _KAPALI_ROTA_DESENI.search(tum_metin)
    if kapali_eslesme:
        return False, f"kapalı bir rota/güzergah önerilmiş gibi görünüyor (\"...{kapali_eslesme.group(0)}...\")"

    if not _ACIK_IFADESI_DESENI.search(tum_metin):
        return False, "hiçbir öneride rotanın/güzergahın AÇIK olduğuna dair bir ifade geçmiyor"

    return True, "geçerli"





def analiz_et(rapor_metni: str) -> Dict[str, Any]:
   
    yanit = requests.post(
        f"{API_TABAN_URL}/api/analyze-crisis",
        json={"rapor_metni": rapor_metni},
        timeout=ISTEK_ZAMAN_ASIMI_SANIYE,
    )
    yanit.raise_for_status()
    return yanit.json()


def senaryoyu_sifirla() -> None:
    
    try:
        yanit = requests.post(f"{API_TABAN_URL}/api/reset-scenario", timeout=30)
        yanit.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Senaryo sıfırlanamadı (bir sonraki tur ÖNCEKİ kalıntılarla karışabilir): %s", exc)


def kaydet(senaryo_turu: str, rapor_metni: str, sonuc: Dict[str, Any]) -> None:
    
    kayit = {
        "zaman": datetime.now(timezone.utc).isoformat(),
        "senaryo_turu": senaryo_turu,
        "rapor_metni": rapor_metni,
        "durum_ozeti": sonuc.get("durum_ozeti", ""),
        "taktiksel_oneriler": sonuc.get("taktiksel_oneriler", []),
    }
    with open(VERI_SETI_DOSYASI, "a", encoding="utf-8") as f:
        f.write(json.dumps(kayit, ensure_ascii=False) + "\n")




def main() -> None:
    print("=" * 78)
    print(f"KARAVUL — FAZ 7: GÖLGE MODU (War Gaming v2) — hedef: {MAX_SUCCESSFUL_SCENARIOS} geçerli senaryo")
    print(f"API: {API_TABAN_URL}  ·  Çıktı: {VERI_SETI_DOSYASI}")
    print("=" * 78)

    try:
        db = Neo4jConnection()
        db.connect()
        uretici = ProceduralInfrastructureCrisis(db)
    except (Neo4jConnectionError, RuntimeError) as exc:
        print(f"✗ Hedef havuzları yüklenemedi: {exc}")
        sys.exit(1)

    basarili = 0
    deneme = 0

    
    while basarili < MAX_SUCCESSFUL_SCENARIOS and deneme < MAX_TOTAL_DENEME:
        deneme += 1
        senaryo_turu, rapor_metni = uretici.uret()
        print(f"\n[{deneme}/{MAX_TOTAL_DENEME}] ({basarili}/{MAX_SUCCESSFUL_SCENARIOS} kaydedildi) "
              f"Senaryo: {senaryo_turu}")
        print(f"  → {rapor_metni[:110]}{'...' if len(rapor_metni) > 110 else ''}")

        try:
            sonuc = analiz_et(rapor_metni)
        except requests.RequestException as exc:
            print(f"  🛑 API isteği başarısız oldu: {exc}")
            senaryoyu_sifirla()
            time.sleep(DONGU_BEKLEME_SANIYE)
            continue

        gecerli, sebep = gecerli_mi(sonuc)
        if not gecerli:
            print(f"  🛑 Geçersiz Taktik - Reddedildi ({sebep})")
        else:
            kaydet(senaryo_turu, rapor_metni, sonuc)
            basarili += 1
            print(f"  ✅ Kaydedildi ({basarili}/{MAX_SUCCESSFUL_SCENARIOS})")

       
        senaryoyu_sifirla()
        time.sleep(DONGU_BEKLEME_SANIYE)

    print("\n" + "=" * 78)
    if basarili >= MAX_SUCCESSFUL_SCENARIOS:
        print(f"✅ HEDEFE ULAŞILDI: {basarili} geçerli senaryo '{VERI_SETI_DOSYASI}' dosyasına kaydedildi "
              f"({deneme} toplam denemede).")
    else:
        print(f"⚠️ DENEME SINIRINA ({MAX_TOTAL_DENEME}) ULAŞILDI: sadece {basarili}/{MAX_SUCCESSFUL_SCENARIOS} "
              "geçerli senaryo toplanabildi — API/Ollama'nın sağlıklı çalıştığını kontrol edin.")
    print("=" * 78)

    db.close()


if __name__ == "__main__":
    main()
