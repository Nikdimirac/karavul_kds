

from __future__ import annotations

import difflib
import heapq
import logging
import math
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from dotenv import load_dotenv
from neo4j import Driver, GraphDatabase, Session, unit_of_work
from neo4j.exceptions import Neo4jError, ServiceUnavailable

from src.core.models import (
    NODE_REGISTRY,
    BaseNode,
    NodeLabel,
    Relationship,
    RelationshipType,
)


try:
    from geopy.exc import GeocoderServiceError, GeocoderTimedOut
    from geopy.geocoders import Nominatim

    _GEOPY_MEVCUT = True
except ImportError:  
    _GEOPY_MEVCUT = False

logger = logging.getLogger(__name__)

load_dotenv()


class Neo4jConnectionError(RuntimeError):
    """Neo4j bağlantı/işlem hatalarını sarmalayan uygulama-özel hata sınıfı."""




_TURKISH_FOLD_MAP = str.maketrans(
    {
        "İ": "i", "I": "i", "ı": "i",
        "Ş": "s", "ş": "s",
        "Ğ": "g", "ğ": "g",
        "Ü": "u", "ü": "u",
        "Ö": "o", "ö": "o",
        "Ç": "c", "ç": "c",
    }
)


def _fold_isim(value: str) -> str:
 
    return " ".join(value.strip().translate(_TURKISH_FOLD_MAP).lower().split())


FUZZY_ISIM_MATCH_THRESHOLD = 0.85


_ALTYAPI_TIP_KELIMELERI = frozenset(
    {"caddesi", "cadde", "cad", "sokak", "sokagi", "sok", "sk", "bulvari", "bulvar", "blv"}
)


def _altyapi_adi_anahtari(isim: str) -> str:
   
    katlanmis = _fold_isim(isim)
    kelimeler = [k for k in katlanmis.split() if k not in _ALTYAPI_TIP_KELIMELERI]
    return "".join(kelimeler)


FUZZY_ALTYAPI_ESIK = 0.75

def _konum_point_ifadesi(props_ifadesi: str) -> str:
  
    return f"point({{latitude: {props_ifadesi}.enlem, longitude: {props_ifadesi}.boylam, crs: 'wgs-84'}})"


