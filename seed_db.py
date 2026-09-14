
from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import List, Tuple

from src.core.database import Neo4jConnection, Neo4jConnectionError
from src.data_ingestion.local_osm_reader import (
    LocalOsmReader,
    pbf_dosyasi_hazir_mi,
    varsayilan_pbf_yolu,
)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)




class Renk:
    BASLIK = "\033[95m"
    MAVI = "\033[94m"
    YESIL = "\033[92m"
    SARI = "\033[93m"
    KIRMIZI = "\033[91m"
    BOLD = "\033[1m"
    BITIS = "\033[0m"


def _baslik(metin: str) -> None:
    cizgi = "=" * 78
    print(f"\n{Renk.BOLD}{Renk.BASLIK}{cizgi}\n{metin}\n{cizgi}{Renk.BITIS}")


def _satir(etiket: str, deger: object, renk: str = Renk.YESIL) -> None:
    print(f"  {Renk.BOLD}{etiket}:{Renk.BITIS} {renk}{deger}{Renk.BITIS}")


def _argumanlari_ayristir(argv: List[str]) -> argparse.Namespace:
    ayristirici = argparse.ArgumentParser(
        description=(
            "Karar Destek Sistemi icin TEK SEFERLIK, offline harita tohumlama "
            "(seeding) betigi — canli kriz akisindan TAMAMEN izole PBF yukleme."
        )
    )
    ayristirici.add_argument(
        "--pbf",
        metavar="DOSYA_YOLU",
        default=None,
        help=(
            "Yerel .osm.pbf dosya yolu (verilmezse TURKEY_OSM_PBF_PATH ortam "
            "degiskeni, o da yoksa data/turkey-latest.osm.pbf aranir)."
        ),
    )
    ayristirici.add_argument(
        "--atla-omurga",
        action="store_true",
        help=(
            "Ulusal omurgayi (motorway/trunk/primary + hastane/polis/itfaiye/"
            "askeri-us/havalimani) YENIDEN TARAMAYI atla — daha once basariyla "
            "yuklendiyse (idempotent MERGE oldugu icin tekrar zararsizdir, ama "
            "buyuk dosyada zaman alir) bu bayrakla atlanabilir."
        ),
    )
    ayristirici.add_argument(
        "--bolge",
        nargs=3,
        type=float,
        metavar=("ENLEM", "BOYLAM", "YARICAP_KM"),
        action="append",
        default=None,
        help=(
            "Bu merkez/yaricap cevresindeki kilcal (residential/tertiary dahil) "
            "yol agini da KALICI olarak yukle — birden fazla kez tekrarlanabilir "
            "(ör. --bolge 38.681 39.226 15 --bolge 38.355 38.309 15)."
        ),
    )
    return ayristirici.parse_args(argv)


