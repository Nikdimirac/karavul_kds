
from __future__ import annotations

import logging
import math
import os
import sys
import uuid
from typing import Any, Dict, List, Optional, Tuple

try:
    import osmium
except ImportError as _exc:  
    osmium = None 
    _OSMIUM_IMPORT_HATASI = _exc
else:
    _OSMIUM_IMPORT_HATASI = None

from src.core.database import Neo4jConnection
from src.core.models import (
    BaseNode,
    Facility,
    FacilityType,
    FacilityStatus,
    Infrastructure,
    InfrastructureType,
    MobilityStatus,
    OperationalStatus,
    Unit,
    UnitType,
)
from src.data_ingestion.real_osm_loader import OSM_TEKNIK_KIMLIK_IMZASI
from src.data_ingestion.tucbs_etl_loader import TucbsETLLoader

logger = logging.getLogger(__name__)



VARSAYILAN_PBF_DOSYA_ADI = "turkey-latest.osm.pbf"
PBF_YOLU_ENV_DEGISKENI = "TURKEY_OSM_PBF_PATH"


def varsayilan_pbf_yolu() -> str:

    override = os.environ.get(PBF_YOLU_ENV_DEGISKENI)
    if override:
        return override
    proje_koku = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    return os.path.join(proje_koku, "data", VARSAYILAN_PBF_DOSYA_ADI)


def pbf_dosyasi_hazir_mi(pbf_dosya_yolu: str) -> bool:
    
    return bool(pbf_dosya_yolu) and os.path.isfile(pbf_dosya_yolu)


def _osmium_gerekli() -> None:
   
    if osmium is None:  
        raise RuntimeError(
            "'osmium' paketi kurulu değil (Yerel/Offline OSM okuma için "
            "ZORUNLUDUR). Kurulum: pip install osmium"
        ) from _OSMIUM_IMPORT_HATASI


OMURGA_HIGHWAY_TIPLERI: Tuple[str, ...] = ("motorway", "trunk", "primary")

OMURGA_AMENITY_TIPLERI: Tuple[str, ...] = ("hospital", "police", "fire_station")



OMURGA_LANDUSE_TIPLERI: Tuple[str, ...] = ("military",)
OMURGA_MILITARY_TIPLERI: Tuple[str, ...] = ("base",)

OMURGA_AEROWAY_TIPLERI: Tuple[str, ...] = ("aerodrome",)



KILCAL_HIGHWAY_TIPLERI: Tuple[str, ...] = (
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential", "living_street",
)

KILCAL_YALNIZCA_HIGHWAY_TIPLERI: Tuple[str, ...] = tuple(
    tip for tip in KILCAL_HIGHWAY_TIPLERI if tip not in OMURGA_HIGHWAY_TIPLERI
)


_VARSAYILAN_YARICAP_KM = 5.0

_OMURGA_MIN_KOPRU_UZUNLUK_KM = 0.05


_VARSAYILAN_HASTANE_KAPASITESI = 50.0
_VARSAYILAN_ASKERI_US_KAPASITESI = 100.0
_VARSAYILAN_HAVALIMANI_KAPASITESI = 200.0

_VARSAYILAN_PERSONEL_SAYISI = 15


def _bbox_hesapla(
    merkez_enlem: float, merkez_boylam: float, yaricap_km: float
) -> Tuple[float, float, float, float]:
   
    enlem_delta = yaricap_km / 111.32
    boylam_delta = yaricap_km / (111.32 * math.cos(math.radians(merkez_enlem)) or 1e-9)
    return (
        merkez_enlem - enlem_delta,
        merkez_boylam - boylam_delta,
        merkez_enlem + enlem_delta,
        merkez_boylam + boylam_delta,
    )


def _nokta_bbox_icinde_mi(enlem: float, boylam: float, bbox: Tuple[float, float, float, float]) -> bool:
    enlem_min, boylam_min, enlem_max, boylam_max = bbox
    return enlem_min <= enlem <= enlem_max and boylam_min <= boylam <= boylam_max



