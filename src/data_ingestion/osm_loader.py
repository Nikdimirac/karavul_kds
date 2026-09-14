

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

from src.core.database import Neo4jConnection
from src.core.models import (
    BackupPowerStatus,
    CommunicationNetwork,
    CommunicationNetworkType,
    EnergyInfrastructure,
    EnergyInfrastructureType,
    Facility,
    FacilityStatus,
    FacilityType,
    Infrastructure,
    InfrastructureType,
    MobilityStatus,
    OperationalStatus,
    ResourceHub,
    ResourceType,
    Unit,
    UnitType,
)

logger = logging.getLogger(__name__)



OVERPASS_ENDPOINTS: Tuple[str, ...] = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
)


ELAZIG_BBOX: Tuple[float, float, float, float] = (38.45, 39.00, 38.85, 39.50)

_REQUEST_TIMEOUT_SANIYE = 90
_OVERPASS_ZAMAN_ASIMI_SANIYE = 60  # Overpass QL icindeki [timeout:N]


OVERPASS_REQUEST_HEADERS: Dict[str, str] = {
    "User-Agent": "KrizYonetimKDS_Elazig/1.0 (test_project)"
}


_VARSAYILAN_HASTANE_KAPASITESI = 50.0
_VARSAYILAN_ASKERI_US_KAPASITESI = 100.0
_VARSAYILAN_PERSONEL_SAYISI = 15

_VARSAYILAN_HAVALIMANI_KAPASITESI = 200.0
_VARSAYILAN_LIMAN_KAPASITESI = 100.0

_VARSAYILAN_SANTRAL_KAPASITESI_MW = 50.0
_VARSAYILAN_TRAFO_KAPASITESI_MW = 10.0
_VARSAYILAN_BARAJ_KAPASITESI_MW = 100.0
_VARSAYILAN_BAZ_ISTASYONU_KAPSAMA_KM = 5.0
_VARSAYILAN_YAKIT_STOK_YUZDE = 75.0
_VARSAYILAN_YAKIT_TUKENME_GUN = 30.0


_HIGHWAY_TONAJ_VARSAYIMLARI: Dict[str, float] = {"trunk": 60.0, "primary": 40.0}

_DUNYA_YARICAPI_KM = 6371.0



def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
   
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * _DUNYA_YARICAPI_KM * math.asin(math.sqrt(a))


def _geometri_uzunlugu_km(geometry: Sequence[Dict[str, float]]) -> float:
    
    toplam = 0.0
    for a, b in zip(geometry, geometry[1:]):
        toplam += _haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
    return toplam


def _en_uzak_iki_nokta(
    noktalar: Sequence[Tuple[float, float]]
) -> Tuple[Tuple[float, float], Tuple[float, float]]:
   
    en_iyi_cift = (noktalar[0], noktalar[0])
    en_iyi_mesafe = -1.0
    for i in range(len(noktalar)):
        for j in range(i + 1, len(noktalar)):
            mesafe = _haversine_km(*noktalar[i], *noktalar[j])
            if mesafe > en_iyi_mesafe:
                en_iyi_mesafe = mesafe
                en_iyi_cift = (noktalar[i], noktalar[j])
    return en_iyi_cift


