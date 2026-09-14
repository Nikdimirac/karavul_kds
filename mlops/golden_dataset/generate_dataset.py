

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.core.database import Neo4jConnection, Neo4jConnectionError  

from mlops.golden_dataset.pilot_test import (  
    API_TABAN_URL,
    _altin_yanit_insa_et,
    _analiz_et,
    _sifirla,
)
from mlops.golden_dataset.scenario_generator import (  
    RISK_MATRIX,
    OlayTuru,
    _yasakli_eylem_kalibi_var_mi,
    gecerli_olay_turleri,
    senaryo_gecerli_mi,
)

VERI_SETI_DOSYASI = Path(__file__).resolve().parent / "golden_dataset.jsonl"
KAYNAK_ETIKETI = "golden_v1"
HEDEF_VARSAYILAN = 1000
MAKS_DENEME_CARPANI = 3
"""Hedefe ulaşmak için denenecek ÜST sınır = hedef * bu çarpan — bazı
denemeler API/Ollama hatasıyla ya da `durum_ozeti` boş dönerek başarısız
olabilir; sonsuz döngüye girmeden AÇIKÇA durur."""




_ilce_onbellegi: Dict[str, List[str]] = {}


def _rastgele_ilce_adi(db: Neo4jConnection, il: str) -> Optional[str]:
    if il not in _ilce_onbellegi:
        sonuc = db.execute_query(
            "MATCH (n:Settlement) WHERE n.yerlesim_tipi = 'Ilce Merkezi' AND n.il = $il "
            "RETURN n.isim AS isim LIMIT 200",
            {"il": il}, write=False,
        )
        isimler = []
        for row in sonuc:
            isim = row["isim"]
            parantez = isim.rfind(" (")
            isimler.append(isim[:parantez] if parantez != -1 else isim)
        _ilce_onbellegi[il] = isimler
    havuz = _ilce_onbellegi[il]
    return random.choice(havuz) if havuz else None


def _konum_ifadesi(db: Neo4jConnection, il: str) -> str:
    ilce = _rastgele_ilce_adi(db, il)
    if ilce and ilce != il:
        return f"{il} ili {ilce} ilçesinde"
    return f"{il} il merkezinde"




