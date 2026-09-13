"""
Kriz ve Afet Yönetimi Karar Destek Sistemi
===========================================
"FAZ 8: SİVİL YERLEŞİM YERLERİ" — TEK SEFERLİK VERİ ZENGİNLEŞTİRME BETİĞİ
(kullanıcı talebi — Kurucu tespiti: "Karavul bir afet yönetim sistemi
olduğu için veritabanında sivil Yerleşim Yerleri'nin olmaması büyük bir
mantık hatası").

Bu betik, OpenStreetMap Overpass API'sinden Türkiye'nin TÜM il VE ilçe
MERKEZLERİNİ (idari sınır İLİŞKİLERİNİN Overpass `out center` ile
hesapladığı temsili merkez noktası) çekip Neo4j'e `Settlement` düğümleri
olarak yazar, ardından HER yerleşimi coğrafi olarak en yakın gerçek yol
(`Infrastructure`) düğümüne `[:CONNECTED_TO]` ilişkisiyle bağlar — böylece
Karar Destek Motoru artık SADECE tesis/birlik/olay değil, GERÇEK sivil
nüfus merkezlerini de lojistik ağın bir parçası olarak görebilir.

MİMARİ KARAR — NEDEN YENİ BİR `Settlement` DÜĞÜM TİPİ (`AdministrativeArea`
DEĞİL): bkz. `src.core.models.Settlement` sınıfının docstring'i — özetle,
`AdministrativeArea.nufus`/`bina_sayisi` BİLİNÇLİ OLARAK REQUIRED'dır
(sentetik/TUCBS ETL'i için tasarlandı); GERÇEK OSM verisinde bu alanlar
güvenilir biçimde mevcut olmadığından, onları zorlamak ya SAHTE veri
uydurmayı ya da mevcut TUCBS akışını bozmayı gerektirirdi. Bunun yerine
`nufus`u GERÇEKTEN Optional tutan, bağımsız/hafif bir `Settlement` tipi
tercih edildi.

MİMARİ KARAR — NEDEN Overpass'TAN `(Infrastructure:Road)` DEĞİL, TÜM
`Infrastructure` ARANIYOR: kullanıcı talebi `(Infrastructure:Road)`
diyordu (bkz. `models.Infrastructure._ALT_ETIKET_ESLEMESI` — "Road" İKİNCİL
etiketi SADECE `infrastructure_type="Karayolu"` içindir). CANLI olarak
doğrulandı: `Karayolu` tipi SADECE LLM'in bir kriz raporunda ÜRETTİĞİ
ad-hoc bir kategoridir — `seed_db.py` ile yüklenen ULUSAL OMURGA/kılcal ağ
BUNU HİÇ İÇERMEZ (SADECE Sokak/Köprü + Facility/Unit içerir). `:Road`e
SIKI SIKIYA bağlı kalınsaydı, TAZE bir kurulumda NEREDEYSE HİÇBİR yerleşim
bağlanamazdı (arama havuzu boş). Bu yüzden arama BİLİNÇLİ olarak `konum`u
olan TÜM `Infrastructure` alt-tiplerine (Sokak/Köprü DAHİL — gerçek OMURGA
ağının KENDİSİ) genişletildi; `CONNECTED_TO` ilişkisi (bkz. `models.
RelationshipType` — zaten "Infrastructure <-> Infrastructure/Facility"
için tanımlıydı, burada Settlement'a GENİŞLETİLDİ) yine de SADECE gerçek,
Neo4j'de VAR OLAN bir düğüme kurulur — hiçbir "hayalet" bağlantı YOKTUR.

DÜRÜST SINIR: "İl ve ilçe merkezi" için OSM'in idari sınır (`boundary=
administrative`) İLİŞKİLERİ kullanılır (`admin_level=4`→il, `admin_level=
6`→ilçe) — bu, TÜİK'in resmi 81 il + ~922 ilçe listesiyle BİREBİR AYNI
OLMAYABİLİR (OSM topluluk verisidir, birkaç fazla/eksik kayıt olabilir;
canlı testte 82 il + 975 ilçe elemanı döndü — "yaklaşık 1000" hedefiyle
TUTARLI). İlçenin BAĞLI OLDUĞU il, OSM etiketlerinden GÜVENİLİR biçimde
okunamadığından (canlı testte `ISO3166-2` etiketi ilçelerin %0'ında
BULUNDU) coğrafi olarak EN YAKIN il merkezine göre (haversine) belirlenir
— bu, TÜİK'in idari kaydına göre NADİREN yanlış olabilecek (bir ilçe
merkezi, kendi iline değil komşu bir ile daha yakın koordinatta olabilir)
ama dürüstçe belgelenmiş bir yaklaşıklıktır.

Çalıştırmak için (proje kök dizininden, Neo4j ÇALIŞIYORKEN):
    python add_settlements.py
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from src.core.database import Neo4jConnection, Neo4jConnectionError
from src.core.models import Settlement, SettlementType
from src.data_ingestion.osm_loader import OVERPASS_ENDPOINTS, OVERPASS_REQUEST_HEADERS
from src.data_ingestion.real_osm_loader import _run_overpass_query  # bkz. modül başı "reuse" gerekçesi
from src.data_ingestion.tucbs_etl_loader import TucbsETLLoader

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

_OVERPASS_ZAMAN_ASIMI_SANIYE = 180
"""İl+ilçe idari sınır sorgusu (~1000 `relation`, her biri bir sınır
poligonu) tek bir tesis/sokak sorgusundan HACİMCE daha küçük olsa da,
Overpass'ın bu ilişkileri çözmesi (özellikle `out center` hesaplaması)
zaman alabilir; `real_osm_loader.py`deki AYNI "cömert payla" ilkesiyle
büyük tutuldu."""

_MIN_BEKLENEN_SONUC: Dict[str, int] = {"4": 70, "6": 800}
""""SESSİZCE BOZUK VERİYLE DEVAM ETME" DÜZELTMESİ (canlı testte YAKALANDI):
`_run_overpass_query`nin (bkz. `real_osm_loader.py`) "HTTP 200 + remark
yok = başarılı" kontrolü, bir aynanın (canlı testte `overpass.osm.ch`)
GERÇEKTEN 0 eleman içeren ama teknik olarak "hatasız" bir yanıt dönmesini
YAKALAYAMAZ — bu durumda `_en_yakin_il` boş bir listeyle çağrılıp
`IndexError` ile ÇÖKÜYORDU. Türkiye'nin GERÇEK il (81) ve ilçe (~922) sayısı
BİLİNDİĞİNDEN, bu asgari eşiklerin (payla birlikte) ALTINDA kalan bir sonuç
"bozuk/eksik yanıt" sayılıp `_idari_sinir_merkezlerini_getir_guvenli` (bkz.
aşağısı) TARAFINDAN bir kez daha denenir; ikinci denemede de başarısız
olursa (nadir), betik SESSİZCE yanlış veriyle DEVAM ETMEK yerine AÇIKÇA
`RuntimeError` fırlatır."""

_BAGLANTI_YARICAPI_KM_ILK = 5.0
_BAGLANTI_YARICAPI_KM_GENIS = 10.0
""""max 5-10 km yarıçapında" (kullanıcı talebi): ÖNCE 5 km denenir; hiçbir
Infrastructure bulunamazsa (seyrek nüfuslu/kılcal ağın henüz yüklenmediği
bir bölge), pes etmeden ÖNCE 10 km'ye genişletilir. İKİSİNDE de bulunamazsa
o yerleşim BAĞLANTISIZ kalır (SESSİZCE bir "hayalet" ilişki UYDURULMAZ) —
bkz. `main()` sonundaki dürüst özet raporu."""


# ---------------------------------------------------------------------------
# 1) VERİ ÇEKME (Ingestion) — Overpass'tan il/ilçe idari sınır merkezleri
# ---------------------------------------------------------------------------


def _idari_sinir_merkezlerini_getir(admin_level: str) -> List[Dict[str, Any]]:
    """Türkiye sınırları İÇİNDEKİ, verilen `admin_level`teki (4=il, 6=ilçe)
    TÜM idari sınır İLİŞKİLERİNİ (`relation`), Overpass'ın `out center`
    özelliğiyle hesapladığı temsili merkez koordinatlarıyla BİRLİKTE çeker.

    `out center`: bir `relation`ın (poligon sınırı) merkezini Overpass'ın
    KENDİSİ hesaplayıp döndürür — ayrıca bir "admin_centre" üye-düğümü
    çözme sorgusu GEREKMEZ (daha basit, TEK sorguda tamamlanır).
    """
    sorgu = f"""
