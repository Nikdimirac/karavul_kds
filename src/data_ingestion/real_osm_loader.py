

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import requests

from src.core.database import Neo4jConnection
from src.core.models import (
    BaseNode,
    Infrastructure,
    InfrastructureType,
    OperationalStatus,
)
from src.data_ingestion.osm_loader import (
    OVERPASS_ENDPOINTS,
    OVERPASS_REQUEST_HEADERS,
    OSMBaselineLoader,
    _geometri_uzunlugu_km,
    _tahmini_sayisal_deger,
)
from src.data_ingestion.tucbs_etl_loader import TucbsETLLoader

logger = logging.getLogger(__name__)


class OverpassQueryError(RuntimeError):



OSM_TEKNIK_KIMLIK_IMZASI = "(OSM "



_OVERPASS_ZAMAN_ASIMI_SANIYE = 120

_REQUEST_TIMEOUT_SANIYE = _OVERPASS_ZAMAN_ASIMI_SANIYE + 60


_MAX_DONGU_DENEMESI = 3

_DONGU_ARASI_BEKLEME_SANIYE = 5.0

_RATE_LIMIT_BEKLEME_SANIYE = 8.0

_SEHIRLER_ARASI_BEKLEME_SANIYE = 3.0


_SOKAK_HIGHWAY_TIPLERI: Tuple[str, ...] = (
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential", "living_street",
)


ANA_DAMAR_HIGHWAY_TIPLERI: Tuple[str, ...] = ("motorway", "trunk", "primary", "secondary")

_VARSAYILAN_SEHIRLER: Tuple[str, ...] = ("Elazığ",)


_SIRKUMFLEKS_ESLEMESI: Dict[str, str] = {"a": "[aâ]", "i": "[iî]", "u": "[uû]"}


def _sehir_adi_regex(sehir_adi: str) -> str:
    
    parcalar = [_SIRKUMFLEKS_ESLEMESI.get(harf.lower(), re.escape(harf)) for harf in sehir_adi]
    return "^" + "".join(parcalar) + "$"




def _run_overpass_query(
    query: str,
    endpoints: Sequence[str] = OVERPASS_ENDPOINTS,
    request_timeout: int = _REQUEST_TIMEOUT_SANIYE,
    max_dongu: int = _MAX_DONGU_DENEMESI,
) -> Dict[str, Any]:
   
    son_hata: Optional[Exception] = None
    for dongu in range(1, max_dongu + 1):
        for endpoint in endpoints:
            try:
                yanit = requests.post(
                    endpoint,
                    data={"data": query},
                    timeout=request_timeout,
                    headers=OVERPASS_REQUEST_HEADERS,
                )
                if yanit.status_code == 429:
                    logger.warning(
                        "Overpass 429 Too Many Requests (%s) - %.0f sn bekleyip devam edilecek...",
                        endpoint, _RATE_LIMIT_BEKLEME_SANIYE,
                    )
                    son_hata = OverpassQueryError(f"429 Too Many Requests ({endpoint})")
                    time.sleep(_RATE_LIMIT_BEKLEME_SANIYE)
                    continue
                yanit.raise_for_status()
                veri = yanit.json()
            except (requests.RequestException, ValueError) as exc:
                logger.warning("Overpass sorgusu basarisiz oldu (%s): %s", endpoint, exc)
                son_hata = exc
                continue

            remark = veri.get("remark")
            if remark:
                logger.warning(
                    "Overpass sunucusu bir hata/timeout remark'i dondurdu (%s): %s", endpoint, remark
                )
                son_hata = OverpassQueryError(remark)
                continue

            eleman_sayisi = len(veri.get("elements", []))
            logger.info("Overpass sorgusu basarili (%s): %d eleman alindi.", endpoint, eleman_sayisi)
            return veri

        if dongu < max_dongu:
            bekleme = _DONGU_ARASI_BEKLEME_SANIYE * dongu
            logger.warning(
                "Tum Overpass aynalari basarisiz oldu (deneme %d/%d) - %.0f sn sonra tekrar denenecek...",
                dongu, max_dongu, bekleme,
            )
            time.sleep(bekleme)

    raise RuntimeError(
        f"Hicbir Overpass API ucuna ulasilamadi/gecerli veri alinamadi "
        f"({max_dongu} tam ayna-turu denendi; aynalar: {list(endpoints)}): {son_hata}"
    ) from son_hata


