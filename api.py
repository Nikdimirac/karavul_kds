
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from src.core.database import Neo4jConnection, Neo4jConnectionError
from src.core.decision_engine import (
    _AKTIF_BIRLIK_LISTELEME_LIMITI,
    _isim_bazinda_tekillestir,
    DecisionEngine,
    HASARLI_TESIS_DURUMLARI,
    KRITIK_OLAY_SIDDETLERI,
    parse_oneri_maddeleri,
)
from src.data_ingestion.nlp_parser import OllamaParser
from src.data_ingestion.real_osm_loader import OSM_TEKNIK_KIMLIK_IMZASI
from src.ui.app import _grafa_yaz  

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
logger = logging.getLogger(__name__)

_YERLESIM_LISTELEME_LIMITI = 2000


_TUM_TESIS_LISTELEME_LIMITI = 5000




@lru_cache()
def get_connection() -> Neo4jConnection:
    return Neo4jConnection()


@lru_cache()
def get_parser() -> OllamaParser:
   
    return OllamaParser(model=os.getenv("OLLAMA_EXTRACTION_MODEL", "llama3"))


@lru_cache()
def get_engine() -> DecisionEngine:
  
    return DecisionEngine(get_connection(), model=os.getenv("OLLAMA_TACTICAL_MODEL", "karavul-kurmay-qwen"))


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
  
    db = get_connection()
    try:
        db.connect()
        db.ensure_constraints()
    except Neo4jConnectionError as exc:
        logger.error("CRITICAL API ERROR (başlangıç): Neo4j'e bağlanılamadı: %s", exc, exc_info=True)
        print(f"CRITICAL API ERROR (başlangıç): Neo4j'e bağlanılamadı: {exc}")
        raise
    logger.info("API başlatıldı: Neo4j bağlantısı ve index'ler hazır.")
    yield
    db.close()
    logger.info("API kapatıldı: Neo4j bağlantısı temiz biçimde sonlandırıldı.")


app = FastAPI(
    title="Kriz ve Afet Yönetimi Karar Destek Sistemi — API",
    description=(
        "Kriz motorunun (GraphRAG Karar Destek Motoru) HTTP üzerinden erişilebilir "
        "REST arayüzü. Frontend İÇERMEZ — SADECE /docs üzerinden test edilir."
    ),
    version="0.5.0",
    lifespan=lifespan,
)


_GELISTIRME_ORIJINLERI = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",  
    "http://127.0.0.1:4173",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_GELISTIRME_ORIJINLERI,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)





def _beklenmeyen_hatayi_logla_ve_yukselt(baglam: str, exc: Exception) -> HTTPException:
    logger.error("CRITICAL API ERROR (%s, BEKLENMEYEN tip): %s", baglam, exc, exc_info=True)
    print(f"CRITICAL API ERROR ({baglam}, BEKLENMEYEN tip): {type(exc).__name__}: {exc}")
    return HTTPException(status_code=500, detail=f"Beklenmeyen hata ({type(exc).__name__}): {exc}")




class KrizRaporuIstek(BaseModel):
    rapor_metni: str = Field(
        ..., min_length=1,
        description="Serbest metin kriz raporu / telsiz konuşması (Ollama ile analiz edilip Bilgi Grafına işlenir).",
        examples=["Kahramanmaraş merkezli 7.8 büyüklüğünde bir deprem meydana geldi. Nurdağı Viyadüğü çöktü."],
    )