def main(argv: List[str] | None = None) -> None:
    args = _argumanlari_ayristir(argv if argv is not None else sys.argv[1:])
    pbf_yolu = args.pbf or varsayilan_pbf_yolu()
    bolgeler: List[Tuple[float, float, float]] = [tuple(b) for b in (args.bolge or [])]  

    _baslik("HARİTA TOHUMLAMA (SEED) — TEK SEFERLİK, OFFLINE, İZOLE KURULUM AKIŞI")
    _satir("PBF dosyası", pbf_yolu, Renk.MAVI)
    _satir("Omurga (ulusal)", "ATLANACAK" if args.atla_omurga else "yüklenecek", Renk.SARI if args.atla_omurga else Renk.YESIL)
    _satir("Kalıcı bölge sayısı", len(bolgeler), Renk.MAVI)

    if not pbf_dosyasi_hazir_mi(pbf_yolu):
        print(
            f"\n{Renk.KIRMIZI}{Renk.BOLD}✗ Yerel OSM dosyası bulunamadı: {pbf_yolu}{Renk.BITIS}\n"
            f"{Renk.KIRMIZI}  Lütfen Türkiye .osm.pbf dosyasını indirip bu yola koyun, ya da "
            f"--pbf / TURKEY_OSM_PBF_PATH ile doğru yolu belirtin.{Renk.BITIS}"
        )
        sys.exit(1)

    try:
        db = Neo4jConnection()
        db.connect()
        db.ensure_constraints()
    except Neo4jConnectionError as exc:
        print(f"{Renk.KIRMIZI}{Renk.BOLD}✗ Neo4j'e bağlanılamadı: {exc}{Renk.BITIS}")
        sys.exit(1)

    reader = LocalOsmReader(pbf_yolu, db)
    t0 = time.perf_counter()

    if not args.atla_omurga:
        print(
            f"\n{Renk.MAVI}{Renk.BOLD}[1/{1 + len(bolgeler)}] Ulusal omurga yükleniyor "
            f"(ana yollar + hastane/polis/itfaiye/askeri-üs/havalimanı, TÜM Türkiye)..."
            f"{Renk.BITIS}"
        )
        try:
            omurga_sonuc = reader.omurga_yukle()
        except RuntimeError as exc:
            print(f"\n{Renk.KIRMIZI}{Renk.BOLD}✗ Omurga yüklenemedi: {exc}{Renk.BITIS}")
            sys.exit(1)
        for kategori, adet in sorted(omurga_sonuc.items(), key=lambda kv: -kv[1]):
            _satir(f"    - {kategori}", adet)
    else:
        print(f"\n{Renk.SARI}[1/{1 + len(bolgeler)}] Omurga yükleme ATLANDI (--atla-omurga).{Renk.BITIS}")

    for idx, (enlem, boylam, yaricap_km) in enumerate(bolgeler, start=2):
        print(
            f"\n{Renk.MAVI}{Renk.BOLD}[{idx}/{1 + len(bolgeler)}] Bölge yükleniyor "
            f"(merkez=({enlem:.4f}, {boylam:.4f}), yarıçap={yaricap_km:.0f} km, KALICI)..."
            f"{Renk.BITIS}"
        )
        try:
            bolge_sonuc = reader.kriz_bolgesi_yukle(enlem, boylam, yaricap_km=yaricap_km, kalici=True)
        except RuntimeError as exc:
            print(f"\n{Renk.KIRMIZI}{Renk.BOLD}✗ Bölge yüklenemedi: {exc}{Renk.BITIS}")
            continue
        for kategori, adet in sorted(bolge_sonuc.items(), key=lambda kv: -kv[1] if isinstance(kv[1], int) else 0):
            if kategori == "operasyon_id":
                continue
            _satir(f"    - {kategori}", adet)

    sure = time.perf_counter() - t0

    _baslik("SONUÇ RAPORU")
    gercek_toplam_dugum = db.execute_query("MATCH (n) RETURN count(n) AS adet")[0]["adet"]
    etiket_dagilimi = db.execute_query(
        "MATCH (n) UNWIND labels(n) AS etiket RETURN etiket, count(*) AS adet ORDER BY adet DESC"
    )
    print(f"\n  {Renk.BOLD}Neo4j'deki gerçek etiket dağılımı:{Renk.BITIS}")
    for row in etiket_dagilimi:
        print(f"    - {row['etiket']:<22} {Renk.YESIL}{row['adet']}{Renk.BITIS}")

    _satir("\n  Neo4j'deki GERÇEK toplam düğüm sayısı", gercek_toplam_dugum, Renk.SARI)
    _satir("  Toplam süre", f"{sure:.2f} sn", Renk.MAVI)

    if gercek_toplam_dugum == 0:
        print(
            f"\n{Renk.KIRMIZI}{Renk.BOLD}✗ BAŞARISIZ: hiçbir düğüm Neo4j'e yazılmadı. Yukarıdaki "
            f"loglara bakın (dosya/filtre uyuşmazlığı olabilir).{Renk.BITIS}\n"
        )
        sys.exit(1)

    print(
        f"\n{Renk.YESIL}{Renk.BOLD}✅ Harita tohumlama tamamlandı ({gercek_toplam_dugum} düğüm doğrulandı). "
        f"Bu veri KALICIDIR — bir kriz senaryosu sıfırlansa (`db.reset_crisis_scenario`) BİLE SİLİNMEZ."
        f"{Renk.BITIS}"
    )
    print(
        f"{Renk.YESIL}   Karar Destek Sistemi'ni başlatmak için: "
        f"{Renk.BOLD}streamlit run src/ui/app.py{Renk.BITIS}\n"
        f"{Renk.YESIL}   (Canlı kriz akışı artık bu betikle yüklenen veriyi SADECE OKUR; "
        f"hiçbir dosya taraması/yeni harita düğümü yazımı YAPMAZ.){Renk.BITIS}\n"
    )
    db.close()


if __name__ == "__main__":
    main()