class Neo4jConnection:
    

    _instance: Optional["Neo4jConnection"] = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls, *args: Any, **kwargs: Any) -> "Neo4jConnection":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:  
                    instance = super().__new__(cls)
                    instance._initialized = False
                    cls._instance = instance
        return cls._instance

    def __init__(
        self,
        uri: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None,
    ) -> None:
       
        if getattr(self, "_initialized", False):
            return

        self._uri: str = uri or os.getenv("NEO4J_URI", "bolt://localhost:7687")
        self._user: str = user or os.getenv("NEO4J_USER", "neo4j")
        self._password: str = password or os.getenv("NEO4J_PASSWORD", "")
        self._database: str = database or os.getenv("NEO4J_DATABASE", "neo4j")

        if not self._password:
            logger.warning(
                "NEO4J_PASSWORD .env dosyasinda tanimli degil; "
                "bos sifre ile baglanti denenecek."
            )

        self._driver: Optional[Driver] = None

       
        self._yol_agi_onbellek: Dict[Tuple[float, float, float], Any] = {}
        self._yol_agi_onbellek_zamani: Dict[Tuple[float, float, float], float] = {}

      
        self._geolocator: Optional[Any] = None
        self._geokodlama_onbellek: Dict[str, Optional[Tuple[float, float]]] = {}
        self._son_geokodlama_zamani: float = 0.0

        self._initialized = True

    
    def connect(self) -> Driver:
        if self._driver is None:
            try:
                self._driver = GraphDatabase.driver(
                    self._uri,
                    auth=(self._user, self._password),
                   
                    max_connection_pool_size=50,
                    connection_acquisition_timeout=60.0,
                )
                self._driver.verify_connectivity()
                logger.info("Neo4j baglantisi kuruldu: %s (db=%s)", self._uri, self._database)
            except ServiceUnavailable as exc:
                self._driver = None
                raise Neo4jConnectionError(
                    f"Neo4j'e baglanilamadi: {self._uri}"
                ) from exc
        return self._driver

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None
            logger.info("Neo4j baglantisi kapatildi.")

    @classmethod
    def reset_singleton(cls) -> None:
        with cls._lock:
            if cls._instance is not None:
                cls._instance.close()
            cls._instance = None

    @property
    def driver(self) -> Driver:
        return self.connect()

    def session(self, **kwargs: Any) -> Session:
        return self.driver.session(database=self._database, **kwargs)

    def __enter__(self) -> "Neo4jConnection":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

   
    _VARSAYILAN_SORGU_ZAMAN_ASIMI_SANIYE = 60.0
   

    def execute_query(
        self,
        query: str,
        parameters: Optional[Dict[str, Any]] = None,
        write: bool = False,
        timeout: Optional[float] = _VARSAYILAN_SORGU_ZAMAN_ASIMI_SANIYE,
    ) -> List[Dict[str, Any]]:
        
        parameters = parameters or {}

        @unit_of_work(timeout=timeout)
        def _tx_calistir(tx: Any) -> List[Any]:
            return list(tx.run(query, parameters))

        try:
            with self.session() as session:
                if write:
                    records = session.execute_write(_tx_calistir)
                else:
                    records = session.execute_read(_tx_calistir)
                return [record.data() for record in records]
        except Neo4jError as exc:
            logger.error("Cypher sorgusu basarisiz oldu | query=%s | hata=%s", query, exc)
            raise Neo4jConnectionError(str(exc)) from exc

    

    @staticmethod
    def node_to_cypher(node: BaseNode, merge_isim: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
        
        etiketler = node.neo4j_labels()
        kategori_etiketi = etiketler[0]
        ek_etiketler = etiketler[1:]

        props = node.to_cypher_properties()
        merge_key = merge_isim if merge_isim is not None else node.isim
        update_props = {key: value for key, value in props.items() if key not in ("id", "isim")}

        ek_etiket_clause = ""
        if ek_etiketler:
            ek_etiket_str = "".join(f":{etiket}" for etiket in ek_etiketler)
            ek_etiket_clause = f" SET n{ek_etiket_str}"

      
        query = (
            f"MERGE (n:{kategori_etiketi} {{isim: $isim}}) "
            "ON CREATE SET n = $props, "
            f"n.konum = {_konum_point_ifadesi('$props')} "
            "ON MATCH SET n += $update_props, "
            f"n.konum = {_konum_point_ifadesi('$update_props')}"
            f"{ek_etiket_clause} "
            "RETURN n"
        )
        parameters = {"isim": merge_key, "props": props, "update_props": update_props}
        return query, parameters

    @staticmethod
    def relationship_to_cypher(
        relationship: Relationship,
        kaynak_isim: Optional[str] = None,
        hedef_isim: Optional[str] = None,
    ) -> Tuple[str, Dict[str, Any]]:
       
        rel_type = relationship.tip.value
        props = relationship.to_cypher_properties()
        query = (
            "MATCH (a {isim: $kaynak_isim}) "
            "MATCH (b {isim: $hedef_isim}) "
            f"MERGE (a)-[r:{rel_type}]->(b) "
            "SET r += $props "
            "RETURN a, r, b"
        )
        parameters = {
            "kaynak_isim": kaynak_isim if kaynak_isim is not None else relationship.kaynak_id,
            "hedef_isim": hedef_isim if hedef_isim is not None else relationship.hedef_id,
            "props": props,
        }
        return query, parameters

    def _resolve_canonical_isim(self, isim: str, label: Optional[str] = None) -> str:
        
        if label:
            tam_sonuc = self.execute_query(
                f"MATCH (n:{label} {{isim: $isim}}) RETURN n.isim AS isim LIMIT 1",
                {"isim": isim},
                write=False,
            )
        else:
            
            union_dallari = " UNION ALL ".join(
                f"MATCH (n:{etiket.value} {{isim: $isim}}) RETURN n.isim AS isim LIMIT 1"
                for etiket in NODE_REGISTRY
            )
            tam_sonuc = self.execute_query(union_dallari, {"isim": isim}, write=False)
        if tam_sonuc:
            return tam_sonuc[0]["isim"]

        label_clause = f":{label}" if label else ""
        existing = self.execute_query(
            f"MATCH (n{label_clause}) WHERE n.isim IS NOT NULL AND NOT n:Street AND NOT n:Bridge "
            "RETURN DISTINCT n.isim AS isim",
            write=False,
        )
        existing_isimler = [row["isim"] for row in existing]
        if not existing_isimler:
            return isim

        folded_target = _fold_isim(isim)
        best_match: Optional[str] = None
        best_ratio = 0.0
        for candidate in existing_isimler:
            ratio = difflib.SequenceMatcher(None, folded_target, _fold_isim(candidate)).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = candidate

        if best_match is not None and best_ratio >= FUZZY_ISIM_MATCH_THRESHOLD:
            logger.info(
                "Bulanik isim eslesmesi: '%s' -> '%s' (benzerlik=%.2f) ayni duguma MERGE edilecek.",
                isim, best_match, best_ratio,
            )
            return best_match
        return isim

   
    _KOORDINAT_ZARFI_CACHE_SANIYE = 60.0
   

    _koordinat_zarfi_onbellek: Optional[Tuple[float, float, float, float]] = None
    _koordinat_zarfi_onbellek_zamani: float = 0.0

    def get_real_koordinat_zarfi(
        self, marj_derece: float = 1.0, _onbellek_atla: bool = False
    ) -> Optional[Tuple[float, float, float, float]]:
        
        simdi = time.monotonic()
        if (
            not _onbellek_atla
            and self._koordinat_zarfi_onbellek is not None
            and (simdi - self._koordinat_zarfi_onbellek_zamani) < self._KOORDINAT_ZARFI_CACHE_SANIYE
        ):
            return self._koordinat_zarfi_onbellek

        sonuc = self.execute_query(
            "MATCH (n) WHERE n:Street OR n:Bridge "
            "RETURN percentileDisc(n.enlem, 0.01) AS enlem_min, percentileDisc(n.enlem, 0.99) AS enlem_max, "
            "percentileDisc(n.boylam, 0.01) AS boylam_min, percentileDisc(n.boylam, 0.99) AS boylam_max",
            write=False,
        )
        if not sonuc or sonuc[0]["enlem_min"] is None:
            self._koordinat_zarfi_onbellek = None
            self._koordinat_zarfi_onbellek_zamani = simdi
            return None

        r = sonuc[0]
        zarf = (
            r["enlem_min"] - marj_derece,
            r["enlem_max"] + marj_derece,
            r["boylam_min"] - marj_derece,
            r["boylam_max"] + marj_derece,
        )
        self._koordinat_zarfi_onbellek = zarf
        self._koordinat_zarfi_onbellek_zamani = simdi
        return zarf

    @staticmethod
    def koordinat_gercekci_mi(
        enlem: Optional[float], boylam: Optional[float], zarf: Optional[Tuple[float, float, float, float]]
    ) -> bool:
       
        if zarf is None or enlem is None or boylam is None:
            return True
        enlem_min, enlem_max, boylam_min, boylam_max = zarf
        return enlem_min <= enlem <= enlem_max and boylam_min <= boylam <= boylam_max

  
    _YER_ADI_DURAK_KELIMELERI = frozenset(
        {
            "depremi", "deprem", "seli", "sel", "yangini", "yangin", "binasi",
            "bina", "merkezi", "istasyonu", "istasyon", "deposu", "koprusu",
            "kopru", "tuneli", "tunel", "kavsagi", "kavsak", "caddesi", "cadde",
            "sokak", "sokagi", "bulvari", "bulvar", "hastanesi", "hastane",
            "havalimani", "ussu", "us", "limani", "liman", "siginagi", "siginak",
            "timi", "tim", "birligi", "birlik", "ekibi", "ekip", "noktasi",
            "nokta", "patlamasi", "patlama", "saldirisi", "saldiri", "kazasi",
            "kaza", "olayi", "olay", "ile", "veya", "yolu", "yol",
        }
    )
    

    def _yer_adi_adaylarini_cikar(self, isim: str) -> List[str]:
      
        kelimeler = re.findall(r"[A-Za-zÇĞİÖŞÜçğıöşü]+", isim or "")
        if not kelimeler:
            return []

        adaylar: List[str] = []
        ilk = kelimeler[0]
        if len(ilk) >= 3:
            adaylar.append(ilk)
        for kelime in kelimeler[1:]:
            if len(kelime) >= 4 and kelime.lower() not in self._YER_ADI_DURAK_KELIMELERI:
                adaylar.append(kelime)
        return adaylar

    _yerlesim_onbellek: Optional[List[Dict[str, Any]]] = None
    _yerlesim_onbellek_zamani: float = 0.0
    _YERLESIM_ONBELLEK_CACHE_SANIYE = 60.0
    "

    def _yerlesimleri_getir(self) -> List[Dict[str, Any]]:
      
        simdi = time.monotonic()
        if (
            self._yerlesim_onbellek is not None
            and (simdi - self._yerlesim_onbellek_zamani) < self._YERLESIM_ONBELLEK_CACHE_SANIYE
        ):
            return self._yerlesim_onbellek
        sonuc = self.execute_query(
            "MATCH (s:Settlement) RETURN s.isim AS isim, s.il AS il, s.enlem AS enlem, s.boylam AS boylam",
            write=False,
        )
        self._yerlesim_onbellek = sonuc
        self._yerlesim_onbellek_zamani = simdi
        return sonuc

    def _yer_adindan_gercek_merkez_koordinat_bul(self, isim: str) -> Optional[Tuple[float, float]]:
        
        adaylar = self._yer_adi_adaylarini_cikar(isim)
        if not adaylar:
            return None
        adaylar_katlanmis = {_fold_isim(a) for a in adaylar}

        yerlesimler = self._yerlesimleri_getir()
        for aday in adaylar:
            aday_katlanmis = _fold_isim(aday)
            
            eslesenler = [
                s for s in yerlesimler
                if s.get("isim") and _fold_isim(s["isim"].split(" (")[0]) == aday_katlanmis
            ]
            if not eslesenler:
                continue
            if len(eslesenler) == 1:
                s = eslesenler[0]
                logger.info(
                    "COĞRAFİ BAĞLAMA (Settlement): '%s' adayı '%s' (%s) ile eşleşti; "
                    "merkez koordinat (%.5f, %.5f) kullanılacak.",
                    aday, s["isim"], s.get("il"), s["enlem"], s["boylam"],
                )
                return s["enlem"], s["boylam"]

            il_ile_eslesen = [
                s for s in eslesenler
                if s.get("il") and _fold_isim(s["il"]) in adaylar_katlanmis
            ]
            if len(il_ile_eslesen) == 1:
                s = il_ile_eslesen[0]
                logger.info(
                    "COĞRAFİ BAĞLAMA (Settlement, İL/İLÇE HİYERARŞİSİYLE ÇAKIŞMA ÇÖZÜLDÜ): "
                    "'%s' adayı %d farklı ilde eşleşti (%s); raporda geçen il ipucu sayesinde "
                    "'%s' (%s) seçildi, merkez koordinat (%.5f, %.5f) kullanılacak.",
                    aday, len(eslesenler), [e.get("il") for e in eslesenler],
                    s["isim"], s.get("il"), s["enlem"], s["boylam"],
                )
                return s["enlem"], s["boylam"]

           
            logger.warning(
                "COĞRAFİ BAĞLAMA: '%s' adayı %d FARKLI ilde eşleşti (%s) ve raporda "
                "hiçbiriyle eşleşen bir il ipucu yok — belirsizlik nedeniyle bu aday "
                "ATLANDI (ortalama/tahmin ÜRETİLMEDİ, sıradaki adaya/kademeye geçiliyor).",
                aday, len(eslesenler), [e.get("il") for e in eslesenler],
            )

        for aday in adaylar:
            sonuc = self.execute_query(
                "MATCH (n) WHERE (n:Street OR n:Bridge) AND n.acik_mi = true "
                "AND n.isim CONTAINS $terim AND n.enlem IS NOT NULL AND n.boylam IS NOT NULL "
             
                "AND (n.highway_tipi IS NULL OR NOT n.highway_tipi IN ['motorway', 'trunk']) "
                "RETURN avg(n.enlem) AS enlem, avg(n.boylam) AS boylam, count(n) AS adet",
                {"terim": aday}, write=False,
            )
            if sonuc and sonuc[0]["adet"] and sonuc[0]["adet"] > 0:
                logger.info(
                    "COĞRAFİ BAĞLAMA (Street/Bridge): '%s' adayı GERÇEK veride %d noktayla "
                    "eşleşti; merkez koordinat (%.5f, %.5f) kullanılacak.",
                    aday, sonuc[0]["adet"], sonuc[0]["enlem"], sonuc[0]["boylam"],
                )
                return sonuc[0]["enlem"], sonuc[0]["boylam"]
        return None

    _GEOKODLAMA_MIN_ARALIK_SANIYE = 1.1
    

    def harici_geokodlama_ile_koordinat_bul(self, isim: str) -> Optional[Tuple[float, float]]:
       
        if not _GEOPY_MEVCUT:
            logger.warning(
                "Harici geokodlama İSTENDİ ama 'geopy' paketi KURULU DEĞİL "
                "(bkz. requirements.txt); bu kademe ATLANIYOR."
            )
            return None

        adaylar = self._yer_adi_adaylarini_cikar(isim)
        if not adaylar:
            return None
        sorgu = adaylar[0]

        onbellek_anahtari = sorgu.lower()
        if onbellek_anahtari in self._geokodlama_onbellek:
            return self._geokodlama_onbellek[onbellek_anahtari]

        if self._geolocator is None:
            
            self._geolocator = Nominatim(
                user_agent="kriz-karar-destek-sistemi-c4isr/1.0", timeout=5
            )

        gecen_sure = time.monotonic() - self._son_geokodlama_zamani
        if gecen_sure < self._GEOKODLAMA_MIN_ARALIK_SANIYE:
            time.sleep(self._GEOKODLAMA_MIN_ARALIK_SANIYE - gecen_sure)

        sonuc: Optional[Tuple[float, float]] = None
        try:
            self._son_geokodlama_zamani = time.monotonic()
            konum = self._geolocator.geocode(
                f"{sorgu}, Türkiye", country_codes="tr", exactly_one=True
            )
            if konum is not None:
                sonuc = (konum.latitude, konum.longitude)
                logger.info(
                    "HARİCİ GEOKODLAMA (Nominatim): '%s' -> (%.5f, %.5f).",
                    sorgu, sonuc[0], sonuc[1],
                )
            else:
                logger.warning("HARİCİ GEOKODLAMA: '%s' için Nominatim'de sonuç bulunamadı.", sorgu)
        except (GeocoderTimedOut, GeocoderServiceError) as exc:
            logger.warning("HARİCİ GEOKODLAMA başarısız oldu ('%s'): %s", sorgu, exc)
        except Exception as exc:  
            logger.warning("HARİCİ GEOKODLAMA beklenmedik hatayla başarısız oldu ('%s'): %s", sorgu, exc)

        self._geokodlama_onbellek[onbellek_anahtari] = sonuc
        return sonuc


    _YOL_AGI_ARAMA_YARICAPI_KM = 25.0
    
    _HEDEF_ADAY_COKLUGU_KATSAYISI = 20
   
    _HEDEF_ADAY_MIN_LIMITI = 50
   

    _YOL_AGI_DUGUM_GUVENLIK_LIMITI = 150_000


    _KESISIM_TOLERANSI_METRE = 100.0
  
    _HEDEF_SNAP_YARICAPI_KM = 5.0
  

    _KANCA_MAKS_MESAFE_KM = 1.0
   
    _OSM_WAY_INDEX_DESENI = re.compile(r"\(OSM way/(\d+)(?:\s*#(\d+))?\)")

    @staticmethod
    def _geodesic_km(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
      
        R_KM = 6371.0088
        lat1, lon1 = math.radians(p1[0]), math.radians(p1[1])
        lat2, lon2 = math.radians(p2[0]), math.radians(p2[1])
        dlat, dlon = lat2 - lat1, lon2 - lon1
        a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return 2 * R_KM * math.asin(min(1.0, math.sqrt(a)))

    @classmethod
    def _osm_way_ve_indeks_ayikla(cls, isim: str) -> Optional[Tuple[str, Optional[int]]]:
        
        eslesme = cls._OSM_WAY_INDEX_DESENI.search(isim or "")
        if not eslesme:
            return None
        idx_str = eslesme.group(2)
        return eslesme.group(1), (int(idx_str) if idx_str is not None else None)

    def _yerel_acik_yol_agini_getir(self, enlem: float, boylam: float, yaricap_km: float) -> List[Dict[str, Any]]:
  
        return self.execute_query(
            "MATCH (n:Infrastructure) WHERE (n:Street OR n:Bridge) AND n.acik_mi = true "
            "AND n.konum IS NOT NULL "
            "AND point.distance(n.konum, point({latitude: $enlem, longitude: $boylam, crs: 'wgs-84'})) "
            "<= $yaricap_metre "
            "WITH n, point.distance(n.konum, point({latitude: $enlem, longitude: $boylam, crs: 'wgs-84'})) "
            "AS mesafe_metre "
            "RETURN n.isim AS isim, n.enlem AS enlem, n.boylam AS boylam "
            "ORDER BY mesafe_metre ASC LIMIT $dugum_limiti",
            {
                "enlem": enlem, "boylam": boylam, "yaricap_metre": yaricap_km * 1000.0,
                "dugum_limiti": self._YOL_AGI_DUGUM_GUVENLIK_LIMITI,
            },
            write=False,
        )

    @classmethod
    def _yol_agi_kur(
        cls, noktalar: List[Dict[str, Any]]
    ) -> Tuple[Dict[str, List[Tuple[str, float]]], Dict[str, Tuple[float, float]]]:
       
        konum: Dict[str, Tuple[float, float]] = {n["isim"]: (n["enlem"], n["boylam"]) for n in noktalar}
        komsuluk: Dict[str, List[Tuple[str, float]]] = {isim: [] for isim in konum}

      
        way_gruplari: Dict[str, List[Tuple[int, str]]] = {}
        for isim in konum:
            ayiklanan = cls._osm_way_ve_indeks_ayikla(isim)
            if ayiklanan is None or ayiklanan[1] is None:
                continue
            way_id, idx = ayiklanan
            way_gruplari.setdefault(way_id, []).append((idx, isim))

        for elemanlar in way_gruplari.values():
            elemanlar.sort(key=lambda t: t[0])
            for (idx1, isim1), (idx2, isim2) in zip(elemanlar, elemanlar[1:]):
                if idx2 - idx1 != 1:
                    continue 
                agirlik = cls._geodesic_km(konum[isim1], konum[isim2])
                komsuluk[isim1].append((isim2, agirlik))
                komsuluk[isim2].append((isim1, agirlik))

        
        HUCRE_DERECE = 0.001  
        izgara: Dict[Tuple[int, int], List[str]] = {}
        for isim, (enlem, boylam) in konum.items():
            hucre = (int(enlem / HUCRE_DERECE), int(boylam / HUCRE_DERECE))
            izgara.setdefault(hucre, []).append(isim)

        kontrol_edilmis: set = set()
        for (cx, cy), bu_hucredekiler in izgara.items():
            komsu_adaylari: List[str] = []
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    komsu_adaylari.extend(izgara.get((cx + dx, cy + dy), []))
            for isim1 in bu_hucredekiler:
                way1 = cls._osm_way_ve_indeks_ayikla(isim1)
                way1_id = way1[0] if way1 else None
                for isim2 in komsu_adaylari:
                    if isim1 >= isim2:
                        continue
                    cift = (isim1, isim2)
                    if cift in kontrol_edilmis:
                        continue
                    kontrol_edilmis.add(cift)
                    way2 = cls._osm_way_ve_indeks_ayikla(isim2)
                    way2_id = way2[0] if way2 else None
                    if way1_id is not None and way1_id == way2_id:
                        continue  
                    agirlik_km = cls._geodesic_km(konum[isim1], konum[isim2])
                    if agirlik_km * 1000.0 <= cls._KESISIM_TOLERANSI_METRE:
                        komsuluk[isim1].append((isim2, agirlik_km))
                        komsuluk[isim2].append((isim1, agirlik_km))

        return komsuluk, konum

    @staticmethod
    def _dijkstra_en_kisa_mesafeler(
        komsuluk: Dict[str, List[Tuple[str, float]]], kaynak_isim: str
    ) -> Tuple[Dict[str, float], Dict[str, str]]:
       
        mesafeler: Dict[str, float] = {kaynak_isim: 0.0}
        onceki: Dict[str, str] = {}
        pq: List[Tuple[float, str]] = [(0.0, kaynak_isim)]
        ziyaret_edildi: set = set()
        while pq:
            mesafe, dugum = heapq.heappop(pq)
            if dugum in ziyaret_edildi:
                continue
            ziyaret_edildi.add(dugum)
            for komsu, agirlik in komsuluk.get(dugum, []):
                yeni_mesafe = mesafe + agirlik
                if yeni_mesafe < mesafeler.get(komsu, float("inf")):
                    mesafeler[komsu] = yeni_mesafe
                    onceki[komsu] = dugum
                    heapq.heappush(pq, (yeni_mesafe, komsu))
        return mesafeler, onceki

    @staticmethod
    def _yolu_yeniden_insa_et(
        onceki: Dict[str, str], konum: Dict[str, Tuple[float, float]], kaynak_isim: str, hedef_isim: str
    ) -> List[Tuple[float, float]]:
       
        if hedef_isim == kaynak_isim:
            return [konum[kaynak_isim]] if kaynak_isim in konum else []
        yol: List[Tuple[float, float]] = []
        dugum = hedef_isim
        ziyaret_edildi: set = set()
        while dugum != kaynak_isim:
            if dugum in ziyaret_edildi or dugum not in konum:
                return [] 
            ziyaret_edildi.add(dugum)
            yol.append(konum[dugum])
            onceki_dugum = onceki.get(dugum)
            if onceki_dugum is None:
                return []
            dugum = onceki_dugum
        yol.append(konum[kaynak_isim])
        yol.reverse()
        return yol

    def _en_yakin_ulasilabilir_noktayi_bul(
        self,
        baslangic_konum: Tuple[float, float],
        mesafeler: Dict[str, float],
        konum: Dict[str, Tuple[float, float]],
        maks_mesafe_km: float = _KANCA_MAKS_MESAFE_KM,
    ) -> Optional[Tuple[str, float]]:
        
        en_yakin_isim: Optional[str] = None
        en_yakin_km = float("inf")
        for isim in mesafeler:
            k = konum.get(isim)
            if k is None:
                continue
            m = self._geodesic_km(baslangic_konum, k)
            if m < en_yakin_km:
                en_yakin_km, en_yakin_isim = m, isim
        if en_yakin_isim is None or en_yakin_km > maks_mesafe_km:
            return None
        return en_yakin_isim, en_yakin_km

    _YOL_AGI_ONBELLEK_SANIYE = 30.0


    def _yerel_yol_agi_ve_kaynak_getir(
        self, event_enlem: float, event_boylam: float, yol_agi_yaricapi_km: float
    ) -> Optional[
        Tuple[
            Dict[str, List[Tuple[str, float]]],
            Dict[str, Tuple[float, float]],
            Dict[str, float],
            Dict[str, str],
            str,
        ]
    ]:
        
        anahtar = (round(event_enlem, 4), round(event_boylam, 4), yol_agi_yaricapi_km)
        simdi = time.monotonic()
        onbellek_zamani = self._yol_agi_onbellek_zamani.get(anahtar)
        if onbellek_zamani is not None and (simdi - onbellek_zamani) < self._YOL_AGI_ONBELLEK_SANIYE:
            return self._yol_agi_onbellek[anahtar]

        yol_noktalari = self._yerel_acik_yol_agini_getir(event_enlem, event_boylam, yol_agi_yaricapi_km)
        if not yol_noktalari:
            sonuc = None
        else:
            komsuluk, konum = self._yol_agi_kur(yol_noktalari)
            kaynak_isim = min(
                konum, key=lambda isim: self._geodesic_km((event_enlem, event_boylam), konum[isim])
            )
            mesafeler, onceki = self._dijkstra_en_kisa_mesafeler(komsuluk, kaynak_isim)
            sonuc = (komsuluk, konum, mesafeler, onceki, kaynak_isim)

        self._yol_agi_onbellek[anahtar] = sonuc
        self._yol_agi_onbellek_zamani[anahtar] = simdi
        return sonuc

    def en_yakin_ulasilan_varliklari_bul(
        self,
        event_enlem: float,
        event_boylam: float,
        hedef_label: str,
        ilk_n: int = 3,
        yol_agi_yaricapi_km: float = _YOL_AGI_ARAMA_YARICAPI_KM,
        ekstra_where: str = "",
        ekstra_parametreler: Optional[Dict[str, Any]] = None,
        unit_tipleri: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
    
        yol_agi = self._yerel_yol_agi_ve_kaynak_getir(event_enlem, event_boylam, yol_agi_yaricapi_km)
        if yol_agi is None:
            logger.warning(
                "en_yakin_ulasilan_varliklari_bul: (%.4f, %.4f) cevresinde %.0f km icinde "
                "ACIK ana damar noktasi bulunamadi; erisilebilirlik hesaplanamiyor.",
                event_enlem, event_boylam, yol_agi_yaricapi_km,
            )
            return []
        komsuluk, konum, mesafeler, onceki, kaynak_isim = yol_agi

        parametreler: Dict[str, Any] = dict(ekstra_parametreler or {})
        parametreler.update({
            "enlem": event_enlem, "boylam": event_boylam,
            "yaricap_metre": yol_agi_yaricapi_km * 1000.0,
        })
        tip_kosulu = ""
        if unit_tipleri:
            tip_kosulu = " AND h.unit_type IN $unit_tipleri"
            parametreler["unit_tipleri"] = list(unit_tipleri)
       
        aday_limiti = max(ilk_n * self._HEDEF_ADAY_COKLUGU_KATSAYISI, self._HEDEF_ADAY_MIN_LIMITI)
        parametreler["aday_limiti"] = aday_limiti
    
        hedefler = self.execute_query(
            f"MATCH (h:{hedef_label}) WHERE h.konum IS NOT NULL "
            "AND point.distance(h.konum, point({latitude: $enlem, longitude: $boylam, crs: 'wgs-84'})) "
            "<= $yaricap_metre" + tip_kosulu + (f" {ekstra_where}" if ekstra_where else "") + " "
            "WITH h, point.distance(h.konum, point({latitude: $enlem, longitude: $boylam, crs: 'wgs-84'})) "
            "AS duz_hat_mesafe_metre "
            "RETURN h ORDER BY duz_hat_mesafe_metre ASC LIMIT $aday_limiti",
            parametreler,
            write=False,
        )

        sonuclar: List[Dict[str, Any]] = []
        for row in hedefler:
            hedef = dict(row["h"])
            h_enlem, h_boylam = hedef.get("enlem"), hedef.get("boylam")
            if h_enlem is None or h_boylam is None:
                continue
            en_yakin_yol_noktasi, en_yakin_snap_km = None, float("inf")
            for yol_isim, yol_konum in konum.items():
                m = self._geodesic_km((h_enlem, h_boylam), yol_konum)
                if m < en_yakin_snap_km:
                    en_yakin_snap_km, en_yakin_yol_noktasi = m, yol_isim
            if en_yakin_yol_noktasi is None or en_yakin_snap_km > self._HEDEF_SNAP_YARICAPI_KM:
                continue  

            i
                kanca = self._en_yakin_ulasilabilir_noktayi_bul(konum[en_yakin_yol_noktasi], mesafeler, konum)
                if kanca is None:
           
                    logger.info(
                        "'%s' olay yerinden ACIK yol agiyla ERISILEMEZ (kapali yol(lar) izole "
                        "ediyor; %.1f km'lik kanca toleransi icinde de baglanti bulunamadi).",
                        hedef.get("isim"), self._KANCA_MAKS_MESAFE_KM,
                    )
                    continue
                kanca_isim, kanca_km = kanca
                logger.info(
                    "'%s' icin KANCA uygulandi: '%s' heuristik olarak izoleydi, '%s'e %.0f m'lik "
                    "bir baglantiyla (ayni yolun bolunmus parcasi varsayimiyla) baglandi.",
                    hedef.get("isim"), en_yakin_yol_noktasi, kanca_isim, kanca_km * 1000,
                )
                hedef["mesafe_km"] = round(mesafeler[kanca_isim] + kanca_km + en_yakin_snap_km, 1)
                hedef["erisilebilir"] = True
                hedef["kanca_uygulandi"] = True
                rota = self._yolu_yeniden_insa_et(onceki, konum, kaynak_isim, kanca_isim)
                if rota:
                    rota = rota + [konum[en_yakin_yol_noktasi], (h_enlem, h_boylam)]
                hedef["rota_noktalari"] = rota
                sonuclar.append(hedef)
                continue

            hedef["mesafe_km"] = round(mesafeler[en_yakin_yol_noktasi] + en_yakin_snap_km, 1)
            hedef["erisilebilir"] = True
            hedef["kanca_uygulandi"] = False
            
            rota = self._yolu_yeniden_insa_et(onceki, konum, kaynak_isim, en_yakin_yol_noktasi)
            if rota:
                rota = rota + [(h_enlem, h_boylam)]
            hedef["rota_noktalari"] = rota
            sonuclar.append(hedef)

        sonuclar.sort(key=lambda s: s["mesafe_km"])
        return sonuclar[:ilk_n]
    def add_node(self, node: BaseNode) -> Dict[str, Any]:
      
        canonical_isim = self._resolve_canonical_isim(node.isim, label=node.neo4j_label.value)
        query, parameters = self.node_to_cypher(node, merge_isim=canonical_isim)
        result = self.execute_query(query, parameters, write=True)
        return result[0] if result else {}

    def add_nodes(self, nodes: Sequence[BaseNode]) -> None:
        """Birden fazla düğümü tek tek MERGE eder."""
        for node in nodes:
            self.add_node(node)

    def update_infrastructure_status_by_name(
        self, aranan_isim: str, guncellemeler: Dict[str, Any], label: str = "Infrastructure"
    ) -> Tuple[int, Optional[str]]:
       
        hedef_anahtar = _altyapi_adi_anahtari(aranan_isim)
        if not hedef_anahtar:
            return 0, None

    
        tam_esitlik_sonuc = self.execute_query(
            f"MATCH (n:{label} {{aciklama: $ad}}) RETURN n.aciklama AS ad LIMIT 1",
            {"ad": aranan_isim},
            write=False,
        )
        if tam_esitlik_sonuc:
            return self._infrastructure_status_guncelle_ve_temsili_isim_dondur(
                label, tam_esitlik_sonuc[0]["ad"], guncellemeler
            )

        adaylar = self.execute_query(
            f"MATCH (n:{label}) WHERE n.aciklama IS NOT NULL RETURN DISTINCT n.aciklama AS ad",
            write=False,
        )
        if not adaylar:
            return 0, None

        
        eslesen_ad: Optional[str] = None
        for row in adaylar:
            if _altyapi_adi_anahtari(row["ad"]) == hedef_anahtar:
                eslesen_ad = row["ad"]
                break

        
        if eslesen_ad is None:
            en_iyi_oran = 0.0
            en_iyi_aday: Optional[str] = None
            for row in adaylar:
                aday_ad = row["ad"]
                oran = difflib.SequenceMatcher(None, hedef_anahtar, _altyapi_adi_anahtari(aday_ad)).ratio()
                if oran > en_iyi_oran:
                    en_iyi_oran = oran
                    en_iyi_aday = aday_ad
            if en_iyi_oran < FUZZY_ALTYAPI_ESIK:
                logger.info(
                    "Altyapi eslesmesi bulunamadi: '%s' (en yakin aday='%s', oran=%.2f, esik=%.2f)",
                    aranan_isim, en_iyi_aday, en_iyi_oran, FUZZY_ALTYAPI_ESIK,
                )
                return 0, None
            eslesen_ad = en_iyi_aday

        logger.info("Altyapi eslesmesi bulundu: '%s' -> '%s' (GERCEK OSM adi)", aranan_isim, eslesen_ad)
        return self._infrastructure_status_guncelle_ve_temsili_isim_dondur(label, eslesen_ad, guncellemeler)

    def _infrastructure_status_guncelle_ve_temsili_isim_dondur(
        self, label: str, eslesen_ad: str, guncellemeler: Dict[str, Any]
    ) -> Tuple[int, Optional[str]]:
       
        sonuc = self.execute_query(
            f"MATCH (n:{label} {{aciklama: $ad}}) SET n += $props RETURN count(n) AS adet",
            {"ad": eslesen_ad, "props": guncellemeler},
            write=True,
        )
        guncellenen = sonuc[0]["adet"] if sonuc else 0
        if not guncellenen:
            return 0, None

        temsili_sonuc = self.execute_query(
            f"MATCH (n:{label} {{aciklama: $ad}}) RETURN n.isim AS isim LIMIT 1",
            {"ad": eslesen_ad},
            write=False,
        )
        temsili_isim = temsili_sonuc[0]["isim"] if temsili_sonuc else None
        return guncellenen, temsili_isim

    def get_node(
        self, node_id: str, label: Optional[NodeLabel] = None
    ) -> Optional[Dict[str, Any]]:
        label_clause = f":{label.value}" if label else ""
        query = f"MATCH (n{label_clause} {{id: $id}}) RETURN n"
        result = self.execute_query(query, {"id": node_id}, write=False)
        return result[0]["n"] if result else None

    def find_nodes(
        self, label: NodeLabel, filters: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        filters = filters or {}
        where_clause = ""
        if filters:
            conditions = " AND ".join(f"n.{key} = ${key}" for key in filters)
            where_clause = f" WHERE {conditions}"
        query = f"MATCH (n:{label.value}){where_clause} RETURN n"
        result = self.execute_query(query, filters, write=False)
        return [record["n"] for record in result]

    def delete_node(self, node_id: str, detach: bool = True) -> None:
        detach_clause = "DETACH " if detach else ""
        query = f"MATCH (n {{id: $id}}) {detach_clause}DELETE n"
        self.execute_query(query, {"id": node_id}, write=True)


    def add_relationship(self, relationship: Relationship) -> Dict[str, Any]:
       
        kaynak_isim = self._resolve_canonical_isim(relationship.kaynak_id)
        hedef_isim = self._resolve_canonical_isim(relationship.hedef_id)
        query, parameters = self.relationship_to_cypher(
            relationship, kaynak_isim=kaynak_isim, hedef_isim=hedef_isim
        )
        result = self.execute_query(query, parameters, write=True)
        return result[0] if result else {}

    def add_relationships(self, relationships: Sequence[Relationship]) -> None:
        for relationship in relationships:
            self.add_relationship(relationship)

    def delete_relationship(
        self, kaynak_id: str, hedef_id: str, tip: RelationshipType
    ) -> None:
        """Belirli tip ve uç noktalara sahip ilişkiyi siler."""
        query = (
            "MATCH (a {id: $kaynak_id})-[r:" + tip.value + "]->(b {id: $hedef_id}) "
            "DELETE r"
        )
        self.execute_query(
            query, {"kaynak_id": kaynak_id, "hedef_id": hedef_id}, write=True
        )


    def ensure_constraints(self) -> None:
   
        for label in NODE_REGISTRY:
            id_constraint_name = f"unique_{label.value.lower()}_id"
            isim_constraint_name = f"unique_{label.value.lower()}_isim"
            self.execute_query(
                f"CREATE CONSTRAINT {id_constraint_name} IF NOT EXISTS "
                f"FOR (n:{label.value}) REQUIRE n.id IS UNIQUE",
                write=True,
            )
            self.execute_query(
                f"CREATE CONSTRAINT {isim_constraint_name} IF NOT EXISTS "
                f"FOR (n:{label.value}) REQUIRE n.isim IS UNIQUE",
                write=True,
            )
        logger.info("Neo4j kisitlari (constraints) dogrulandi/olusturuldu.")
        self.ensure_spatial_indexes()
        self.ensure_property_indexes()

    def ensure_spatial_indexes(self) -> None:
       
        for label in ("Infrastructure", "Facility", "Unit"):
            index_adi = f"spatial_{label.lower()}_konum"
            self.execute_query(
                f"CREATE POINT INDEX {index_adi} IF NOT EXISTS "
                f"FOR (n:{label}) ON (n.konum)",
                write=True,
            )
        logger.info("Neo4j mekansal (POINT) indeksleri dogrulandi/olusturuldu.")

    
    def ensure_property_indexes(self) -> None:
        
        alan_bazinda_indeksler = (
            ("Infrastructure", "acik_mi"),
            ("Facility", "mevcut_durum"),
            ("Event", "siddet"),
            ("Unit", "durum"),
            ("Infrastructure", "aciklama"),
            ("Facility", "aciklama"),
            ("Unit", "aciklama"),
        )
        for label, alan in alan_bazinda_indeksler:
            index_adi = f"prop_{label.lower()}_{alan.lower()}"
            self.execute_query(
                f"CREATE INDEX {index_adi} IF NOT EXISTS FOR (n:{label}) ON (n.{alan})",
                write=True,
            )
        logger.info(
            "Neo4j property indeksleri dogrulandi/olusturuldu (%s).",
            ", ".join(f"{label}.{alan}" for label, alan in alan_bazinda_indeksler),
        )

  
    _GUVENLI_BATCH_BOYUTU = 50_000
   
    def batched_bulk_write(
        self,
        match_where_cypher: str,
        action_cypher: str,
        params: Optional[Dict[str, Any]] = None,
        batch_size: int = _GUVENLI_BATCH_BOYUTU,
        islem_adi: str = "toplu islem",
    ) -> int:
        
        params = dict(params or {})
        toplam = 0
        while True:
            sonuc = self.execute_query(
                f"{match_where_cypher} WITH n LIMIT $batch_size {action_cypher} RETURN count(n) AS adet",
                {**params, "batch_size": batch_size},
                write=True,
            )
            islenen = sonuc[0]["adet"] if sonuc else 0
            toplam += islenen
            if islenen == 0:
                break
            logger.info("%s: %d düğüm işlendi (toplam: %d)...", islem_adi, islenen, toplam)
        return toplam

    def clear_database(self, batch_boyutu: int = _GUVENLI_BATCH_BOYUTU) -> None:
       
        toplam_silinen = self.batched_bulk_write(
            "MATCH (n)", "DETACH DELETE n", batch_size=batch_boyutu, islem_adi="clear_database",
        )
        logger.warning("Neo4j veritabanindaki tum veriler silindi (%d dugum).", toplam_silinen)

    def reset_crisis_scenario(self, bolge: Optional[str] = None) -> Dict[str, int]:
      
        bolge_kosulu_e = " {bolge: $bolge}" if bolge else ""
        silinen_olay = self.batched_bulk_write(
            f"MATCH (n:Event{bolge_kosulu_e})",
            "DETACH DELETE n",
            params={"bolge": bolge},
            islem_adi="reset_crisis_scenario (Event silme)",
        )

       
        bolge_kosulu_i = " AND n.bolge = $bolge" if bolge else ""
        acilan_altyapi = self.batched_bulk_write(
            f"MATCH (n:Infrastructure) WHERE (n.acik_mi <> true OR n.acik_mi IS NULL){bolge_kosulu_i}",
            "SET n.acik_mi = true",
            params={"bolge": bolge},
            islem_adi="reset_crisis_scenario (Infrastructure acma)",
        )

        bolge_kosulu_f = " AND n.bolge = $bolge" if bolge else ""
        iyilesen_tesis = self.batched_bulk_write(
            f"MATCH (n:Facility) WHERE (n.mevcut_durum <> 'Aktif' OR n.mevcut_durum IS NULL "
            f"OR n.durum <> 'Aktif' OR n.durum IS NULL){bolge_kosulu_f}",
            "SET n.mevcut_durum = 'Aktif', n.durum = 'Aktif'",
            params={"bolge": bolge},
            islem_adi="reset_crisis_scenario (Facility iyilestirme)",
        )
        iyilesen_birim = self.batched_bulk_write(
            f"MATCH (n:Unit) WHERE (n.durum <> 'Aktif' OR n.durum IS NULL){bolge_kosulu_f}",
            "SET n.durum = 'Aktif'",
            params={"bolge": bolge},
            islem_adi="reset_crisis_scenario (Unit iyilestirme)",
        )

        logger.warning(
            "Kriz senaryosu sifirlandi (bolge=%s): %d Event dugumu silindi, %d Infrastructure "
            "dugumu tekrar acik_mi=True yapildi, %d Facility + %d Unit dugumu tekrar Aktif "
            "yapildi (gercek sehir topolojisi KORUNDU).",
            bolge or "TUMU",
            silinen_olay,
            acilan_altyapi,
            iyilesen_tesis,
            iyilesen_birim,
        )
        return {
            "silinen_olay": silinen_olay,
            "acilan_altyapi": acilan_altyapi,
            "iyilesen_tesis": iyilesen_tesis,
            "iyilesen_birim": iyilesen_birim,
        }


def get_db() -> Neo4jConnection:
    """`Neo4jConnection` singleton örneğine kısayol erişim fonksiyonu."""
    return Neo4jConnection()