class KrizRaporuYaniti(BaseModel):
    yazilan_varlik_sayisi: int = Field(description="Bilgi Grafına yazılan YENİ düğüm sayısı.")
    yazilan_iliski_sayisi: int = Field(description="Kurulan ilişki sayısı.")
    guncellenen_altyapi_sayisi: int = Field(description="Durumu güncellenen GERÇEK OSM altyapı düğümü sayısı (ör. kapatılan bir köprü).")
    engellenen_ghost_sayisi: int = Field(description="Gerçek bir OSM eşleşmesi bulunamadığı için REDDEDİLEN (hayali düğüm açılmayan) kayıt sayısı.")
    engellenen_sahte_koordinat_sayisi: int = Field(description="Gerçek yüklü bölgenin dışına düştüğü için REDDEDİLEN kayıt sayısı.")
    dogrulama_hatalari: List[str] = Field(default_factory=list, description="Pydantic doğrulamasından geçemeyip ATLANAN kayıtların açıklamaları.")
    durum_ozeti: str = Field(default="", description="Karar motorunun değerlendirmeye esas aldığı GraphRAG bağlamı (ham metin).")
    taktiksel_oneriler: List[str] = Field(default_factory=list, description="3 maddelik taktiksel karar/yönlendirme önerisi.")
    uyari: Optional[str] = Field(default=None, description="Rapor Bilgi Grafına işlendi ama taktiksel öneri üretilemediyse buraya düşer (rapor kaybolmaz, SADECE öneri adımı atlanır).")


@app.post("/api/analyze-crisis", response_model=KrizRaporuYaniti, tags=["Kriz Analizi"])
def analyze_crisis(
    istek: KrizRaporuIstek,
    db: Neo4jConnection = Depends(get_connection),
    parser: OllamaParser = Depends(get_parser),
    engine: DecisionEngine = Depends(get_engine),
) -> KrizRaporuYaniti:
    
    try:
        sonuc = parser.extract_entities(istek.rapor_metni)
    except (RuntimeError, ValueError) as exc:
        logger.error("CRITICAL API ERROR (Ollama ayrıştırma): %s", exc, exc_info=True)
        print(f"CRITICAL API ERROR (Ollama ayrıştırma): {exc}")
        raise HTTPException(status_code=503, detail=f"Ollama servisiyle iletişim kurulamadı: {exc}") from exc
    except Exception as exc: 
        raise _beklenmeyen_hatayi_logla_ve_yukselt("Ollama ayrıştırma", exc) from exc

    try:
        (
            yazilan_dugum,
            yazilan_iliski,
            altyapi_guncellemeleri,
            engellenen_ghost,
            engellenen_sahte_koordinat,
            _yer_adiyla_baglanan,
            _harici_geokodlama_ile_baglanan,
            yazilan_event_koordinatlari,
        ) = _grafa_yaz(db, sonuc)
    except (RuntimeError, ValueError, Neo4jConnectionError) as exc:
        logger.error("CRITICAL API ERROR (Bilgi Grafı yazımı): %s", exc, exc_info=True)
        print(f"CRITICAL API ERROR (Bilgi Grafı yazımı): {exc}")
        raise HTTPException(status_code=503, detail=f"Bilgi Grafına yazılırken hata oluştu: {exc}") from exc
    except Exception as exc:  
        raise _beklenmeyen_hatayi_logla_ve_yukselt("Bilgi Grafı yazımı", exc) from exc

    dogrulama_hatalari = sonuc.get("validation_hatalari") or []

    if yazilan_dugum == 0 and yazilan_iliski == 0 and altyapi_guncellemeleri == 0:
        raise HTTPException(
            status_code=422,
            detail=(
                "Modelden Bilgi Grafına yazılabilecek hiçbir geçerli varlık/ilişki "
                "çıkarılamadı." + (f" Doğrulama hataları: {dogrulama_hatalari}" if dogrulama_hatalari else "")
            ),
        )

    durum_ozeti = ""
    taktiksel_oneriler: List[str] = []
    uyari: Optional[str] = None
    try:
       
        ai_sonuc = engine.generate_recommendations(
            odak_koordinatlari=yazilan_event_koordinatlari or None, paralel_calistir=True
        )
        durum_ozeti = ai_sonuc.get("durum_ozeti") or ""
        taktiksel_oneriler = parse_oneri_maddeleri(ai_sonuc.get("oneriler_metni") or "")
        
        if ai_sonuc.get("cografi_red"):
            uyari = f"🚫 {ai_sonuc.get('cografi_red_nedeni') or 'Coğrafi tutarsızlık tespit edildi.'}"
    except Exception as exc:
        logger.error("CRITICAL API ERROR (taktiksel öneri üretimi): %s", exc, exc_info=True)
        print(f"CRITICAL API ERROR (taktiksel öneri üretimi): {type(exc).__name__}: {exc}")
        uyari = f"Rapor Bilgi Grafına işlendi ANCAK taktiksel öneri üretilemedi ({type(exc).__name__}): {exc}"

    return KrizRaporuYaniti(
        yazilan_varlik_sayisi=yazilan_dugum,
        yazilan_iliski_sayisi=yazilan_iliski,
        guncellenen_altyapi_sayisi=altyapi_guncellemeleri,
        engellenen_ghost_sayisi=engellenen_ghost,
        engellenen_sahte_koordinat_sayisi=engellenen_sahte_koordinat,
        dogrulama_hatalari=dogrulama_hatalari,
        durum_ozeti=durum_ozeti,
        taktiksel_oneriler=taktiksel_oneriler,
        uyari=uyari,
    )