[out:json][timeout:{_OVERPASS_ZAMAN_ASIMI_SANIYE}];
area["name"="Türkiye"]["boundary"="administrative"]["admin_level"="2"]->.turkiye;
(
  relation(area.turkiye)["boundary"="administrative"]["admin_level"="{admin_level}"];
);
out center tags;
""".strip()
    veri = _run_overpass_query(
        sorgu,
        endpoints=OVERPASS_ENDPOINTS,
        request_timeout=_OVERPASS_ZAMAN_ASIMI_SANIYE + 60,
    )
    sonuclar: List[Dict[str, Any]] = []
    for eleman in veri.get("elements", []):
        etiketler = eleman.get("tags", {})
        merkez = eleman.get("center")
        isim = etiketler.get("name")
        if not isim or not merkez:
            continue  # ismi/merkezi olmayan (bozuk/eksik) bir kayıt SESSİZCE atlanır.
        sonuclar.append(
            {
                "isim": isim,
                "enlem": merkez["lat"],
                "boylam": merkez["lon"],
                "nufus": _guvenli_int(etiketler.get("population")),
            }
        )
    return sonuclar


def _idari_sinir_merkezlerini_getir_guvenli(admin_level: str) -> List[Dict[str, Any]]:
    """`_idari_sinir_merkezlerini_getir`i, sonucu `_MIN_BEKLENEN_SONUC`e karşı
    doğrulayarak çağırır — şüpheli derecede KÜÇÜK/boş bir sonuç (bkz.
    yukarıdaki "SESSİZCE BOZUK VERİYLE DEVAM ETME" notu) bir kez daha
    denenir; ikinci denemede de başarısız olursa `RuntimeError` fırlatılır."""
    for deneme in (1, 2):
        sonuc = _idari_sinir_merkezlerini_getir(admin_level)
        if len(sonuc) >= _MIN_BEKLENEN_SONUC[admin_level]:
            return sonuc
        logger.warning(
            "admin_level=%s sorgusu şüpheli derecede az sonuç döndürdü (%d, beklenen >= %d) "
            "— deneme %d/2.", admin_level, len(sonuc), _MIN_BEKLENEN_SONUC[admin_level], deneme,
        )
        time.sleep(10)
    raise RuntimeError(
        f"admin_level={admin_level} sorgusu 2 denemede de yeterli sonuç döndürmedi "
        f"(beklenen >= {_MIN_BEKLENEN_SONUC[admin_level]}) — Overpass aynaları geçici olarak "
        "bozuk veri dönüyor olabilir, lütfen birkaç dakika sonra tekrar deneyin."
    )


def _guvenli_int(deger: Any) -> Optional[int]:
    """OSM `population` etiketi bazen sayı DEĞİL serbest metin (ör.
    "yaklaşık 50000") olabilir — ayrıştırılamazsa UYDURULMAZ, `None` döner
    (bkz. `Settlement.nufus` docstring'i)."""
    if deger is None:
        return None
    try:
        return int(str(deger).replace(".", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _en_yakin_il(
    enlem: float, boylam: float, iller: List[Dict[str, Any]]
) -> str:
    """Bir ilçe merkezinin BAĞLI OLDUĞU ili, OSM etiketlerinden GÜVENİLİR
    biçimde okunamadığı için (bkz. modül başı "DÜRÜST SINIR" notu) coğrafi
    olarak EN YAKIN il merkezine göre (haversine) belirler."""
    en_yakin_isim = iller[0]["isim"]
    en_yakin_mesafe = float("inf")
    for il in iller:
        mesafe = Neo4jConnection._geodesic_km((enlem, boylam), (il["enlem"], il["boylam"]))
        if mesafe < en_yakin_mesafe:
            en_yakin_mesafe = mesafe
            en_yakin_isim = il["isim"]
    return en_yakin_isim


def yerlesimleri_olustur() -> List[Settlement]:
    """Overpass'tan il+ilçe merkezlerini çekip `Settlement` nesnelerine
    çevirir. İlçe isimleri HER ZAMAN `"<İlçe> (<İl>)"` biçiminde
    SONEKLENİR (il isimleri sonek istemez, 81 il arasında zaten benzersizdir)
    — bkz. `local_osm_reader._amenity_dugumu_uret`deki AYNI "ULUSAL İSİM
    ÇAKIŞMASI" düzeltmesi: `isim`, Neo4j'de TEK başına benzersizlik
    anahtarıdır (bkz. `ensure_constraints`); iki farklı ildeki aynı isimli
    ilçelerin SESSİZCE tek düğüme MERGE olmasını önler.
    """
    print("[1/3] İl merkezleri (admin_level=4) Overpass'tan çekiliyor...")
    iller_ham = _idari_sinir_merkezlerini_getir_guvenli("4")
    print(f"      {len(iller_ham)} il merkezi bulundu.")

    print("[2/3] İlçe merkezleri (admin_level=6) Overpass'tan çekiliyor (biraz sürebilir)...")
    ilceler_ham = _idari_sinir_merkezlerini_getir_guvenli("6")
    print(f"      {len(ilceler_ham)} ilçe merkezi bulundu.")

    yerlesimler: List[Settlement] = []
    for il in iller_ham:
        yerlesimler.append(
            Settlement(
                isim=il["isim"],
                yerlesim_tipi=SettlementType.IL_MERKEZI,
                il=il["isim"],
                enlem=il["enlem"],
                boylam=il["boylam"],
                nufus=il["nufus"],
            )
        )

    for ilce in ilceler_ham:
        en_yakin_il = _en_yakin_il(ilce["enlem"], ilce["boylam"], iller_ham)
        yerlesimler.append(
            Settlement(
                isim=f"{ilce['isim']} ({en_yakin_il})",
                yerlesim_tipi=SettlementType.ILCE_MERKEZI,
                il=en_yakin_il,
                enlem=ilce["enlem"],
                boylam=ilce["boylam"],
                nufus=ilce["nufus"],
            )
        )

    return yerlesimler


# ---------------------------------------------------------------------------
# 2) NEO4J ŞEMASI — yazma + lojistik ağa `[:CONNECTED_TO]` ile bağlama
# ---------------------------------------------------------------------------


def en_yakin_altyapiya_bagla(db: Neo4jConnection, yerlesim_id: str, enlem: float, boylam: float) -> Optional[float]:
    """Bir `Settlement` düğümünü, `konum`u olan EN YAKIN `Infrastructure`
    düğümüne (bkz. modül başı "NEDEN TÜM Infrastructure" gerekçesi) ÖNCE 5,
    bulunamazsa 10 km yarıçapında arar ve `MERGE` ile `[:CONNECTED_TO]`
    ilişkisi kurar (`MERGE` — betik tekrar çalıştırılırsa DUPLIKE ilişki
    OLUŞMAZ). Zaten var olan `spatial_infrastructure_konum` POINT INDEX'ini
    (bkz. `database.ensure_spatial_indexes`) kullanır.

    Returns:
        Bağlanılan altyapıya olan mesafe (km) — hiçbir aday bulunamazsa
        `None` (bu yerleşim BAĞLANTISIZ kalır, hayalet bir ilişki UYDURULMAZ).
    """
    for yaricap_km in (_BAGLANTI_YARICAPI_KM_ILK, _BAGLANTI_YARICAPI_KM_GENIS):
        sonuc = db.execute_query(
            "MATCH (i:Infrastructure) WHERE i.konum IS NOT NULL "
            "AND point.distance(i.konum, point({latitude: $enlem, longitude: $boylam, crs: 'wgs-84'})) <= $yaricap_metre "
            "WITH i, point.distance(i.konum, point({latitude: $enlem, longitude: $boylam, crs: 'wgs-84'})) AS mesafe_metre "
            "ORDER BY mesafe_metre ASC LIMIT 1 "
            "MATCH (s:Settlement {id: $yerlesim_id}) "
            "MERGE (s)-[r:CONNECTED_TO]->(i) "
            "SET r.mesafe_km = mesafe_metre / 1000.0 "
            "RETURN mesafe_metre / 1000.0 AS mesafe_km",
            {
                "enlem": enlem, "boylam": boylam,
                "yaricap_metre": yaricap_km * 1000.0,
                "yerlesim_id": yerlesim_id,
            },
            write=True,
        )
        if sonuc:
            return sonuc[0]["mesafe_km"]
    return None


def main() -> None:
    t0 = time.perf_counter()
    try:
        db = Neo4jConnection()
        db.connect()
        db.ensure_constraints()  # Settlement DAHİL, NODE_REGISTRY'deki HER etiket için id/isim kısıtı+index.
    except Neo4jConnectionError as exc:
        print(f"✗ Neo4j'e bağlanılamadı: {exc}")
        sys.exit(1)

    try:
        yerlesimler = yerlesimleri_olustur()
    except RuntimeError as exc:
        print(f"✗ Overpass'tan veri çekilemedi: {exc}")
        sys.exit(1)

    if not yerlesimler:
        print("✗ BAŞARISIZ: Overpass sorgusu 0 yerleşim döndürdü.")
        sys.exit(1)

    print(f"[3/3] {len(yerlesimler)} Settlement düğümü Neo4j'e yazılıyor...")
    sayaclar = TucbsETLLoader(db).bulk_insert_nodes(yerlesimler, toplam_tahmini=len(yerlesimler))
    print(f"      Yazıldı: {sayaclar}")

    print(f"\n[Lojistik Bağlantı] {len(yerlesimler)} yerleşim, en yakın yol/altyapı düğümüne bağlanıyor "
          f"({_BAGLANTI_YARICAPI_KM_ILK:.0f}-{_BAGLANTI_YARICAPI_KM_GENIS:.0f} km yarıçapında)...")
    baglanan = 0
    baglanamayan: List[str] = []
    for i, yerlesim in enumerate(yerlesimler, start=1):
        mesafe = en_yakin_altyapiya_bagla(db, yerlesim.id, yerlesim.enlem, yerlesim.boylam)
        if mesafe is not None:
            baglanan += 1
        else:
            baglanamayan.append(yerlesim.isim)
        if i % 100 == 0 or i == len(yerlesimler):
            print(f"      {i}/{len(yerlesimler)} işlendi ({baglanan} bağlandı)...")

    sure = time.perf_counter() - t0

    print("\n" + "=" * 78)
    print(f"✅ TAMAMLANDI: {len(yerlesimler)} yerleşim yazıldı, {baglanan}/{len(yerlesimler)} "
          f"en yakın altyapıya [:CONNECTED_TO] ile bağlandı. Süre: {sure:.1f} sn.")
    if baglanamayan:
        print(f"⚠️  {len(baglanamayan)} yerleşim {_BAGLANTI_YARICAPI_KM_GENIS:.0f} km içinde HİÇBİR "
              "altyapı düğümü bulamadı (muhtemelen o bölgenin kılcal yol ağı henüz `seed_db.py` ile "
              "yüklenmemiş) — bunlar BAĞLANTISIZ kaldı, hayalet bir ilişki UYDURULMADI:")
        for isim in baglanamayan[:20]:
            print(f"      - {isim}")
        if len(baglanamayan) > 20:
            print(f"      ... ve {len(baglanamayan) - 20} tane daha.")
    print("=" * 78)

    db.close()


if __name__ == "__main__":
    main()