def _rapor_uret(db: Neo4jConnection, il: str, olay_turu: OlayTuru) -> Tuple[str, Optional[float]]:
    """`(rapor_metni, deprem_mw_veya_None)` döner."""
    konum = _konum_ifadesi(db, il)
    profil = RISK_MATRIX[il]

    if olay_turu == OlayTuru.DEPREM:
        ust_sinir = profil.gercekci_max_deprem_mw
        mw = round(random.uniform(max(4.0, ust_sinir - 2.5), ust_sinir), 1)
        metin = (
            f"{konum} {mw} büyüklüğünde bir deprem meydana geldi. Çok sayıda bina hasar gördü, "
            f"enkaz altında vatandaş olduğu bildiriliyor."
        )
        return metin, mw

    if olay_turu == OlayTuru.SEL:
        mm = random.randint(70, 220)
        saat = random.randint(4, 24)
        metin = (
            f"{konum} son {saat} saatte {mm} mm'yi bulan aşırı yağış sonucu ani bir sel felaketi "
            f"yaşandı. Bölgedeki dereler taştı, çok sayıda ev su altında kaldı."
        )
        return metin, None

    if olay_turu == OlayTuru.ORMAN_YANGINI:
        hektar = random.randint(50, 900)
        metin = (
            f"{konum} kırsal kesiminde çıkan orman yangını rüzgarın etkisiyle hızla büyüyerek "
            f"yaklaşık {hektar} hektarlık alanı etkisi altına aldı."
        )
        return metin, None

    if olay_turu == OlayTuru.HEYELAN:
        metin = (
            f"{konum} şiddetli yağış sonrası meydana gelen heyelan nedeniyle bölgedeki ana "
            f"güzergah kapandı, bazı evler tahliye edildi."
        )
        return metin, None

    if olay_turu == OlayTuru.CIG:
        metin = (
            f"{konum} dağlık kesimde meydana gelen çığ düşmesi sonucu ana yol ulaşıma kapandı, "
            f"bölgede mahsur kalan araçlar olduğu bildiriliyor."
        )
        return metin, None

    if olay_turu == OlayTuru.LIMAN_YANGINI:
        varyant = random.choice(["konteyner sahasında", "depolama alanında", "yakıt istasyonunda"])
        metin = (
            f"{il} Limanı'nda {varyant} başlayan endüstriyel bir yangın kısa sürede büyüdü. "
            f"Liman tesisleri hasar aldı, gemi trafiği durduruldu."
        )
        return metin, None

    if olay_turu == OlayTuru.GEMI_KAZASI:
        metin = (
            f"{il} açıklarında iki gemi arasında meydana gelen çatışma sonucu bir gemide yangın "
            f"çıktı, mürettebat tahliyesi gerekiyor."
        )
        return metin, None

    if olay_turu == OlayTuru.KIMYASAL_PATLAMA:
        metin = (
            f"{konum} bir kimya fabrikasında büyük bir patlama ve ardından yangın meydana geldi. "
            f"Zehirli duman çevreye yayılıyor, yakın mahallelerde tahliye ihtiyacı var."
        )
        return metin, None

    if olay_turu == OlayTuru.ENDUSTRIYEL_YANGIN:
        metin = (
            f"{konum} bir sanayi tesisinde büyük bir yangın çıktı, alevlerin yakın binalara "
            f"sıçraması riski bulunuyor."
        )
        return metin, None

    if olay_turu == OlayTuru.ZINCIRLEME_KAZA:
        arac = random.randint(8, 45)
        sebep = random.choice(["yoğun sis", "aşırı yağış", "buzlanma", "ani sürücü hatası"])
        metin = (
            f"{konum} bulunan ana karayolunda {sebep} nedeniyle {arac} aracın karıştığı "
            f"zincirleme trafik kazası meydana geldi, yol trafiğe kapatıldı."
        )
        return metin, None

    if olay_turu == OlayTuru.HASTANE_KRIZI:
        metin = (
            f"{konum} bulunan devlet hastanesinde ana elektrik sisteminde meydana gelen teknik "
            f"arıza sonucu yoğun bakım üniteleri risk altında, acil müdahale gerekiyor."
        )
        return metin, None

    if olay_turu == OlayTuru.SINIR_GUVENLIK_OLAYI:
        metin = (
            f"{il} ilinin {profil.sinir_komsusu} sınır hattında güvenlik güçlerince şüpheli bir "
            f"hareketlilik tespit edildi, bölgede alarm durumu ilan edildi."
        )
        return metin, None

    if olay_turu == OlayTuru.SIBER_SALDIRI:
        metin = (
            f"{il} Valiliği'ne bağlı bilgi sistemlerine yönelik bir siber saldırı tespit edildi, "
            f"kritik altyapı sistemlerinde kesinti riski bulunuyor."
        )
        return metin, None

  
    metin = f"{konum} silahlı bir saldırı bildirildi, bölgede çok sayıda güvenlik ekibi sevk edildi."
    return metin, None




