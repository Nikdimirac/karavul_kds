"""
Kriz ve Afet Yönetimi Karar Destek Sistemi
===========================================
"FAZ 5: TİCARİ ÜRÜN MİMARİSİ" — BAĞIMSIZ REST API SERVİSİ ("Streamlit
arayüzünü bir kenara bırakıp kurumsal bir C4ISR ürününe dönüşme" kararı).

Bu dosya, `src.core.decision_engine`/`src.core.database`de yaşayan (ve bu
projede kanıtlanmış: index'ler, senkron kilitlenme
düzeltmesi vb.) kriz motorunu HİÇ DEĞİŞTİRMEDEN, dışarıdan HTTP ile
erişilebilir hale getiren İNCE bir FastAPI sarmalayıcısıdır — "kaputun
altındaki motor" (decision_engine.py/database.py) burada TEK SATIR bile
DEĞİŞTİRİLMEMİŞTİR; bu dosya SADECE onu çağırır.

MİMARİ KARAR — `src.ui.app._grafa_yaz`in YENİDEN KULLANILMASI: bir kriz
raporunu (Ollama çıktısını) Bilgi Grafına GÜVENLE yazma mantığı (Ghost
Infrastructure koruması, Sahte Koordinat Kapanı, Coğrafi Bağlama/Harici
Geokodlama kurtarmaları — bkz. o fonksiyonun docstring'i) ZATEN `src.ui.
app` içinde eksiksiz ve test edilmiş halde var. Bu karmaşık/kritik mantığı
burada YENİDEN YAZMAK (kopyalamak) hem gereksiz risk hem de gelecekte iki
kopyanın birbirinden SAPMASI riski taşırdı; bu yüzden `_grafa_yaz`
DOĞRUDAN import edilip ÇAĞRILIR. `src.ui.app`in `import`u Streamlit'i
ÇALIŞTIRMAZ (`app.main()` SADECE `if __name__ == "__main__":` altında
çağrılır) — sadece bazı Streamlit önbellek dekoratörlerinin "ScriptRunContext
bulunamadı" uyarılarını konsola basabilir; bu ZARARSIZDIR, göz ardı edilebilir.

ENDPOINT'LER (bkz. `/docs`'taki otomatik Swagger arayüzü — bu dosyanın
ilk sürümünde TEK test aracıydı; "Faz 5" kararıyla artık `karavul_ui/`
altında ayrı, bağımsız bir React/Vite frontend'i de bu API'yi tüketiyor):
  1. POST /api/analyze-crisis        — kriz raporu metni -> Bilgi Grafı
     yazımı + taktiksel öneriler (JSON).
  2. GET  /api/infrastructure/status — hasarlı VE tüm tesisler/kapalı
     yollar/aktif birlikler/kritik olaylar (JSON) — bir harita arayüzünün
     besleneceği hafif, DOĞRUDAN Neo4j sorgularıdır (kullanıcının "sık
     poll edilecek" bir uç nokta beklentisiyle BİLEREK `DecisionEngine.
     gather_situational_picture`nin AĞIR Dijkstra/en-yakın-birlik
     analizini TETİKLEMEZ — bu endpoint SADECE ham durum listesi döner,
     taktiksel analiz DEĞİL).
  3. POST /api/reset-scenario        — `Neo4jConnection.reset_crisis_
     scenario`yu tetikler (NÜKLEER — TÜM senaryoyu sıfırlar).
  4. POST /api/resolve-event/{olay_ismi} — TEK bir olayı isimle bulup
     siler (diğer olayları/kapalı yolları/hasarlı tesisleri ETKİLEMEZ).

CORS ("Faz 5" frontend'i FARKLI bir origin'den, Vite'ın
varsayılan `5173` portundan bu API'ye istek atar): tarayıcılar, farklı
port/origin'e giden `fetch` isteklerini CORS başlıkları OLMADAN SESSİZCE
engeller (istek ağda GİDER ama tarayıcı yanıtı JS'e GEÇİRMEZ) — bu yüzden
`CORSMiddleware` en altta EKLENMİŞTİR (bkz. `app.add_middleware` çağrısı).

ÇALIŞTIRMAK İÇİN (proje kök dizininden):
    uvicorn api:app --reload --port 8000

Test etmek için:
    http://localhost:8000/docs  (otomatik Swagger UI)
    karavul_ui/ (React/Vite frontend — bkz. o dizindeki README/instructions)
"""

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
from src.ui.app import _grafa_yaz  # bkz. modül başındaki "MİMARİ KARAR" notu

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
logger = logging.getLogger(__name__)