class RealOsmLoader:
    
    def __init__(
        self,
        db: Optional[Neo4jConnection] = None,
        sehirler: Optional[Sequence[str]] = None,
        overpass_endpoints: Sequence[str] = OVERPASS_ENDPOINTS,
        request_timeout: int = _REQUEST_TIMEOUT_SANIYE,
    ) -> None:
        self.db = db or Neo4jConnection()
        self.sehirler: List[str] = list(sehirler) if sehirler else list(_VARSAYILAN_SEHIRLER)
        self._endpoints = list(overpass_endpoints)
        self._request_timeout = request_timeout

   
    @staticmethod
    def _build_point_query(alan_adi: str) -> str:
       
            raise RuntimeError(
                f"[{alan_adi}] Overpass API'den HICBIR eleman donmedi (0 nokta + 0 yol). Olasi "
                "nedenler: bu isimde (harf-duyarsiz, sirkumfleks-toleransli aramaya ragmen) bir "
                "idari alan OSM'de bulunamadi -- il/ilce adini kontrol edin, tum Overpass "
                "aynalari basarisiz oldu, veya sunucu(lar) sorguyu zaman asimina ugratti. "
                "Yukaridaki loglara bakin."
            )

        return {"noktalar": nokta_elemanlari, "yollar": yol_elemanlari}

 

    def build_nodes(
        self,
        sayac_cikti: Optional[Dict[str, int]] = None,
        basarisiz_bolgeler_cikti: Optional[List[Tuple[str, str]]] = None,
    ) -> Iterator[BaseNode]:
        
        toplam_facility = toplam_unit = toplam_sokak = toplam_kopru = 0
        toplam_enerji = toplam_iletisim = toplam_kaynak = 0

        for idx, alan_adi in enumerate(self.sehirler):
            if idx > 0:
                logger.info(
                    "Sehirler arasi nezaket beklemesi (%s -> siradaki): %.0f sn...",
                    alan_adi, _SEHIRLER_ARASI_BEKLEME_SANIYE,
                )
                time.sleep(_SEHIRLER_ARASI_BEKLEME_SANIYE)

            logger.info("=== Bolge isleniyor (idari alan): %s ===", alan_adi)
            try:
                ham = self._fetch_raw_elements_for(alan_adi)
            except RuntimeError as exc:
                logger.error("[%s] BOLGE ATLANDI (hata cekildi, diger iller ETKILENMEZ): %s", alan_adi, exc)
                if basarisiz_bolgeler_cikti is not None:
                    basarisiz_bolgeler_cikti.append((alan_adi, str(exc)))
                continue
            facilities, units, energiler, iletisimler, kaynaklar = OSMBaselineLoader._convert_point_elements(
                ham["noktalar"]
            )
            for facility in facilities:
                facility.bolge = alan_adi
                yield facility
            for unit in units:
                unit.bolge = alan_adi
                yield unit
           
            for enerji in energiler:
                enerji.bolge = alan_adi
                yield enerji
            for iletisim in iletisimler:
                iletisim.bolge = alan_adi
                yield iletisim
            for kaynak in kaynaklar:
                kaynak.bolge = alan_adi
                yield kaynak
            toplam_facility += len(facilities)
            toplam_unit += len(units)
            toplam_enerji += len(energiler)
            toplam_iletisim += len(iletisimler)
            toplam_kaynak += len(kaynaklar)

            for way in ham["yollar"]:
                geometry = way.get("geometry")
                if not geometry or len(geometry) < 2:
                    continue
                tags = way.get("tags", {})
                way_id = way.get("id", "?")
                bridge_etiketi = str(tags.get("bridge", "")).strip().lower()
                gercek_ad = tags.get("name")  

                if bridge_etiketi not in ("", "no", "false", "0"):
                    toplam_kopru += 1
                    uzunluk_km = round(_geometri_uzunlugu_km(geometry), 3)
                    temel_isim = gercek_ad or "Kopru"
                    yield Infrastructure(
                        isim=f"{temel_isim} {OSM_TEKNIK_KIMLIK_IMZASI}way/{way_id})",
                       
                        aciklama=gercek_ad,
                        bolge=alan_adi,
                        enlem=geometry[0]["lat"],
                        boylam=geometry[0]["lon"],
                        bitis_enlem=geometry[-1]["lat"],
                        bitis_boylam=geometry[-1]["lon"],
                        durum=OperationalStatus.AKTIF,
                        infrastructure_type=InfrastructureType.KOPRU,
                        uzunluk_km=uzunluk_km,
                        acik_mi=True,
                        tonaj_kapasitesi=_tahmini_sayisal_deger(tags, ("maxweight",), 40.0),
                        serit_sayisi=int(_tahmini_sayisal_deger(tags, ("lanes",), 2)),
                        highway_tipi=tags.get("highway"),
                    )
                    continue

              
                isim_on_eki = gercek_ad or "Sokak Dugumu"
                for idx2, nokta in enumerate(geometry):
                    lat, lon = nokta.get("lat"), nokta.get("lon")
                    if lat is None or lon is None:
                        continue
                    toplam_sokak += 1
                    yield Infrastructure(
                        isim=f"{isim_on_eki} {OSM_TEKNIK_KIMLIK_IMZASI}way/{way_id} #{idx2})",
                        aciklama=gercek_ad,
                        bolge=alan_adi,
                        enlem=lat,
                        boylam=lon,
                        durum=OperationalStatus.AKTIF,
                        infrastructure_type=InfrastructureType.SOKAK,
                        uzunluk_km=0.0,
                        acik_mi=True,
                        highway_tipi=tags.get("highway"),
                    )

        if sayac_cikti is not None:
            sayac_cikti["facility"] = toplam_facility
            sayac_cikti["unit"] = toplam_unit
            sayac_cikti["sokak"] = toplam_sokak
            sayac_cikti["kopru"] = toplam_kopru
            sayac_cikti["energy_infrastructure"] = toplam_enerji
            sayac_cikti["communication_network"] = toplam_iletisim
            sayac_cikti["resource_hub"] = toplam_kaynak

    

    def load(self, basarisiz_bolgeler_cikti: Optional[List[Tuple[str, str]]] = None) -> Dict[str, int]:
       
        loader = TucbsETLLoader(self.db)
        return loader.bulk_insert_nodes(self.build_nodes(basarisiz_bolgeler_cikti=basarisiz_bolgeler_cikti))