class _OsmStreamHandler:

    def __init__(
        self,
        highway_tipleri: Tuple[str, ...],
        amenity_tipleri: Tuple[str, ...],
        bbox: Optional[Tuple[float, float, float, float]],
        gecici_mi: bool,
        operasyon_id: Optional[str],
        siki_filtre: bool = False,
        askeri_havalimani_dahil: bool = False,
    ) -> None:
        self.highway_tipleri = set(highway_tipleri)
        self.amenity_tipleri = set(amenity_tipleri)
        self.bbox = bbox
        self.gecici_mi = gecici_mi
        self.operasyon_id = operasyon_id
      
        self.siki_filtre = siki_filtre
        
        self.askeri_havalimani_dahil = askeri_havalimani_dahil
        self.dugumler: List[BaseNode] = []
        self.way_sayisi = 0
        self.node_sayisi = 0
        self.elenen_kucuk_kopru_sayisi = 0
        self.elenen_isimsiz_tesis_sayisi = 0


    def _ozel_alan_tip_anahtari(self, tags: Any) -> Optional[str]:
       
        amenity = tags.get("amenity")
        if amenity in self.amenity_tipleri:
            return amenity
        if not self.askeri_havalimani_dahil:
            return None
        if tags.get("landuse") in OMURGA_LANDUSE_TIPLERI or tags.get("military") in OMURGA_MILITARY_TIPLERI:
            return "military"
        if tags.get("aeroway") in OMURGA_AEROWAY_TIPLERI:
            return "aerodrome"
        return None

    def _node_isle(self, n: Any) -> None:
        self.node_sayisi += 1
        tip_anahtari = self._ozel_alan_tip_anahtari(n.tags)
        if tip_anahtari is None:
            return
        
        if self.siki_filtre and not str(n.tags.get("name") or "").strip():
            self.elenen_isimsiz_tesis_sayisi += 1
            return
        if not n.location.valid():
            return
        enlem, boylam = n.location.lat, n.location.lon
        if self.bbox is not None and not _nokta_bbox_icinde_mi(enlem, boylam, self.bbox):
            return
        varlik = _amenity_dugumu_uret(tip_anahtari, n.tags, n.id, "node", enlem, boylam)
        if varlik is not None:
            self.dugumler.append(varlik)

    def _way_isle(self, w: Any) -> None:
        self.way_sayisi += 1
        tags = w.tags
        highway = tags.get("highway")
      
        tip_anahtari = self._ozel_alan_tip_anahtari(tags)

        if tip_anahtari is not None and self.siki_filtre and not str(tags.get("name") or "").strip():
            self.elenen_isimsiz_tesis_sayisi += 1
        if tip_anahtari is not None and (
            not self.siki_filtre or str(tags.get("name") or "").strip()
        ):
            
            ilk_nokta = _way_ilk_gecerli_nokta(w)
            if ilk_nokta is not None:
                enlem, boylam = ilk_nokta
                if self.bbox is None or _nokta_bbox_icinde_mi(enlem, boylam, self.bbox):
                    varlik = _amenity_dugumu_uret(tip_anahtari, tags, w.id, "way", enlem, boylam)
                    if varlik is not None:
                        self.dugumler.append(varlik)

        if highway not in self.highway_tipleri:
            return

        gercek_ad = tags.get("name")
        bridge_etiketi = str(tags.get("bridge", "") or "").strip().lower()
        geometri = _way_gecerli_koordinatlari(w)
        if len(geometri) < 2:
            return

        if self.bbox is not None and not any(_nokta_bbox_icinde_mi(e, b, self.bbox) for e, b in geometri):
            return

        if bridge_etiketi not in ("", "no", "false", "0"):
            uzunluk_km = _geometri_uzunlugu_km(geometri)
            
            if (
                self.siki_filtre
                and not str(gercek_ad or "").strip()
                and uzunluk_km < _OMURGA_MIN_KOPRU_UZUNLUK_KM
            ):
                self.elenen_kucuk_kopru_sayisi += 1
                return
            temel_isim = gercek_ad or "Kopru"
            self.dugumler.append(
                Infrastructure(
                    isim=f"{temel_isim} {OSM_TEKNIK_KIMLIK_IMZASI}way/{w.id})",
                    aciklama=gercek_ad,
                    enlem=geometri[0][0],
                    boylam=geometri[0][1],
                    bitis_enlem=geometri[-1][0],
                    bitis_boylam=geometri[-1][1],
                    durum=OperationalStatus.AKTIF,
                    infrastructure_type=InfrastructureType.KOPRU,
                    uzunluk_km=uzunluk_km,
                    acik_mi=True,
                    highway_tipi=highway,
                    gecici_mi=self.gecici_mi,
                    operasyon_id=self.operasyon_id,
                )
            )
            return

        isim_on_eki = gercek_ad or "Sokak Dugumu"
        for idx, (enlem, boylam) in enumerate(geometri):
            self.dugumler.append(
                Infrastructure(
                    isim=f"{isim_on_eki} {OSM_TEKNIK_KIMLIK_IMZASI}way/{w.id} #{idx})",
                    aciklama=gercek_ad,
                    enlem=enlem,
                    boylam=boylam,
                    durum=OperationalStatus.AKTIF,
                    infrastructure_type=InfrastructureType.SOKAK,
                    uzunluk_km=0.0,
                    acik_mi=True,
                    highway_tipi=highway,
                    gecici_mi=self.gecici_mi,
                    operasyon_id=self.operasyon_id,
                )
            )