class TesisDurumu(BaseModel):
    id: Optional[str] = None
    isim: str
    tip: Optional[str] = None
    durum: Optional[str] = None
    enlem: float
    boylam: float
    kapasite: Optional[float] = None


class AltyapiDurumu(BaseModel):
    isim: str
    tip: Optional[str] = None
    enlem: float
    boylam: float
    uzunluk_km: Optional[float] = None
   
    bitis_enlem: Optional[float] = None
    bitis_boylam: Optional[float] = None


class BirlikDurumu(BaseModel):
    isim: str
    tip: Optional[str] = None
    personel: Optional[int] = None
    hareket_kabiliyeti: Optional[str] = None
    enlem: float
    boylam: float
    durum: Optional[str] = None


class OlayDurumu(BaseModel):
    isim: str
    tip: Optional[str] = None
    siddet: Optional[str] = None
    etki_alani_km: Optional[float] = None
    enlem: float
    boylam: float
    zaman: Optional[str] = None


class YerlesimDurumu(BaseModel):
    isim: str
    il: Optional[str] = None
    yerlesim_tipi: Optional[str] = None
    nufus: Optional[int] = None
    enlem: float
    boylam: float


class AltyapiDurumYaniti(BaseModel):
    hasarli_tesisler: List[TesisDurumu]
    
    tum_tesisler: List[TesisDurumu]
    kapali_yollar: List[AltyapiDurumu]
    aktif_birlikler: List[BirlikDurumu]
    kritik_olaylar: List[OlayDurumu]
   
    yerlesimler: List[YerlesimDurumu]