def main() -> None:  
   
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    
    sehirler = sys.argv[1:] or None

    db = Neo4jConnection()
    db.connect()
    db.ensure_constraints()

    loader = RealOsmLoader(db, sehirler=sehirler)
    print(f"Overpass API'den {loader.sehirler} GERÇEK idari alan(lar)ı çekiliyor (bu birkaç dakika sürebilir)...")
    basarisiz_bolgeler: List[Tuple[str, str]] = []
    sayaclar = loader.load(basarisiz_bolgeler_cikti=basarisiz_bolgeler)

    toplam = sum(sayaclar.values())
    print("\nGerçek şehir topolojisi Bilgi Grafı'na yüklendi:")
    for kategori, adet in sayaclar.items():
        print(f"  - {kategori}: {adet}")

    if basarisiz_bolgeler:
        print(
            f"\n⚠️  {len(basarisiz_bolgeler)}/{len(loader.sehirler)} bölge BAŞARISIZ oldu "
            "(diğerleri etkilenmedi):"
        )
        for il_adi, hata in basarisiz_bolgeler:
            print(f"  - {il_adi}: {hata}")

    if toplam == 0:

        raise RuntimeError("Yukleme 'basarili' tamamlandi ama 0 dugum yazildi — bu beklenmeyen bir durumdur.")


if __name__ == "__main__":
    main()