_YERLESIM_LISTELEME_LIMITI = 2000
"""`/api/infrastructure/status`daki `yerlesimler` alanı için üst sınır —
`add_settlements.py` ile yüklenen ~1057 il/ilçe merkezinin TAMAMINI zaten
kapsar; `_AKTIF_BIRLIK_LISTELEME_LIMITI` ile AYNI "kesin sınır" ilkesiyle
(hiçbir liste endpoint'i sınırsız dönmez) yine de bir üst sınır konur."""

_TUM_TESIS_LISTELEME_LIMITI = 5000
"""`/api/infrastructure/status`daki `tum_tesisler` alanı için üst sınır —
veri tabanındaki ~3046 `Facility` düğümünün (Hastane 1917, Askeri Üs
1052, Havalimanı 74, Liman 3) TAMAMINI kapsar. BULGU ("hastaneler/
havalimanları haritada YOK" riski): `hasarli_tesisler` SADECE
`mevcut_durum` HASARLI/Yok Edilmiş olan tesisleri döner — aktif kriz
YOKKEN (kritik_olaylar boşken) bu liste HER ZAMAN boş kalır, dolayısıyla
binlerce SAĞLAM hastane/havalimanı Neo4j'de var olsa bile frontend'e HİÇ
ULAŞMAZ (frontend'deki "Hastaneler"/"Havalimanları" filtre kutucukları bu
yüzden ETKİSİZDİ). `tum_tesisler`, `yerlesimler` ile AYNI mantıkla, durum
FARK ETMEKSİZİN TÜM tesisleri döner; frontend duruma göre renklendirir
(bkz. `karavul_ui/src/components/MapView.tsx`)."""


# ---------------------------------------------------------------------------
# Kaynak (resource) yönetimi — `src.ui.app`daki `@st.cache_resource` deseninin
# Streamlit'siz eşdeğeri: her nesne SÜREÇ başına TEK SEFER kurulur.
# ---------------------------------------------------------------------------
# NOT: `Neo4jConnection`in KENDİSİ zaten `__new__` ile bir singleton'dır
# (bkz. `database.py`) — `lru_cache` burada TEKNİK olarak zorunlu değildir,
# ama `get_parser`/`get_engine` ile AYNI, tutarlı bir bağımlılık-enjeksiyonu
# (Dependency Injection) deseni sağlamak için BİLİNÇLİ olarak kullanılır.


@lru_cache()
def get_connection() -> Neo4jConnection:
    return Neo4jConnection()


@lru_cache()
def get_parser() -> OllamaParser:
    # Kanıtlanmış bir boşluk için savunma ("Analiz Et" butonu "LLM cevabi
    # gecerli JSON degil (agresif temizlik SONRASI bile)" hatası verebiliyordu):
    # BU fonksiyon ile `get_engine()` AYNI `OLLAMA_MODEL` değişkenini
    # OKUYORDU. `karavul-kurmay` (bkz. `train_model.py`), SADECE kısa/serbest
    # askeri emir CÜMLELERİ üretmek için fine-tune edildi (`decision_engine`
    # görevine ÖZGÜ) — `OllamaParser`ın burada ihtiyaç duyduğu, `models.py`
    # şemasının TAMAMINI kapsayan UZUN/karmaşık JSON çıktısını GÜVENİLİR
    # üretmek için EĞİTİLMEDİ; `.env`de `OLLAMA_MODEL=karavul-kurmay`
    # ayarlandığında bu fonksiyon da SESSİZCE o modele geçti ve JSON ayrıştırma
    # kırıldı. Çözüm: iki görev artık AYRI env değişkenleriyle yapılandırılır
    # — bu fonksiyon `OLLAMA_EXTRACTION_MODEL`i (varsayılan: genel amaçlı
    # `llama3`) okur, `get_engine()` ise `OLLAMA_TACTICAL_MODEL`i (varsayılan:
    # fine-tuned `karavul-kurmay`).
    return OllamaParser(model=os.getenv("OLLAMA_EXTRACTION_MODEL", "llama3"))