@app.get("/api/infrastructure/status", response_model=AltyapiDurumYaniti, tags=["Durum"])
def infrastructure_status(db: Neo4jConnection = Depends(get_connection)) -> AltyapiDurumYaniti:
   
    try:
        hasarli_tesisler = db.execute_query(
            "MATCH (f:Facility) WHERE f.mevcut_durum IN $durumlar "
            "WITH f, COALESCE(f.aciklama, f.isim) AS isim "
            "WHERE NOT isim CONTAINS $osm_imza "
            "RETURN f.id AS id, isim, f.facility_type AS tip, f.mevcut_durum AS durum, "
            "f.enlem AS enlem, f.boylam AS boylam, f.kapasite AS kapasite "
            "ORDER BY f.guncelleme_tarihi DESC",
            {"durumlar": HASARLI_TESIS_DURUMLARI, "osm_imza": OSM_TEKNIK_KIMLIK_IMZASI},
        )
        tum_tesisler = db.execute_query(
            "MATCH (f:Facility) "
            "WITH f, COALESCE(f.aciklama, f.isim) AS isim "
            "WHERE NOT isim CONTAINS $osm_imza "
            "RETURN f.id AS id, isim, f.facility_type AS tip, f.mevcut_durum AS durum, "
            "f.enlem AS enlem, f.boylam AS boylam, f.kapasite AS kapasite "
            "LIMIT $limit",
            {"osm_imza": OSM_TEKNIK_KIMLIK_IMZASI, "limit": _TUM_TESIS_LISTELEME_LIMITI},
        )
        kapali_yollar_ham = db.execute_query(
            "MATCH (i:Infrastructure) WHERE i.acik_mi = false "
            "WITH COALESCE(i.aciklama, i.isim) AS isim, i "
            "WHERE NOT isim CONTAINS $osm_imza "
            "RETURN isim, i.infrastructure_type AS tip, i.enlem AS enlem, i.boylam AS boylam, "
            "i.uzunluk_km AS uzunluk_km, i.bitis_enlem AS bitis_enlem, i.bitis_boylam AS bitis_boylam "
            "ORDER BY i.guncelleme_tarihi DESC",
            {"osm_imza": OSM_TEKNIK_KIMLIK_IMZASI},
        )
        aktif_birlikler = db.execute_query(
            "MATCH (u:Unit) "
            "RETURN COALESCE(u.aciklama, u.isim) AS isim, u.unit_type AS tip, "
            "u.personel_sayisi AS personel, u.hareket_kabiliyeti AS hareket_kabiliyeti, "
            "u.enlem AS enlem, u.boylam AS boylam, u.durum AS durum "
            "LIMIT $limit",
            {"limit": _AKTIF_BIRLIK_LISTELEME_LIMITI},
        )
        kritik_olaylar = db.execute_query(
            "MATCH (e:Event) WHERE e.siddet IN $siddetler "
            "RETURN e.isim AS isim, e.event_type AS tip, e.siddet AS siddet, "
            "e.etki_alani_km AS etki_alani_km, e.enlem AS enlem, e.boylam AS boylam, "
            "e.zaman_damgasi AS zaman "
            "ORDER BY e.zaman_damgasi DESC",
            {"siddetler": KRITIK_OLAY_SIDDETLERI},
        )
        yerlesimler = db.execute_query(
            "MATCH (s:Settlement) "
            "RETURN s.isim AS isim, s.il AS il, s.yerlesim_tipi AS yerlesim_tipi, "
            "s.nufus AS nufus, s.enlem AS enlem, s.boylam AS boylam "
            "LIMIT $limit",
            {"limit": _YERLESIM_LISTELEME_LIMITI},
        )
    except Neo4jConnectionError as exc:
        logger.error("CRITICAL API ERROR (altyapı durumu sorgusu): %s", exc, exc_info=True)
        print(f"CRITICAL API ERROR (altyapı durumu sorgusu): {exc}")
        raise HTTPException(status_code=503, detail=f"Neo4j sorgusu başarısız oldu: {exc}") from exc
    except Exception as exc:  
        raise _beklenmeyen_hatayi_logla_ve_yukselt("altyapı durumu sorgusu", exc) from exc

    return AltyapiDurumYaniti(
        hasarli_tesisler=hasarli_tesisler,
        tum_tesisler=tum_tesisler,
        kapali_yollar=_isim_bazinda_tekillestir(kapali_yollar_ham),
        aktif_birlikler=aktif_birlikler,
        kritik_olaylar=kritik_olaylar,
        yerlesimler=yerlesimler,
    )




class SifirlamaYaniti(BaseModel):
    silinen_olay: int
    acilan_altyapi: int
    iyilesen_tesis: int
    iyilesen_birim: int


@app.post("/api/reset-scenario", response_model=SifirlamaYaniti, tags=["Durum"])
def reset_scenario(db: Neo4jConnection = Depends(get_connection)) -> SifirlamaYaniti:
  
    try:
        sonuc = db.reset_crisis_scenario()
    except Neo4jConnectionError as exc:
        logger.error("CRITICAL API ERROR (senaryo sıfırlama): %s", exc, exc_info=True)
        print(f"CRITICAL API ERROR (senaryo sıfırlama): {exc}")
        raise HTTPException(status_code=503, detail=f"Senaryo sıfırlanamadı: {exc}") from exc
    except Exception as exc: 
        raise _beklenmeyen_hatayi_logla_ve_yukselt("senaryo sıfırlama", exc) from exc

    return SifirlamaYaniti(**sonuc)