def _eleman_koordinati(el: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
   
    if "lat" in el and "lon" in el:
        return el["lat"], el["lon"]
    center = el.get("center")
    if center:
        return center.get("lat"), center.get("lon")
    return None, None


def _varsayilan_isim(el: Dict[str, Any], tip_etiketi: str) -> str:
   
    osm_tip = el.get("type", "node")
    osm_id = el.get("id", "?")
    return f"{tip_etiketi} (OSM {osm_tip}/{osm_id})"


def _isimli_veya_teknik_isim(el: Dict[str, Any], gercek_ad: Optional[str], tip_etiketi: str) -> str:
   
    osm_tip = el.get("type", "node")
    osm_id = el.get("id", "?")
    taban = gercek_ad or tip_etiketi
    return f"{taban} (OSM {osm_tip}/{osm_id})"


def _tahmini_sayisal_deger(tags: Dict[str, Any], anahtarlar: Sequence[str], varsayilan: float) -> float:
    for anahtar in anahtarlar:
        deger = tags.get(anahtar)
        if deger is None:
            continue
        try:
            return float(deger)
        except (TypeError, ValueError):
            continue
    return varsayilan


class OSMBaselineLoader:
    

    def __init__(
        self,
        db: Optional[Neo4jConnection] = None,
        bbox: Tuple[float, float, float, float] = ELAZIG_BBOX,
        overpass_endpoints: Sequence[str] = OVERPASS_ENDPOINTS,
    ) -> None:
        self.db = db or Neo4jConnection()
        self.bbox = bbox
        self._endpoints = list(overpass_endpoints)

   
    def load_baseline(self) -> Dict[str, int]:
        
        varliklar = self.build_nodes()
        sayaclar: Dict[str, int] = {}
        for anahtar, liste in varliklar.items():
            for node in liste:
                self.db.add_node(node)
            sayaclar[anahtar] = len(liste)
            logger.info("Baseline yuklendi: %s -> %d dugum", anahtar, len(liste))
        return sayaclar

    def build_nodes(self) -> Dict[str, List[Any]]:
       
        ham = self.fetch_raw_elements()
        facilities, units, energiler, iletisimler, kaynaklar = self._convert_point_elements(ham["noktalar"])
        infrastructures = self._convert_highway_elements(ham["yollar"])
        return {
            "facilities": facilities,
            "units": units,
            "energy_infrastructures": energiler,
            "communication_networks": iletisimler,
            "resource_hubs": kaynaklar,
            "infrastructures": infrastructures,
        }

    def fetch_raw_elements(self) -> Dict[str, List[Dict[str, Any]]]:
       
        nokta_elemanlari = self._run_overpass_query(self._build_point_query())["elements"]
        yol_elemanlari = self._run_overpass_query(self._build_highway_query())["elements"]
        return {"noktalar": nokta_elemanlari, "yollar": yol_elemanlari}


    def _bbox_str(self) -> str:
        south, west, north, east = self.bbox
        return f"{south},{west},{north},{east}"

    def _build_point_query(self) -> str:
        bbox = self._bbox_str()
        return f"""
[out:json][timeout:{_OVERPASS_ZAMAN_ASIMI_SANIYE}];
(
  node["amenity"="hospital"]({bbox});
  way["amenity"="hospital"]({bbox});
  node["amenity"="fire_station"]({bbox});
  way["amenity"="fire_station"]({bbox});
  node["amenity"="police"]({bbox});
  way["amenity"="police"]({bbox});
  node["landuse"="military"]({bbox});
  way["landuse"="military"]({bbox});
  node["amenity"="military"]({bbox});
  way["amenity"="military"]({bbox});
  node["aeroway"="aerodrome"]({bbox});
  way["aeroway"="aerodrome"]({bbox});
  node["landuse"="port"]({bbox});
  way["landuse"="port"]({bbox});
  node["industrial"="port"]({bbox});
  way["industrial"="port"]({bbox});
  node["harbour"="yes"]({bbox});
  way["harbour"="yes"]({bbox});
  node["power"="plant"]({bbox});
  way["power"="plant"]({bbox});
  node["power"="substation"]({bbox});
  way["power"="substation"]({bbox});
  node["waterway"="dam"]({bbox});
  way["waterway"="dam"]({bbox});
  node["man_made"="communications_tower"]({bbox});
  way["man_made"="communications_tower"]({bbox});
  node["amenity"="fuel"]({bbox});
  way["amenity"="fuel"]({bbox});
);
out center tags;
"""

    def _build_highway_query(self) -> str:
        bbox = self._bbox_str()
        return f"""
[out:json][timeout:{_OVERPASS_ZAMAN_ASIMI_SANIYE}];
(
  way["highway"~"^(primary|trunk)$"]({bbox});
);
out geom tags;
"""

    def _run_overpass_query(self, query: str) -> Dict[str, Any]:
        
        son_hata: Optional[Exception] = None
        for endpoint in self._endpoints:
            try:
                yanit = requests.post(
                    endpoint,
                    data={"data": query},
                    timeout=_REQUEST_TIMEOUT_SANIYE,
                    headers=OVERPASS_REQUEST_HEADERS,
                )
                yanit.raise_for_status()
                return yanit.json()
            except (requests.RequestException, ValueError) as exc:
                logger.warning("Overpass sorgusu basarisiz oldu (%s): %s", endpoint, exc)
                son_hata = exc
                continue
        raise RuntimeError(
            f"Hicbir Overpass API ucuna ulasilamadi (denenenler: {self._endpoints}): {son_hata}"
        ) from son_hata

    
    @staticmethod
    def _convert_point_elements(
        elements: List[Dict[str, Any]],
    ) -> Tuple[List[Facility], List[Unit], List[EnergyInfrastructure], List[CommunicationNetwork], List[ResourceHub]]:
       
        facilities: List[Facility] = []
        units: List[Unit] = []
        energiler: List[EnergyInfrastructure] = []
        iletisimler: List[CommunicationNetwork] = []
        kaynaklar: List[ResourceHub] = []

        for el in elements:
            tags = el.get("tags", {})
            enlem, boylam = _eleman_koordinati(el)
            if enlem is None or boylam is None:
                continue

            amenity = tags.get("amenity")
            is_military = tags.get("landuse") == "military" or amenity == "military"
            is_havalimani = tags.get("aeroway") == "aerodrome"
            is_liman = (
                tags.get("landuse") == "port" or tags.get("industrial") == "port" or tags.get("harbour") == "yes"
            )
            is_santral = tags.get("power") == "plant"
            is_trafo = tags.get("power") == "substation"
            is_baraj = tags.get("waterway") == "dam"
            is_baz_istasyonu = tags.get("man_made") == "communications_tower"
            is_yakit = amenity == "fuel"
            gercek_ad = tags.get("name")

            if amenity == "hospital":
                facilities.append(
                    Facility(
                        isim=_isimli_veya_teknik_isim(el, gercek_ad, "Hastane"),
                        aciklama=gercek_ad,
                        enlem=enlem,
                        boylam=boylam,
                        durum=OperationalStatus.AKTIF,
                        facility_type=FacilityType.HASTANE,
                        kapasite=_tahmini_sayisal_deger(
                            tags, ("capacity:beds", "beds", "capacity"), _VARSAYILAN_HASTANE_KAPASITESI
                        ),
                        mevcut_durum=FacilityStatus.AKTIF,
                    )
                )
            elif is_military:
                facilities.append(
                    Facility(
                        isim=_isimli_veya_teknik_isim(el, gercek_ad, "Askeri Alan"),
                        aciklama=gercek_ad,
                        enlem=enlem,
                        boylam=boylam,
                        durum=OperationalStatus.AKTIF,
                        facility_type=FacilityType.ASKERI_US,
                        kapasite=_VARSAYILAN_ASKERI_US_KAPASITESI,
                        mevcut_durum=FacilityStatus.AKTIF,
                    )
                )
            elif is_havalimani:
                facilities.append(
                    Facility(
                        isim=_isimli_veya_teknik_isim(el, gercek_ad, "Havalimani"),
                        aciklama=gercek_ad,
                        enlem=enlem,
                        boylam=boylam,
                        durum=OperationalStatus.AKTIF,
                        facility_type=FacilityType.HAVALIMANI,
                        kapasite=_VARSAYILAN_HAVALIMANI_KAPASITESI,
                        mevcut_durum=FacilityStatus.AKTIF,
                    )
                )
            elif is_liman:
                facilities.append(
                    Facility(
                        isim=_isimli_veya_teknik_isim(el, gercek_ad, "Liman"),
                        aciklama=gercek_ad,
                        enlem=enlem,
                        boylam=boylam,
                        durum=OperationalStatus.AKTIF,
                        facility_type=FacilityType.LIMAN,
                        kapasite=_VARSAYILAN_LIMAN_KAPASITESI,
                        mevcut_durum=FacilityStatus.AKTIF,
                    )
                )
            elif is_santral:
                energiler.append(
                    EnergyInfrastructure(
                        isim=_isimli_veya_teknik_isim(el, gercek_ad, "Elektrik Santrali"),
                        aciklama=gercek_ad,
                        enlem=enlem,
                        boylam=boylam,
                        durum=OperationalStatus.AKTIF,
                        energy_type=EnergyInfrastructureType.SANTRAL,
                        kapasite_mw=_tahmini_sayisal_deger(
                            tags, ("plant:output:electricity",), _VARSAYILAN_SANTRAL_KAPASITESI_MW
                        ),
                        yedek_guc_durumu=BackupPowerStatus.YOK,
                    )
                )
            elif is_trafo:
                energiler.append(
                    EnergyInfrastructure(
                        isim=_isimli_veya_teknik_isim(el, gercek_ad, "Trafo Merkezi"),
                        aciklama=gercek_ad,
                        enlem=enlem,
                        boylam=boylam,
                        durum=OperationalStatus.AKTIF,
                        energy_type=EnergyInfrastructureType.TRAFO,
                        kapasite_mw=_VARSAYILAN_TRAFO_KAPASITESI_MW,
                        yedek_guc_durumu=BackupPowerStatus.YOK,
                    )
                )
            elif is_baraj:
                energiler.append(
                    EnergyInfrastructure(
                        isim=_isimli_veya_teknik_isim(el, gercek_ad, "Baraj"),
                        aciklama=gercek_ad,
                        enlem=enlem,
                        boylam=boylam,
                        durum=OperationalStatus.AKTIF,
                        energy_type=EnergyInfrastructureType.BARAJ,
                        kapasite_mw=_VARSAYILAN_BARAJ_KAPASITESI_MW,
                        yedek_guc_durumu=BackupPowerStatus.YOK,
                    )
                )
            elif is_baz_istasyonu:
                iletisimler.append(
                    CommunicationNetwork(
                        isim=_isimli_veya_teknik_isim(el, gercek_ad, "Baz Istasyonu"),
                        aciklama=gercek_ad,
                        enlem=enlem,
                        boylam=boylam,
                        durum=OperationalStatus.AKTIF,
                        network_type=CommunicationNetworkType.BAZ_ISTASYONU,
                        kapsama_yaricapi_km=_VARSAYILAN_BAZ_ISTASYONU_KAPSAMA_KM,
                    )
                )
            elif is_yakit:
                kaynaklar.append(
                    ResourceHub(
                        isim=_isimli_veya_teknik_isim(el, gercek_ad, "Yakit Istasyonu"),
                        aciklama=gercek_ad,
                        enlem=enlem,
                        boylam=boylam,
                        durum=OperationalStatus.AKTIF,
                        resource_type=ResourceType.YAKIT,
                        stok_seviyesi_yuzde=_VARSAYILAN_YAKIT_STOK_YUZDE,
                        tukenme_hizi_gun=_VARSAYILAN_YAKIT_TUKENME_GUN,
                    )
                )
            elif amenity == "fire_station":
                units.append(
                    Unit(
                        isim=_isimli_veya_teknik_isim(el, gercek_ad, "Itfaiye Istasyonu"),
                        aciklama=gercek_ad,
                        enlem=enlem,
                        boylam=boylam,
                        durum=OperationalStatus.AKTIF,
                        unit_type=UnitType.ITFAIYE,
                        personel_sayisi=int(
                            _tahmini_sayisal_deger(tags, ("staff_count",), _VARSAYILAN_PERSONEL_SAYISI)
                        ),
                        hareket_kabiliyeti=MobilityStatus.SINIRLI_HAREKETLI,
                    )
                )
            elif amenity == "police":
                units.append(
                    Unit(
                        isim=_isimli_veya_teknik_isim(el, gercek_ad, "Polis Karakolu"),
                        aciklama=gercek_ad,
                        enlem=enlem,
                        boylam=boylam,
                        durum=OperationalStatus.AKTIF,
                        unit_type=UnitType.POLIS,
                        personel_sayisi=int(
                            _tahmini_sayisal_deger(tags, ("staff_count",), _VARSAYILAN_PERSONEL_SAYISI)
                        ),
                        hareket_kabiliyeti=MobilityStatus.SINIRLI_HAREKETLI,
                    )
                )


        return facilities, units, energiler, iletisimler, kaynaklar

    @staticmethod
    def _convert_highway_elements(elements: List[Dict[str, Any]]) -> List[Infrastructure]:
      
        gruplar: Dict[str, Dict[str, Any]] = {}

        for el in elements:
            geometry = el.get("geometry")
            if not geometry or len(geometry) < 2:
                continue
            tags = el.get("tags", {})
            isim = tags.get("name") or _varsayilan_isim(el, "Karayolu")
            highway_tipi = tags.get("highway", "primary")

            grup = gruplar.setdefault(
                isim, {"toplam_km": 0.0, "uc_noktalar": [], "highway_tipi": highway_tipi}
            )
            grup["toplam_km"] += _geometri_uzunlugu_km(geometry)
            grup["uc_noktalar"].append((geometry[0]["lat"], geometry[0]["lon"]))
            grup["uc_noktalar"].append((geometry[-1]["lat"], geometry[-1]["lon"]))

        infra_listesi: List[Infrastructure] = []
        for isim, grup in gruplar.items():
            uc_noktalar = grup["uc_noktalar"]
            if len(uc_noktalar) < 2:
                continue
            (enlem1, boylam1), (enlem2, boylam2) = _en_uzak_iki_nokta(uc_noktalar)
            tonaj = _HIGHWAY_TONAJ_VARSAYIMLARI.get(grup["highway_tipi"], 40.0)

            infra_listesi.append(
                Infrastructure(
                    isim=isim,
                    enlem=enlem1,
                    boylam=boylam1,
                    bitis_enlem=enlem2,
                    bitis_boylam=boylam2,
                    durum=OperationalStatus.AKTIF,
                    infrastructure_type=InfrastructureType.KARAYOLU,
                    tonaj_kapasitesi=tonaj,
                    uzunluk_km=round(grup["toplam_km"], 2),
                    acik_mi=True,
                )
            )
        return infra_listesi


def main() -> None:  
    logging.basicConfig(level=logging.INFO)

    db = Neo4jConnection()
    db.connect()
    db.ensure_constraints()

    loader = OSMBaselineLoader(db)
    print("Overpass API'den Elazığ bölgesi 'Barış Zamanı' (baseline) verisi çekiliyor...")
    sayaclar = loader.load_baseline()

    print("\nBaseline Bilgi Grafı'na yüklendi:")
    for anahtar, adet in sayaclar.items():
        print(f"  - {anahtar}: {adet}")


if __name__ == "__main__":
    main()