def _way_gecerli_koordinatlari(w: Any) -> List[Tuple[float, float]]:
  
    sonuc: List[Tuple[float, float]] = []
    for wn in w.nodes:
        try:
            if wn.location.valid():
                sonuc.append((wn.location.lat, wn.location.lon))
        except Exception:  
            continue
    return sonuc


def _way_ilk_gecerli_nokta(w: Any) -> Optional[Tuple[float, float]]:
    koordinatlar = _way_gecerli_koordinatlari(w)
    return koordinatlar[0] if koordinatlar else None


def _geometri_uzunlugu_km(geometri: List[Tuple[float, float]]) -> float:
    
    toplam = 0.0
    for (e1, b1), (e2, b2) in zip(geometri, geometri[1:]):
        toplam += Neo4jConnection._geodesic_km((e1, b1), (e2, b2))
    return round(toplam, 3)


_TIP_ANAHTARI_JENERIK_ISIM: Dict[str, str] = {
    "hospital": "Hastane",
    "fire_station": "Itfaiye Istasyonu",
    "police": "Polis Karakolu",
    "military": "Askeri Us",
    "aerodrome": "Havalimani",
}



def _amenity_dugumu_uret(
    tip_anahtari: str, tags: Any, osm_id: int, osm_tip: str, enlem: float, boylam: float
) -> Optional[BaseNode]:
    
    gercek_ad = tags.get("name")
    temel_isim = gercek_ad or _TIP_ANAHTARI_JENERIK_ISIM.get(tip_anahtari, tip_anahtari.title())
    isim = f"{temel_isim} {OSM_TEKNIK_KIMLIK_IMZASI}{osm_tip}/{osm_id})"
    if tip_anahtari == "hospital":
        kapasite = _tahmini_sayisal_deger(tags, ("capacity:beds", "beds", "capacity"), _VARSAYILAN_HASTANE_KAPASITESI)
        return Facility(
            isim=isim,
            aciklama=gercek_ad,
            enlem=enlem,
            boylam=boylam,
            durum=OperationalStatus.AKTIF,
            facility_type=FacilityType.HASTANE,
            kapasite=kapasite,
            mevcut_durum=FacilityStatus.AKTIF,
        )
    if tip_anahtari == "fire_station":
        personel = int(_tahmini_sayisal_deger(tags, ("staff_count",), _VARSAYILAN_PERSONEL_SAYISI))
        return Unit(
            isim=isim,
            aciklama=gercek_ad,
            enlem=enlem,
            boylam=boylam,
            durum=OperationalStatus.AKTIF,
            unit_type=UnitType.ITFAIYE,
            personel_sayisi=personel,
            hareket_kabiliyeti=MobilityStatus.SINIRLI_HAREKETLI,
        )
    if tip_anahtari == "police":
        personel = int(_tahmini_sayisal_deger(tags, ("staff_count",), _VARSAYILAN_PERSONEL_SAYISI))
        return Unit(
            isim=isim,
            aciklama=gercek_ad,
            enlem=enlem,
            boylam=boylam,
            durum=OperationalStatus.AKTIF,
            unit_type=UnitType.POLIS,
            personel_sayisi=personel,
            hareket_kabiliyeti=MobilityStatus.SINIRLI_HAREKETLI,
        )
    if tip_anahtari == "military":
        return Facility(
            isim=isim,
            aciklama=gercek_ad,
            enlem=enlem,
            boylam=boylam,
            durum=OperationalStatus.AKTIF,
            facility_type=FacilityType.ASKERI_US,
            kapasite=_VARSAYILAN_ASKERI_US_KAPASITESI,
            mevcut_durum=FacilityStatus.AKTIF,
        )
    if tip_anahtari == "aerodrome":
        return Facility(
            isim=isim,
            aciklama=gercek_ad,
            enlem=enlem,
            boylam=boylam,
            durum=OperationalStatus.AKTIF,
            facility_type=FacilityType.HAVALIMANI,
            kapasite=_VARSAYILAN_HAVALIMANI_KAPASITESI,
            mevcut_durum=FacilityStatus.AKTIF,
        )
    return None