def _mevcut_kayit_sayisi() -> int:
    if not VERI_SETI_DOSYASI.exists():
        return 0
    with open(VERI_SETI_DOSYASI, "r", encoding="utf-8") as f:
        return sum(1 for satir in f if satir.strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hedef", type=int, default=HEDEF_VARSAYILAN)
    ap.add_argument("--devam-et", action="store_true", help="Var olan golden_dataset.jsonl'a EKLER (baştan başlamaz)")
    ap.add_argument(
        "--bekleme-sn", type=float, default=0.0,
        help="Her deneme arasına konacak sabit bekleme (saniye) — RAM sıçramasını önlemek için 'hafif mod'.",
    )
    ap.add_argument(
        "--uzun-bekleme-her", type=int, default=0,
        help="Bu kadar BAŞARILI kayıtta bir --uzun-bekleme-sn kadar ekstra dinlenme molası ver (0 = kapalı).",
    )
    ap.add_argument(
        "--uzun-bekleme-sn", type=float, default=20.0,
        help="--uzun-bekleme-her ile tetiklenen dinlenme molasının süresi (saniye).",
    )
    args = ap.parse_args()

    if not args.devam_et and VERI_SETI_DOSYASI.exists():
        print(f"🛑 {VERI_SETI_DOSYASI} zaten var ve --devam-et verilmedi. "
              "Üzerine EKLEMEK için --devam-et kullanın, baştan başlamak için dosyayı silin.")
        return 1

    try:
        db = Neo4jConnection()
        db.connect()
    except Neo4jConnectionError as exc:
        print(f"🛑 Neo4j'e bağlanılamadı: {exc}")
        return 1

    try:
        requests.get(f"{API_TABAN_URL}/health", timeout=10).raise_for_status()
    except requests.RequestException as exc:
        print(f"🛑 api.py ({API_TABAN_URL}) ayakta değil: {exc}")
        return 1

    iller = list(RISK_MATRIX.keys())
    mevcut = _mevcut_kayit_sayisi()
    hedef = args.hedef
    print("=" * 90)
    print(f"KARAVUL — ALTIN VERİ SETİ — 1000'lik ÜRETİM DÖNGÜSÜ")
    print(f"Hedef: {hedef} | Mevcut: {mevcut} | Çıktı: {VERI_SETI_DOSYASI}")
    if args.bekleme_sn > 0 or args.uzun_bekleme_her > 0:
        print(
            f"🐢 HAFİF MOD: her deneme arası {args.bekleme_sn:.1f} sn bekleme"
            + (
                f" + her {args.uzun_bekleme_her} başarılı kayıtta {args.uzun_bekleme_sn:.0f} sn ekstra mola"
                if args.uzun_bekleme_her > 0 else ""
            )
        )
    print("=" * 90)

    basarili = mevcut
    deneme = 0
    maks_deneme = hedef * MAKS_DENEME_CARPANI
    reddedilen_gecersiz = 0
    reddedilen_yasakli_eylem = 0
    reddedilen_bos_baglam = 0
    reddedilen_api_hata = 0
    t0 = time.monotonic()

    f = open(VERI_SETI_DOSYASI, "a", encoding="utf-8")
    try:
        while basarili < hedef and deneme < maks_deneme:
            if deneme > 0 and args.bekleme_sn > 0:
                time.sleep(args.bekleme_sn)
            deneme += 1
            il = random.choice(iller)
            gecerli_turler = gecerli_olay_turleri(il)
            olay_turu = random.choice(gecerli_turler)

            rapor_metni, deprem_mw = _rapor_uret(db, il, olay_turu)

            gecerli, sebep = senaryo_gecerli_mi(il, olay_turu, deprem_mw)
            if not gecerli:
                reddedilen_gecersiz += 1
                logger_yaz(f"  [{deneme}] REDDEDİLDİ (RISK_MATRIX): {il}/{olay_turu.value} — {sebep}")
                continue

            _sifirla()
            try:
                sonuc = _analiz_et(rapor_metni)
            except requests.RequestException as exc:
                reddedilen_api_hata += 1
                logger_yaz(f"  [{deneme}] API HATASI ({il}/{olay_turu.value}): {exc}")
                _sifirla()
                continue

            durum_ozeti = sonuc.get("durum_ozeti") or ""
            if not durum_ozeti.strip():
                reddedilen_bos_baglam += 1
                logger_yaz(f"  [{deneme}] BOŞ BAĞLAM ({il}/{olay_turu.value}) — atlandı.")
                _sifirla()
                continue

            altin_yanit = _altin_yanit_insa_et(il, rapor_metni, durum_ozeti)
            if _yasakli_eylem_kalibi_var_mi(altin_yanit):
                reddedilen_yasakli_eylem += 1
                logger_yaz(f"  [{deneme}] YASAKLI EYLEM KALIBI ({il}/{olay_turu.value}) — atlandı.")
                _sifirla()
                continue

            kayit = {
                "kaynak": KAYNAK_ETIKETI,
                "il": il,
                "olay_turu": olay_turu.value,
                "rapor_metni": rapor_metni,
                "durum_ozeti": durum_ozeti,
                "taktiksel_oneriler": [altin_yanit],
            }
            f.write(json.dumps(kayit, ensure_ascii=False) + "\n")
            f.flush()
            basarili += 1
            if basarili % 10 == 0 or basarili == hedef:
                gecen_dk = (time.monotonic() - t0) / 60
                print(f"✅ {basarili}/{hedef} ({deneme} denemede, {gecen_dk:.1f} dk) — son: {il}/{olay_turu.value}")

            _sifirla()

            if args.uzun_bekleme_her > 0 and basarili % args.uzun_bekleme_her == 0:
                print(f"😴 Dinlenme molası: {args.uzun_bekleme_sn:.0f} sn (RAM toparlansın diye)...")
                time.sleep(args.uzun_bekleme_sn)
    finally:
        f.close()
        db.close()

    print("\n" + "=" * 90)
    if basarili >= hedef:
        print(f"✅ HEDEFE ULAŞILDI: {basarili} altın örnek, {VERI_SETI_DOSYASI} dosyasında.")
    else:
        print(f"⚠️ HEDEFE ULAŞILAMADI: {basarili}/{hedef} — {deneme} deneme sonunda üst sınıra ulaşıldı.")
    print(
        f"Reddedilenler — RISK_MATRIX: {reddedilen_gecersiz}, boş bağlam: {reddedilen_bos_baglam}, "
        f"yasaklı eylem: {reddedilen_yasakli_eylem}, API hatası: {reddedilen_api_hata}"
    )
    print("=" * 90)
    return 0 if basarili >= hedef else 1


def logger_yaz(msg: str) -> None:
    print(msg)


if __name__ == "__main__":
    sys.exit(main())