@lru_cache()
def get_engine() -> DecisionEngine:
    # bkz. `.env`deki `OLLAMA_TACTICAL_MODEL` notu: taktiksel model artık
    # fine-tune edilmiş `karavul-kurmay` DEĞİL, "Kurmay" kişiliğinin bir
    # SYSTEM prompt'u (bkz. `Modelfile.qwen`) olarak eklendiği ham
    # `qwen2.5` tabanlı `karavul-kurmay-qwen` etiketidir — Türkçe
    # dilbilgisi/tutarlılık açısından fine-tune edilmiş Llama-3 tabanına
    # kıyasla belirgin şekilde daha güçlüdür.
    return DecisionEngine(get_connection(), model=os.getenv("OLLAMA_TACTICAL_MODEL", "karavul-kurmay-qwen"))


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """API açılırken Neo4j bağlantısını KURAR ve şema kısıtlarını/index'lerini
    doğrular (bkz. `Neo4jConnection.ensure_constraints` — idempotenttir,
    zaten var olan index'leri sessizce atlar); API kapanırken bağlantıyı
    temiz biçimde kapatır."""
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

# "FAZ 5" CORS AÇILIMI (bkz. modül başındaki "CORS"
# notu): `karavul_ui/` (Vite dev sunucusu, varsayılan `http://localhost:
# 5173`) FARKLI bir origin'den bu API'ye istek atar. Şimdilik SADECE yerel
# geliştirme origin'lerine (Vite'ın varsayılan/olası portları) izin verilir;
# bu bir "kurumsal ürün"ün üretim dağıtımında (bkz. modül başındaki "Faz 5"
# notu) GERÇEK frontend domainiyle DARALTILMALIDIR — `allow_origins`i
# `"*"` yapmak ASLA önerilmez (bkz. FastAPI/Starlette dokümantasyonu:
# `allow_credentials=True` ile `"*"` zaten birlikte KULLANILAMAZ).
_GELISTIRME_ORIJINLERI = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",  # `vite preview` varsayılan portu
    "http://127.0.0.1:4173",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_GELISTIRME_ORIJINLERI,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Ortak yardımcı: HİÇBİR hata sessizce yutulmasın (bkz. bu projenin daha
# önceki "yutulan hata" düzeltmeleri — AYNI ilke burada da geçerlidir).
# ---------------------------------------------------------------------------


def _beklenmeyen_hatayi_logla_ve_yukselt(baglam: str, exc: Exception) -> HTTPException:
    logger.error("CRITICAL API ERROR (%s, BEKLENMEYEN tip): %s", baglam, exc, exc_info=True)
    print(f"CRITICAL API ERROR ({baglam}, BEKLENMEYEN tip): {type(exc).__name__}: {exc}")
    return HTTPException(status_code=500, detail=f"Beklenmeyen hata ({type(exc).__name__}): {exc}")


# ---------------------------------------------------------------------------
# 1) POST /api/analyze-crisis
# ---------------------------------------------------------------------------


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
    """Kriz raporu metnini Ollama ile ayrıştırır, Bilgi Grafına yazar VE
    (yazma başarılıysa) Karar Destek Motoru'ndan taktiksel öneriler üretir.

    İKİ AŞAMA AYRI hata toleransına sahiptir: Ollama ayrıştırma/Bilgi Grafı
    yazımı başarısız olursa istek TAMAMEN başarısız sayılır (uygun HTTP hata
    koduyla). AMA yazma BAŞARILI olduktan SONRA taktiksel öneri üretimi
    (AYRI bir Ollama çağrısı zinciri) başarısız olursa, rapor YİNE DE Bilgi
    Grafına İŞLENMİŞ sayılır — istek 200 döner, `taktiksel_oneriler` boş
    kalır ve `uyari` alanı bunu AÇIKÇA belirtir (rapor/kriz verisi ASLA
    kaybolmaz, sadece o anki analiz adımı atlanmış olabilir).
    """
    try:
        sonuc = parser.extract_entities(istek.rapor_metni)
    except (RuntimeError, ValueError) as exc:
        logger.error("CRITICAL API ERROR (Ollama ayrıştırma): %s", exc, exc_info=True)
        print(f"CRITICAL API ERROR (Ollama ayrıştırma): {exc}")
        raise HTTPException(status_code=503, detail=f"Ollama servisiyle iletişim kurulamadı: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - KASITLI: hicbir hata sessizce yutulmasin.
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
    except Exception as exc:  # noqa: BLE001 - KASITLI: hicbir hata sessizce yutulmasin.
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

    # İKİNCİ AŞAMA (bkz. docstring): bu adımın başarısızlığı İLK aşamanın
    # (yazma) başarısını GERİYE ALMAZ/geçersiz kılmaz.
    durum_ozeti = ""
    taktiksel_oneriler: List[str] = []
    uyari: Optional[str] = None
    try:
        # Kanıtlanmış bir boşluk için savunma (bkz. `DecisionEngine.
        # generate_recommendations` docstring'indeki ayni basliklı not — art
        # arda birden fazla rapor girildiğinde birlik/mesafe eslesmeleri
        # farkli krizler arasinda KARISABILIYORDU): bu CAGRIDA yazilan Event(ler)in koordinati iletilir,
        # boylece anlati SADECE BU rapora odaklanir; birlik ARAMA kapsami
        # (ulke capinda en iyi/en yakin birligi bulma) DEGISMEZ.
        # `paralel_calistir=True`: bkz. `DecisionEngine.generate_recommendations`
        # docstring'indeki PERFORMANS OPTİMİZASYONU notu — bu YOL (FastAPI)
        # Streamlit'in ScriptRunContext'ini ASLA içermediğinden, eskiden
        # SADECE Streamlit'e özgü bir çakışma yüzünden geri alınan gerçek
        # paralellik burada GÜVENLE kullanılır (~3.8x hızlanma, ölçüldü).
        ai_sonuc = engine.generate_recommendations(
            odak_koordinatlari=yazilan_event_koordinatlari or None, paralel_calistir=True
        )
        durum_ozeti = ai_sonuc.get("durum_ozeti") or ""
        taktiksel_oneriler = parse_oneri_maddeleri(ai_sonuc.get("oneriler_metni") or "")
        # "COĞRAFİ ÖN-KONTROL": rapor LLM'e
        # hiç gitmeden reddedildiyse (bkz. `DecisionEngine.generate_
        # recommendations`), bunu `taktiksel_oneriler`in içine gömülü
        # bırakmak YERİNE `uyari` alanında da AÇIKÇA belirt — API
        # tüketicileri bunu normal bir öneri listesiyle KARIŞTIRMASIN.
        if ai_sonuc.get("cografi_red"):
            uyari = f"🚫 {ai_sonuc.get('cografi_red_nedeni') or 'Coğrafi tutarsızlık tespit edildi.'}"
    except Exception as exc:  # noqa: BLE001 - KASITLI: rapor yazimi BASARILI oldu, bu adim ayrica loglanir.
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


# ---------------------------------------------------------------------------
# 2) GET /api/infrastructure/status
# ---------------------------------------------------------------------------


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
    # "FAZ 6: 3B TAKTİKSEL HARİTA" EKLENTİSİ (şeffaf/neon
    # ÇİZGİ olarak çizilen yollar): `Infrastructure.bitis_enlem`/`bitis_
    # boylam` DOĞRUDAN geçirilir (bkz. `models.Infrastructure`) — SENTETİK
    # bir tahmin ÜRETİLMEZ (bkz. `src.ui.app._tahmini_bitis_noktasi`, o
    # SADECE harita render'ı için kullanılan bir UI yaklaşıklığıdır, API
    # katmanı SADECE gerçek veriyi taşır). İkisi de `None` ise frontend bu
    # kaydı bir ÇİZGİ değil, TEK bir nokta olarak çizer.
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
    # BULGU DÜZELTMESİ (bkz. `_TUM_TESIS_LISTELEME_LIMITI` docstring'i):
    # `hasarli_tesisler` yalnızca HASARLI/Yok Edilmiş tesisleri taşır; harita
    # katmanının ("Hastaneler"/"Havalimanları"/"Limanlar" filtreleri) durum
    # FARK ETMEKSİZİN TÜM tesisleri görebilmesi için bu alan eklendi.
    tum_tesisler: List[TesisDurumu]
    kapali_yollar: List[AltyapiDurumu]
    aktif_birlikler: List[BirlikDurumu]
    kritik_olaylar: List[OlayDurumu]
    # "FAZ 8: SİVİL YERLEŞİM YERLERİ" EKLENTİSİ (bkz.
    # `add_settlements.py`): sivil il/ilçe merkezleri de artık haritanın bir
    # parçası — bu alan olmadan `Settlement` düğümleri Neo4j'de var olsa
    # bile frontend'e HİÇ ULAŞMAZDI.
    yerlesimler: List[YerlesimDurumu]


@app.get("/api/infrastructure/status", response_model=AltyapiDurumYaniti, tags=["Durum"])
def infrastructure_status(db: Neo4jConnection = Depends(get_connection)) -> AltyapiDurumYaniti:
    """Hasarlı tesisleri, kapalı yolları ve aktif birlikleri döner —
    bir harita arayüzünün besleneceği HAFİF, DOĞRUDAN Neo4j sorgularıdır
    (bkz. modül başındaki "ENDPOINT'LER" notu — `DecisionEngine.gather_
    situational_picture`nin ağır Dijkstra/en-yakın-birlik analizini BİLEREK
    TETİKLEMEZ, bu endpoint sık poll edilebilecek şekilde hafif tutulmuştur).

    Sorgular, `decision_engine.gather_situational_picture`deki AYNI
    filtreleme kurallarını (Hasarlı/Yok Edildi durumları, OSM teknik-kimlik
    soneği taşıyan isimsiz adayların elenmesi, aynı caddenin/köprünün
    onlarca noktasının tekilleştirilmesi) kullanır — TUTARLILIK için bu
    sabitler/yardımcı (`HASARLI_TESIS_DURUMLARI`, `_isim_bazinda_
    tekillestir`) doğrudan o modülden import edilir, burada YENİDEN
    TANIMLANMAZ.
    """
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
    except Exception as exc:  # noqa: BLE001 - KASITLI: hicbir hata sessizce yutulmasin.
        raise _beklenmeyen_hatayi_logla_ve_yukselt("altyapı durumu sorgusu", exc) from exc

    return AltyapiDurumYaniti(
        hasarli_tesisler=hasarli_tesisler,
        tum_tesisler=tum_tesisler,
        kapali_yollar=_isim_bazinda_tekillestir(kapali_yollar_ham),
        aktif_birlikler=aktif_birlikler,
        kritik_olaylar=kritik_olaylar,
        yerlesimler=yerlesimler,
    )


# ---------------------------------------------------------------------------
# 3) POST /api/reset-scenario
# ---------------------------------------------------------------------------


class SifirlamaYaniti(BaseModel):
    silinen_olay: int
    acilan_altyapi: int
    iyilesen_tesis: int
    iyilesen_birim: int


@app.post("/api/reset-scenario", response_model=SifirlamaYaniti, tags=["Durum"])
def reset_scenario(db: Neo4jConnection = Depends(get_connection)) -> SifirlamaYaniti:
    """Mevcut kriz senaryosunu sıfırlar (bkz. `Neo4jConnection.reset_crisis_
    scenario`): TÜM `Event` düğümlerini siler, TÜM `Infrastructure`
    düğümlerini tekrar `acik_mi=True` yapar, TÜM `Facility`/`Unit`
    düğümlerinin hasar durumunu tekrar 'Aktif'e döndürür. GERÇEK şehir
    topolojisi (düğümlerin varlığı) ASLA silinmez, SADECE bir önceki
    senaryonun durum/hasar izleri temizlenir."""
    try:
        sonuc = db.reset_crisis_scenario()
    except Neo4jConnectionError as exc:
        logger.error("CRITICAL API ERROR (senaryo sıfırlama): %s", exc, exc_info=True)
        print(f"CRITICAL API ERROR (senaryo sıfırlama): {exc}")
        raise HTTPException(status_code=503, detail=f"Senaryo sıfırlanamadı: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - KASITLI: hicbir hata sessizce yutulmasin.
        raise _beklenmeyen_hatayi_logla_ve_yukselt("senaryo sıfırlama", exc) from exc

    return SifirlamaYaniti(**sonuc)


# ---------------------------------------------------------------------------
# 4) POST /api/resolve-event/{olay_ismi}
# ---------------------------------------------------------------------------
# "BİREYSEL OLAY YÖNETİMİ" ("Haritada önceki senaryolardan kalan olaylar
# duruyor, tek tek yönetilemiyor" ihtiyacına karşı):
# `/api/reset-scenario` NÜKLEER bir aksiyondur (TÜM senaryoyu sıfırlar) —
# bu endpoint onun YANINDA, TEK bir olayı hedefleyen İNCE bir alternatiftir.
# `Event.isim` üzerindeki UNIQUE CONSTRAINT (bkz. `ensure_constraints`)
# sayesinde isimle eşleşme İNDEKS-DESTEKLİDİR (tam taramaya gerek yok).


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
    """Tek bir kritik olayı (`Event`), İSMİYLE bulup Bilgi Grafından
    TAMAMEN SİLER (`DETACH DELETE` — ona bağlı `AFFECTS`/`LOCATED_IN`
    ilişkileri de KALDIRILIR). Komutanın "Çözüldü/Kapat" aksiyonu budur:
    `/api/reset-scenario`nun AKSİNE ne diğer olayları, ne DİĞER olayların
    kapattığı yolları/hasarlı tesisleri ETKİLER — SADECE bu tek olay
    kapanır.

    "ZİNCİRLEME İYİLEŞME" (Cascading Recovery) DÜZELTMESİ (kanıtlanmış bir
    boşluk için savunma: olay silinince o olayın kapattığı yollar/hasar
    verdiği tesisler HÂLÂ kapalı/hasarlı kalabiliyordu, Kapalı Yol sayısı
    düşmüyordu): eski sürüm SADECE `Event` düğümünü siliyordu — `AFFECTS`/
    `LOCATED_IN` ilişkisiyle bağlı `Infrastructure`/`Facility` düğümlerinin
    `acik_mi`/`mevcut_durum` alanlarına HİÇ dokunmuyordu, bu yüzden o
    yol/tesis Neo4j'de GERÇEKTEN kapalı/hasarlı KALIYORDU (bir önbellek
    hayaleti DEĞİL — `reset_crisis_scenario`daki AYNI SINIF "UI Ghosting
    Yanlış Teşhisi" dersi burada da geçerli). Çözüm: `Event` SİLİNMEDEN
    ÖNCE, ona `AFFECTS`/`LOCATED_IN` ile bağlı TÜM `Infrastructure`
    düğümleri `acik_mi=true`ya, TÜM `Facility` düğümleri `mevcut_durum`/
    `durum='Aktif'`e döndürülür — `database.reset_crisis_scenario`daki
    AYNI alan/değer sözleşmesini kullanır (tutarlılık için), ama SADECE bu
    TEK olaya bağlı düğümlerle SINIRLIDIR (`reset_crisis_scenario`nun
    aksine TÜM grafı etkilemez). Üç adım BİLİNÇLİ olarak AYRI sorgulardır
    (tek, karmaşık/çok-koşullu bir sorgu yerine) — `reset_crisis_scenario`
    ile AYNI "her düğüm tipi kendi net sorgusunu alır" deseni.

    `olay_ismi` bulunamazsa (ör. zaten kapatılmış/yanlış isim) 404 döner —
    "sessizce hiçbir şey yapmadı" durumu YOKTUR, çağıran taraf (bkz.
    `karavul_ui`) bunu AÇIKÇA görür.
    """
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
    except Exception as exc:  # noqa: BLE001 - KASITLI: hicbir hata sessizce yutulmasin.
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


# ---------------------------------------------------------------------------
# Sistem — kök/sağlık kontrolü (test kolaylığı için; "iş" endpoint'i DEĞİL)
# ---------------------------------------------------------------------------


@app.get("/", tags=["Sistem"])
def kok() -> Dict[str, str]:
    return {
        "servis": "Kriz ve Afet Yönetimi Karar Destek Sistemi — API",
        "durum": "çalışıyor",
        "dokumantasyon": "/docs",
    }


@app.get("/health", tags=["Sistem"])
def saglik_kontrolu(db: Neo4jConnection = Depends(get_connection)) -> Dict[str, Any]:
    """Neo4j'e gerçek bir sorgu atarak bağlantının GERÇEKTEN canlı olduğunu
    doğrular (sadece nesnenin var olduğunu DEĞİL)."""
    try:
        db.execute_query("RETURN 1 AS ok")
        neo4j_durumu = "bağlı"
    except Neo4jConnectionError as exc:
        logger.error("CRITICAL API ERROR (sağlık kontrolü): %s", exc, exc_info=True)
        neo4j_durumu = f"bağlantısız: {exc}"
    return {"api": "sağlıklı", "neo4j": neo4j_durumu}