def _tahmini_sayisal_deger(tags: Any, anahtarlar: Tuple[str, ...], varsayilan: float) -> float:
    for anahtar in anahtarlar:
        deger = tags.get(anahtar)
        if deger is None:
            continue
        try:
            return float(deger)
        except (TypeError, ValueError):
            continue
    return varsayilan


def _gercek_handler_olustur(ic_handler: _OsmStreamHandler) -> Any:
    _osmium_gerekli()

    class _Handler(osmium.SimpleHandler):  
        def node(self, n: Any) -> None:
            ic_handler._node_isle(n)

        def way(self, w: Any) -> None:
            ic_handler._way_isle(w)

    return _Handler()



class LocalOsmReader:

    def __init__(self, pbf_dosya_yolu: str, db: Optional[Neo4jConnection] = None) -> None:
        self.pbf_dosya_yolu = pbf_dosya_yolu
        self.db = db or Neo4jConnection()

    def omurga_yukle(self) -> Dict[str, int]:
        logger.info(
            "OMURGA yükleme başlıyor (BOOT-zamanı, TÜM ülke, SADECE %s + %s + askeri-us(%s/%s) + "
            "havalimani(%s), SIKI FİLTRE aktif): %s",
            OMURGA_HIGHWAY_TIPLERI, OMURGA_AMENITY_TIPLERI, OMURGA_LANDUSE_TIPLERI,
            OMURGA_MILITARY_TIPLERI, OMURGA_AEROWAY_TIPLERI, self.pbf_dosya_yolu,
        )
        ic_handler = _OsmStreamHandler(
            highway_tipleri=OMURGA_HIGHWAY_TIPLERI,
            amenity_tipleri=OMURGA_AMENITY_TIPLERI,
            bbox=None,
            gecici_mi=False,
            operasyon_id=None,
            siki_filtre=True,
            askeri_havalimani_dahil=True,
        )
        return self._yukle_ve_yaz(ic_handler, "omurga")


    def kriz_bolgesi_yukle(
        self,
        merkez_enlem: float,
        merkez_boylam: float,
        yaricap_km: float = _VARSAYILAN_YARICAP_KM,
        operasyon_id: Optional[str] = None,
        kalici: bool = False,
    ) -> Dict[str, Any]:
      
        if kalici:
            gecici_mi = False
            operasyon_id = None
        else:
            gecici_mi = True
            operasyon_id = operasyon_id or str(uuid.uuid4())
        bbox = _bbox_hesapla(merkez_enlem, merkez_boylam, yaricap_km)
        logger.info(
            "BÖLGE yükleme başlıyor (kalici=%s, operasyon_id=%s, merkez=(%.5f, %.5f), yarıçap=%.1f km, "
            "bbox=%s): %s",
            kalici, operasyon_id, merkez_enlem, merkez_boylam, yaricap_km, bbox, self.pbf_dosya_yolu,
        )
        ic_handler = _OsmStreamHandler(
            highway_tipleri=KILCAL_YALNIZCA_HIGHWAY_TIPLERI,
            amenity_tipleri=(), 
            bbox=bbox,
            gecici_mi=gecici_mi,
            operasyon_id=operasyon_id,
        )
        etiket = "kalici-bolge" if kalici else f"kriz-bolgesi[{operasyon_id}]"
        sonuc = self._yukle_ve_yaz(ic_handler, etiket)
        sonuc["operasyon_id"] = operasyon_id
        return sonuc


    def gecici_kilcal_veriyi_temizle(self, operasyon_id: Optional[str] = None) -> int:
       
        if operasyon_id is not None:
            match_where = "MATCH (n:Infrastructure {gecici_mi: true, operasyon_id: $operasyon_id})"
            hedef_aciklama = f"operasyon_id='{operasyon_id}'"
        else:
            match_where = "MATCH (n:Infrastructure {gecici_mi: true})"
            hedef_aciklama = "TÜM geçici kılcal veri (operasyon_id filtresi YOK)"

        silinen = self.db.batched_bulk_write(
            match_where,
            "DETACH DELETE n",
            params={"operasyon_id": operasyon_id} if operasyon_id is not None else None,
            islem_adi=f"RAM TAHLİYESİ ({hedef_aciklama})",
        )
        logger.info("RAM TAHLİYESİ tamamlandı (%s): %d düğüm silindi.", hedef_aciklama, silinen)
        return silinen

    

    def _yukle_ve_yaz(self, ic_handler: _OsmStreamHandler, etiket: str) -> Dict[str, int]:
        
        _osmium_gerekli()
        gercek_handler = _gercek_handler_olustur(ic_handler)
        try:
            gercek_handler.apply_file(self.pbf_dosya_yolu, locations=True)
        except RuntimeError as exc:
            raise RuntimeError(
                f"Yerel OSM dosyası okunamadı ('{self.pbf_dosya_yolu}'): {exc}. "
                "Dosya yolunun doğru olduğundan ve dosyanın geçerli bir .osm/.osm.pbf "
                "olduğundan emin olun."
            ) from exc

        logger.info(
            "[%s] Yerel dosya tarandı: %d way, %d node işlendi; %d aday varlık bulundu "
            "(siki_filtre=%s ile elenen: %d küçük/isimsiz köprü, %d isimsiz tesis/birim).",
            etiket, ic_handler.way_sayisi, ic_handler.node_sayisi, len(ic_handler.dugumler),
            ic_handler.siki_filtre, ic_handler.elenen_kucuk_kopru_sayisi,
            ic_handler.elenen_isimsiz_tesis_sayisi,
        )
        if not ic_handler.dugumler:
            logger.warning("[%s] Filtreye uyan HİÇBİR varlık bulunamadı — 0 düğüm yazıldı.", etiket)
            return {}

        loader = TucbsETLLoader(self.db)
        sayaclar = loader.bulk_insert_nodes(ic_handler.dugumler, toplam_tahmini=len(ic_handler.dugumler))
        logger.info("[%s] Neo4j'e yazıldı: %s", etiket, sayaclar)
        return sayaclar