class OlayCozumYaniti(BaseModel):
    olay_ismi: str = Field(description="Kapatılan (silinen) olayın ismi.")
    silindi: bool = Field(description="Bu isimde bir olay bulunup silindiyse True.")
    acilan_altyapi_sayisi: int = Field(
        default=0, description="Bu olaya bağlı (AFFECTS/LOCATED_IN), acik_mi=True yapılan Infrastructure düğümü sayısı."
    )
    iyilesen_tesis_sayisi: int = Field(
        default=0, description="Bu olaya bağlı, mevcut_durum/durum'u 'Aktif'e döndürülen Facility düğümü sayısı."
    )


@app.post("/api/resolve-event/{olay_ismi}", response_model=OlayCozumYaniti, tags=["Durum"])
def resolve_event(olay_ismi: str, db: Neo4jConnection = Depends(get_connection)) -> OlayCozumYaniti:
    
    try:
        acilan_altyapi = db.execute_query(
            "MATCH (:Event {isim: $isim})-[:AFFECTS|LOCATED_IN]->(i:Infrastructure) "
            "SET i.acik_mi = true "
            "RETURN count(i) AS adet",
            {"isim": olay_ismi},
            write=True,
        )
        iyilesen_tesis = db.execute_query(
            "MATCH (:Event {isim: $isim})-[:AFFECTS|LOCATED_IN]->(f:Facility) "
            "SET f.mevcut_durum = 'Aktif', f.durum = 'Aktif' "
            "RETURN count(f) AS adet",
            {"isim": olay_ismi},
            write=True,
        )
        silme_sonucu = db.execute_query(
            "MATCH (e:Event {isim: $isim}) "
            "WITH e, count(e) AS adet "
            "DETACH DELETE e "
            "RETURN adet",
            {"isim": olay_ismi},
            write=True,
        )
    except Neo4jConnectionError as exc:
        logger.error("CRITICAL API ERROR (olay kapatma): %s", exc, exc_info=True)
        print(f"CRITICAL API ERROR (olay kapatma): {exc}")
        raise HTTPException(status_code=503, detail=f"Olay kapatılamadı: {exc}") from exc
    except Exception as exc:  
        raise _beklenmeyen_hatayi_logla_ve_yukselt("olay kapatma", exc) from exc

    silinen = silme_sonucu[0]["adet"] if silme_sonucu else 0
    if not silinen:
        raise HTTPException(status_code=404, detail=f"'{olay_ismi}' isimli bir olay bulunamadı (zaten kapatılmış olabilir).")

    return OlayCozumYaniti(
        olay_ismi=olay_ismi,
        silindi=True,
        acilan_altyapi_sayisi=acilan_altyapi[0]["adet"] if acilan_altyapi else 0,
        iyilesen_tesis_sayisi=iyilesen_tesis[0]["adet"] if iyilesen_tesis else 0,
    )




@app.get("/", tags=["Sistem"])
def kok() -> Dict[str, str]:
    return {
        "servis": "Kriz ve Afet Yönetimi Karar Destek Sistemi — API",
        "durum": "çalışıyor",
        "dokumantasyon": "/docs",
    }


@app.get("/health", tags=["Sistem"])
def saglik_kontrolu(db: Neo4jConnection = Depends(get_connection)) -> Dict[str, Any]:
   
    try:
        db.execute_query("RETURN 1 AS ok")
        neo4j_durumu = "bağlı"
    except Neo4jConnectionError as exc:
        logger.error("CRITICAL API ERROR (sağlık kontrolü): %s", exc, exc_info=True)
        neo4j_durumu = f"bağlantısız: {exc}"
    return {"api": "sağlıklı", "neo4j": neo4j_durumu}