def main() -> None:  
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if len(sys.argv) < 3:
        print(
            "Kullanım:\n"
            "  python -m src.data_ingestion.local_osm_reader omurga <dosya.osm.pbf>\n"
            "  python -m src.data_ingestion.local_osm_reader kriz <dosya.osm.pbf> <enlem> <boylam> [yaricap_km]\n"
            "  python -m src.data_ingestion.local_osm_reader temizle <operasyon_id|--hepsi>"
        )
        sys.exit(1)

    komut = sys.argv[1]
    db = Neo4jConnection()
    db.connect()

    if komut == "omurga":
        reader = LocalOsmReader(sys.argv[2], db)
        print(reader.omurga_yukle())
    elif komut == "kriz":
        enlem, boylam = float(sys.argv[3]), float(sys.argv[4])
        yaricap = float(sys.argv[5]) if len(sys.argv) > 5 else _VARSAYILAN_YARICAP_KM
        reader = LocalOsmReader(sys.argv[2], db)
        print(reader.kriz_bolgesi_yukle(enlem, boylam, yaricap))
    elif komut == "temizle":
      
        hedef = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "--hepsi" else None
        reader = LocalOsmReader("", db)
        print(f"{reader.gecici_kilcal_veriyi_temizle(hedef)} düğüm silindi.")
    else:
        print(f"Bilinmeyen komut: {komut}")
        sys.exit(1)

    db.close()


if __name__ == "__main__":  
    main()
