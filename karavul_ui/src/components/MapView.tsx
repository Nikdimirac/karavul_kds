import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import Map, {
  Layer,
  Marker,
  NavigationControl,
  Source,
  type ErrorEvent as MaplibreErrorEvent,
  type MapRef,
  type ViewStateChangeEvent,
} from 'react-map-gl/maplibre'
import type { Map as MaplibreMap } from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import { TriangleAlert } from 'lucide-react'
import TacticalFilters, {
  VARSAYILAN_KATMAN_GORUNURLUGU,
  type KatmanGorunurlugu,
} from './TacticalFilters'
import MapLegend from './MapLegend'
import { ALTYAPI_IKON, birlikIkonuGetir, facilityIkonuGetir, RENK, yerlesimIkonuGetir } from '../utils/tacticalIcons'
import { daireOlustur } from '../utils/geo'
import type { AltyapiDurumu, AltyapiDurumYaniti } from '../types/domain'

// "FAZ 6: 3B TAKTİKSEL HARİTA" ("2D react-leaflet yapısı C4ISR standartları
// için yetersiz" ihtiyacına karşı): zemin motoru WebGL tabanlı
// MapLibre GL'e (react-map-gl/maplibre) taşındı.
//
// STİL SEÇİMİ: `basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json`
// (CartoDB) tercih edilir — UZUN SÜREDİR yerleşik, yaygın olarak
// erişilebilir bir CDN altyapısına sahiptir ve API anahtarı GEREKTİRMEZ.
// Doğrulanmış özellikleri: style JSON -> TileJSON -> gerçek `.mvt` vektör
// karosu -> sprite -> glyph'ler, HEPSİ `Access-Control-Allow-Origin: *`
// ile 200 döner, filigran YOKTUR — hem gerçekten ücretsiz/anahtarsız HEM
// DE Esri'nin raster altlığının aksine (eğilince/pitch verilince
// bulanıklaşır) native VEKTÖR olduğundan 3B eğimde KESKİN kalır.
const KARANLIK_STIL = 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json'

// "SADECE TÜRKÇE ETİKET" KURALI: CartoDB temel haritası yer adlarını
// VERİ KAYNAĞININ KENDİ dilinde gösterir (ör. Yunan adaları için Yunanca
// yer/idari-bölge adları — bkz. "Αποκεντρωμένη Διοίκηση Αιγαίου" gibi
// etiketler) — bu, Türkiye odaklı bir taktiksel komuta sisteminde hem
// tutarsız hem de gereksiz bir görsel gürültüdür. Türkiye içi yer adları
// zaten kendi `yerlesim-il-etiket`/`yerlesim-ilce-etiket` katmanlarımızca
// (Neo4j Settlement verisinden, Türkçe) sağlanır — bu yüzden temel
// haritanın TÜM metin (symbol) katmanları, KENDİ etiket katmanlarımız
// HARİÇ, gizlenir (silinmez — `visibility: none`, stil yeniden
// yüklendiğinde/güncellendiğinde tutarlı kalması için `styledata`
// olayında tekrar uygulanır).
const KENDI_ETIKET_KATMAN_IDLERI = new Set(['yerlesim-il-etiket', 'yerlesim-ilce-etiket'])

function temelHaritaEtiketleriniGizle(harita: MaplibreMap): void {
  // Her katman KENDİ try/catch'i İÇİNDE işlenir: `setLayoutProperty` bir
  // katman için (ör. tanınmayan bir özellik/tip kombinasyonu) İSTİSNA
  // fırlatırsa, bu TEK katmanın hatası döngünün TAMAMINI durdurup SONRAKİ
  // (gizlenmesi gereken) katmanları atlanmış bırakmamalıdır.
  for (const katman of harita.getStyle()?.layers ?? []) {
    if (
      katman.type === 'symbol' &&
      'layout' in katman &&
      !!katman.layout &&
      'text-field' in katman.layout &&
      !KENDI_ETIKET_KATMAN_IDLERI.has(katman.id)
    ) {
      try {
        harita.setLayoutProperty(katman.id, 'visibility', 'none')
      } catch (hata) {
        console.error('Temel harita etiket katmanı gizlenemedi:', katman.id, hata)
      }
    }
  }
}

// `src.core.decision_engine.HASARLI_TESIS_DURUMLARI` ile AYNI değerler —
// bir tesisin `TaktikselRozet` rengini (kırmızı=hasarlı, yeşil=sağlam)
// belirlemek için burada da tekrarlanır (backend, filtreleme SEÇENEĞİ
// olarak `tum_tesisler`i durum ayrımı YAPMADAN döner — bkz. `api.py`).
const TESIS_HASARLI_DURUMLARI = ['Hasarlı', 'Yok Edildi']

const BASLANGIC_GORUNUM = {
  longitude: 35.2,
  latitude: 39.0,
  zoom: 5.3,
  pitch: 45,
  bearing: -12,
}

interface MapViewProps {
  durum: AltyapiDurumYaniti | null
}

/** Zemin katman: tam ekran, 3B (pitch/bearing) WebGL taktiksel durum haritası. */
export default function MapView({ durum }: MapViewProps) {
  const [katmanlar, setKatmanlar] = useState<KatmanGorunurlugu>(VARSAYILAN_KATMAN_GORUNURLUGU)
  // "DİNAMİK BOYUTLANDIRMA" ("birlikler/olaylar zoom yapıldıkça haritayı
  // boğmamalı" ilkesi): sadece zoom hareketi BİTTİĞİNDE
  // güncellenir (`onZoomEnd`) — her animasyon karesinde YÜZLERCE React
  // Marker'ı yeniden boyutlandırmak (sürekli `onMove`) gereksiz render
  // maliyeti getirirdi; komutan yakınlaştırmayı BIRAKTIĞI an boyut netleşir.
  const [zoom, setZoom] = useState(BASLANGIC_GORUNUM.zoom)

  function katmanDegistir(anahtar: keyof KatmanGorunurlugu) {
    setKatmanlar((k) => ({ ...k, [anahtar]: !k[anahtar] }))
  }

  // Kapalı yollar: bitiş koordinatı OLAN kayıtlar ÇİZGİ (LineString),
  // olmayanlar (bkz. `AltyapiDurumu` — sentetik bir tahmin ÜRETİLMEZ)
  // TEK bir nokta olarak ayrıştırılır.
  const { cizgiYollar, noktaYollar } = useMemo(() => {
    const cizgiler: AltyapiDurumu[] = []
    const noktalar: AltyapiDurumu[] = []
    for (const yol of durum?.kapali_yollar ?? []) {
      if (yol.bitis_enlem != null && yol.bitis_boylam != null) cizgiler.push(yol)
      else noktalar.push(yol)
    }
    return { cizgiYollar: cizgiler, noktaYollar: noktalar }
  }, [durum])

  const yolGeoJSON = useMemo(
    () => ({
      type: 'FeatureCollection' as const,
      features: cizgiYollar.map((yol) => ({
        type: 'Feature' as const,
        properties: { isim: yol.isim },
        geometry: {
          type: 'LineString' as const,
          coordinates: [
            [yol.boylam, yol.enlem],
            [yol.bitis_boylam as number, yol.bitis_enlem as number],
          ],
        },
      })),
    }),
    [cizgiYollar],
  )

  // Olay "etki alanı" (km) — MapLibre'nin native circle katmanı yarıçapı
  // PİKSEL cinsinden aldığından, GERÇEK coğrafi yarıçapı doğru göstermek
  // için bir Polygon (çokgen) üretilir (bkz. `utils/geo.daireOlustur`).
  const olayDaireleriGeoJSON = useMemo(
    () => ({
      type: 'FeatureCollection' as const,
      features: (durum?.kritik_olaylar ?? [])
        .filter((olay) => !!olay.etki_alani_km)
        .map((olay) => daireOlustur(olay.boylam, olay.enlem, olay.etki_alani_km as number)),
    }),
    [durum],
  )

  // Zoom arttıkça birlik ikonları büyür (0 => sadece küçük nokta, tam ikon YOK
  // — düşük zoom'da yüzlerce ikonun haritayı "boğmasını" önler).
  const birlikIkonGoster = zoom >= 6.5
  const birlikBoyutu = zoom < 8 ? 16 : zoom < 10 ? 20 : 24

  // "FAZ 8: SİVİL YERLEŞİM YERLERİ" DİNAMİK BOYUTLANDIRMA: 1057 yerleşim
  // (82 il + 975 ilçe) `aktif_birlikler`den (500) bile SAYICA FAZLA — aynı
  // "düşük zoom'da sadece nokta" ilkesi burada da geçerli, AMA il merkezleri
  // (nüfusça/stratejik önemce daha büyük, sadece 82 tane) ilçe merkezlerinden
  // DAHA DÜŞÜK bir zoom eşiğinde ikona geçer — ulusal görünümde önce SADECE
  // il merkezleri belirginleşir, komutan yakınlaştırdıkça ilçe detayı gelir
  // (bkz. "HİYERARŞİK YOL ÇİZİMİ" ile AYNI kademeli-detay felsefesi).
  const ilIkonGoster = zoom >= 5.5
  const ilceIkonGoster = zoom >= 7.5
  const yerlesimBoyutu = zoom < 8 ? 14 : zoom < 10 ? 18 : 22

  // "PERFORMANS DÜZELTMESİ" (kullanıcı tespiti — "tüm filtreler açıkken
  // yakınlaştırma/uzaklaştırma kasıyor"): KÖK SEBEP, yerleşim (1057) + tesis
  // (~3000) + birlik (500) katmanlarının HER NOKTASI için, ikon eşiğinin
  // ALTINDA bile ayrı bir react-map-gl `<Marker>` (yani gerçek bir DOM
  // elemanı, MapLibre tarafından her kamera karesinde CSS transform ile
  // yeniden konumlandırılır) monte ediliyor olmasıydı — ~4500 DOM elemanını
  // her zoom/pan hareketinde yeniden konumlandırmak ağırdır. ÇÖZÜM: bu üç
  // katmanın TAMAMI için TEK, WebGL tabanlı bir GeoJSON `circle` katmanı
  // (bkz. aşağıdaki `*NoktaGeoJSON` + `<Layer type="circle">` kullanımı —
  // `kapali-yollar`/`olay-daireleri`nin ZATEN kullandığı AYNI desen) HER
  // ZAMAN altta çizilir (GPU'da TEK çizim çağrısı, nokta sayısından
  // BAĞIMSIZ olarak ucuzdur); DOM `<Marker>` + ikon rozeti SADECE ilgili
  // zoom eşiği aşıldığında (komutan zaten yakınlaştırmışken, ki o an daha
  // az nokta görünür durumdadır) EK bir detay katmanı olarak render edilir
  // — ikon rozeti, altındaki küçük GL noktasını tamamen örttüğü için çift
  // çizim GÖRSEL OLARAK fark edilmez.
  const yerlesimNoktaGeoJSON = useMemo(
    () => ({
      type: 'FeatureCollection' as const,
      features: katmanlar.yerlesimBolgeleri
        ? (durum?.yerlesimler ?? []).map((y) => ({
            type: 'Feature' as const,
            // "isim" — aşağıdaki native GL metin (symbol) etiket katmanının
            // kaynağı (bkz. "YERLEŞİM ADI ETİKETLERİ" notu); ikon rozetleri
            // GİBİ ayrı bir React state/zoom-takibi GEREKTİRMEZ, MapLibre
            // kendi `minzoom`/collision-detection motoruyla halleder.
            properties: { ilMerkezi: y.yerlesim_tipi === 'Il Merkezi', isim: y.isim },
            geometry: { type: 'Point' as const, coordinates: [y.boylam, y.enlem] },
          }))
        : [],
    }),
    [durum, katmanlar.yerlesimBolgeleri],
  )

  const tesisNoktaGeoJSON = useMemo(
    () => ({
      type: 'FeatureCollection' as const,
      features: (durum?.tum_tesisler ?? [])
        .filter((t) => {
          if (t.tip === 'Havalimani') return katmanlar.havalimanlari
          if (t.tip === 'Liman') return katmanlar.limanlar
          if (t.tip === 'Hastane') return katmanlar.hastaneler
          if (t.tip === 'Askeri Us') return katmanlar.askeriUsler
          return true
        })
        .map((t) => ({
          type: 'Feature' as const,
          properties: { hasarli: !!t.durum && TESIS_HASARLI_DURUMLARI.includes(t.durum) },
          geometry: { type: 'Point' as const, coordinates: [t.boylam, t.enlem] },
        })),
    }),
    [durum, katmanlar.havalimanlari, katmanlar.limanlar, katmanlar.hastaneler, katmanlar.askeriUsler],
  )

  const birlikNoktaGeoJSON = useMemo(
    () => ({
      type: 'FeatureCollection' as const,
      features: katmanlar.birlikler
        ? (durum?.aktif_birlikler ?? []).map((b) => ({
            type: 'Feature' as const,
            properties: {},
            geometry: { type: 'Point' as const, coordinates: [b.boylam, b.enlem] },
          }))
        : [],
    }),
    [durum, katmanlar.birlikler],
  )

  // "SESSİZ SİYAH ZEMİN" RİSKİNE KARŞI KORUMA ("harita JSON'u başarılı
  // dönse bile zemin yok, noktalar uzay boşluğunda süzülüyor" riski): CSS
  // import'u VE container boyutlandırması KOD SEVİYESİNDE ZATEN doğrudur
  // (bkz. yukarıdaki import + `style={{width:'100%',height:'100%'}}`). Bu
  // proje boyunca tekrar tekrar uygulanan "asla sessizce yutma/başarısız
  // olma" ilkesiyle AYNI mantık
  // burada da geçerli: eğer harita GERÇEKTEN yüklenemiyorsa (ör. WebGL bu
  // tarayıcıda/oturumda KULLANILAMIYOR — MapLibre'nin TEK sert önkoşulu;
  // eski `react-leaflet` DOM/Canvas2D tabanlı olduğu için bu sınırlamayı
  // TAŞIMIYORDU), kullanıcı bunu SESSİZ bir siyah ekran yerine AÇIKÇA
  // görmelidir. `onError`/`onLoad` ile TAKİP edilir; harita makul bir
  // sürede (8 sn) yüklenmez VEYA bir hata fırlatırsa, aşağıdaki tanılayıcı
  // uyarı KESİN olarak gösterilir.
  const [haritaDurumu, setHaritaDurumu] = useState<'yukleniyor' | 'yuklendi' | 'hata'>('yukleniyor')
  const [haritaHatasi, setHaritaHatasi] = useState<string | null>(null)
  const haritaRef = useRef<MapRef>(null)

  useEffect(() => {
    if (haritaDurumu !== 'yukleniyor') return
    const zamanAsimi = setTimeout(() => {
      setHaritaDurumu((mevcut) => (mevcut === 'yukleniyor' ? 'hata' : mevcut))
    }, 8000)
    return () => clearTimeout(zamanAsimi)
  }, [haritaDurumu])

  return (
    <>
      {haritaDurumu === 'hata' && (
        <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center">
          <div className="pointer-events-auto flex max-w-md flex-col items-center gap-2 rounded-lg border border-crisis-500/40 bg-cmd-900/90 px-6 py-5 text-center backdrop-blur-md">
            <TriangleAlert size={22} className="text-crisis-400" />
            <div className="text-sm font-semibold text-ink-100">Harita zemini yüklenemedi</div>
            <div className="text-xs leading-relaxed text-ink-300">
              {haritaHatasi ??
                'MapLibre GL, tarayıcıda WebGL başlatamadı VEYA karo sunucusuna ulaşamadı. Tarayıcınızda WebGL\'in aktif olduğunu (chrome://gpu veya about:support) ve donanım hızlandırmanın AÇIK olduğunu doğrulayın; ardından sert yenileme (Ctrl+Shift+R) yapın.'}
            </div>
          </div>
        </div>
      )}
      <Map
        ref={haritaRef}
        initialViewState={BASLANGIC_GORUNUM}
        mapStyle={KARANLIK_STIL}
        style={{ width: '100%', height: '100%' }}
        maxPitch={70}
        onZoomEnd={(e: ViewStateChangeEvent) => setZoom(e.viewState.zoom)}
        onLoad={(e) => {
          setHaritaDurumu('yuklendi')
          temelHaritaEtiketleriniGizle(e.target)
        }}
        onStyleData={(e) => temelHaritaEtiketleriniGizle(e.target)}
        onError={(e: MaplibreErrorEvent) => {
          console.error('CRITICAL MAP ERROR:', e.error)
          setHaritaHatasi(e.error?.message ?? 'Bilinmeyen harita hatası.')
          setHaritaDurumu('hata')
        }}
      >
        <NavigationControl position="top-right" visualizePitch showZoom showCompass />

        {/* Olay etki alanları — yarı saydam dolgu + ince çizgi */}
        <Source id="olay-daireleri" type="geojson" data={olayDaireleriGeoJSON}>
          <Layer id="olay-daire-dolgu" type="fill" paint={{ 'fill-color': RENK.kriz, 'fill-opacity': 0.08 }} />
          <Layer
            id="olay-daire-cizgi"
            type="line"
            paint={{ 'line-color': RENK.kriz, 'line-width': 1, 'line-opacity': 0.35 }}
          />
        </Source>

        {/* Kapalı yollar — çizgi geometrisi (şeffaf/neon kırmızı, %35 opaklık) */}
        {katmanlar.yollar && (
          <Source id="kapali-yollar" type="geojson" data={yolGeoJSON}>
            <Layer
              id="kapali-yollar-cizgi"
              type="line"
              layout={{ 'line-cap': 'round', 'line-join': 'round' }}
              paint={{ 'line-color': RENK.kriz, 'line-width': 3, 'line-opacity': 0.35 }}
            />
          </Source>
        )}

        {/* Sivil yerleşim bölgeleri — GL taban noktası (bkz. yukarıdaki
            "PERFORMANS DÜZELTMESİ" notu): TÜMÜ, ikon eşiğinden BAĞIMSIZ. */}
        <Source id="yerlesim-noktalari" type="geojson" data={yerlesimNoktaGeoJSON}>
          <Layer
            id="yerlesim-nokta-katman"
            type="circle"
            paint={{
              'circle-radius': ['case', ['get', 'ilMerkezi'], 4, 2.5],
              'circle-color': RENK.civil,
              'circle-opacity': 0.9,
            }}
          />
          {/* "YERLEŞİM ADI ETİKETLERİ": eskiden yerleşim isimleri SADECE fare üzerine
              gelindiğinde (`title` tooltip) görünüyordu — profesyonel bir
              taktiksel haritada yer adları KALICI etiket olmalı. Native GL
              `symbol` katmanı kullanılır (React `<Marker>` DEĞİL) ki
              yüzlerce etiket EK DOM/state maliyeti getirmesin; MapLibre'nin
              kendi çakışma-önleme (collision) motoru komşu etiketleri
              otomatik gizler. İl/ilçe ayrımı, ikon rozetlerindeki AYNI
              kademeli-zoom eşiğini (`ilIkonGoster`/`ilceIkonGoster` — 5.5/
              7.5) `minzoom` ile birebir yansıtır. */}
          <Layer
            id="yerlesim-il-etiket"
            type="symbol"
            minzoom={5.5}
            filter={['==', ['get', 'ilMerkezi'], true]}
            layout={{
              'text-field': ['get', 'isim'],
              'text-font': ['Open Sans Regular', 'Arial Unicode MS Regular'],
              'text-size': 13,
              'text-offset': [0, 1.3],
              'text-anchor': 'top',
              'text-optional': true,
              'text-letter-spacing': 0.02,
            }}
            paint={{
              'text-color': '#d6d6f2',
              'text-halo-color': '#0a0e17',
              'text-halo-width': 1.4,
              'text-halo-blur': 0.4,
            }}
          />
          <Layer
            id="yerlesim-ilce-etiket"
            type="symbol"
            minzoom={7.5}
            filter={['==', ['get', 'ilMerkezi'], false]}
            layout={{
              'text-field': ['get', 'isim'],
              'text-font': ['Open Sans Regular', 'Arial Unicode MS Regular'],
              'text-size': 11,
              'text-offset': [0, 1.1],
              'text-anchor': 'top',
              'text-optional': true,
            }}
            paint={{
              'text-color': RENK.civil,
              'text-halo-color': '#0a0e17',
              'text-halo-width': 1.2,
              'text-halo-blur': 0.4,
            }}
          />
        </Source>

        {/* Sivil yerleşim bölgeleri (il/ilçe merkezleri) — ikon DETAYI, SADECE
            ilgili zoom eşiği aşıldığında (dinamik boyutlandırma). */}
        {katmanlar.yerlesimBolgeleri &&
          durum?.yerlesimler.map((yerlesim, i) => {
            const ilMerkeziMi = yerlesim.yerlesim_tipi === 'Il Merkezi'
            if (!(ilMerkeziMi ? ilIkonGoster : ilceIkonGoster)) return null
            const Ikon = yerlesimIkonuGetir(yerlesim.yerlesim_tipi)
            const baslik = `${yerlesim.isim} · ${yerlesim.yerlesim_tipi ?? 'Yerleşim'}${
              yerlesim.nufus ? ` · ${yerlesim.nufus.toLocaleString('tr-TR')} nüfus` : ''
            }`
            return (
              <Marker
                key={`yerlesim-${i}-${yerlesim.isim}`}
                longitude={yerlesim.boylam}
                latitude={yerlesim.enlem}
                anchor="center"
              >
                <TaktikselRozet
                  renk={RENK.civil}
                  boyutPx={ilMerkeziMi ? yerlesimBoyutu + 4 : yerlesimBoyutu}
                  baslik={baslik}
                >
                  <Ikon size={Math.round((ilMerkeziMi ? yerlesimBoyutu + 4 : yerlesimBoyutu) * 0.55)} color="#0a0e17" strokeWidth={2.5} />
                </TaktikselRozet>
              </Marker>
            )
          })}

        {/* Kapalı yollar — sadece nokta verisi olanlar */}
        {katmanlar.yollar &&
          noktaYollar.map((yol, i) => (
            <Marker key={`yol-${i}-${yol.isim}`} longitude={yol.boylam} latitude={yol.enlem} anchor="center">
              <TaktikselRozet renk={RENK.kriz} baslik={`${yol.isim} · ${yol.tip ?? 'Yol'} · KAPALI`}>
                <ALTYAPI_IKON size={13} color="#0a0e17" strokeWidth={2.5} />
              </TaktikselRozet>
            </Marker>
          ))}

        {/* Tesisler — GL taban noktası, durum rengiyle (yeşil=sağlam,
            kırmızı=hasarlı); filtre kutucukları GeoJSON üretiminde ZATEN
            uygulanmış (bkz. `tesisNoktaGeoJSON`). */}
        <Source id="tesis-noktalari" type="geojson" data={tesisNoktaGeoJSON}>
          <Layer
            id="tesis-nokta-katman"
            type="circle"
            paint={{
              'circle-radius': 3,
              'circle-color': ['case', ['get', 'hasarli'], RENK.kriz, RENK.ready],
              'circle-opacity': 0.9,
            }}
          />
        </Source>

        {/* Tesisler (hastane/havalimanı/liman/askeri üs) — BULGU DÜZELTMESİ:
            eskiden SADECE `hasarli_tesisler` çizilirdi, bu da aktif kriz
            yokken (çoğu zaman) haritanın TAMAMEN boş görünmesine yol açardı
            (bkz. `api.py`daki `_TUM_TESIS_LISTELEME_LIMITI` notu). Artık
            durum FARK ETMEKSİZİN tüm tesisler GL katmanında çizilir; ikon
            DETAYI SADECE ilgili zoom eşiği aşıldığında eklenir. */}
        {durum?.tum_tesisler.map((tesis, i) => {
          if (tesis.tip === 'Havalimani' && !katmanlar.havalimanlari) return null
          if (tesis.tip === 'Liman' && !katmanlar.limanlar) return null
          if (tesis.tip === 'Hastane' && !katmanlar.hastaneler) return null
          if (tesis.tip === 'Askeri Us' && !katmanlar.askeriUsler) return null
          if (!birlikIkonGoster) return null
          const Ikon = facilityIkonuGetir(tesis.tip)
          const hasarli = !!tesis.durum && TESIS_HASARLI_DURUMLARI.includes(tesis.durum)
          const renk = hasarli ? RENK.kriz : RENK.ready
          const baslik = `${tesis.isim} · ${tesis.tip ?? '—'} · ${tesis.durum ?? '—'}`
          return (
            <Marker
              key={`tesis-${i}-${tesis.id ?? tesis.isim}`}
              longitude={tesis.boylam}
              latitude={tesis.enlem}
              anchor="center"
            >
              <TaktikselRozet renk={renk} boyutPx={birlikBoyutu} baslik={baslik}>
                <Ikon size={Math.round(birlikBoyutu * 0.55)} color="#0a0e17" strokeWidth={2.5} />
              </TaktikselRozet>
            </Marker>
          )
        })}

        {/* Aktif birlikler — GL taban noktası. BULGU DÜZELTMESİ (kullanıcı
            tespiti — "mavi yıldızlı [Polis] konumlar için filtre yok"):
            filtre artık tip AYRIMI YAPMADAN `katmanlar.birlikler`e bağlı
            (bkz. `birlikNoktaGeoJSON` + `TacticalFilters`teki ilgili not). */}
        <Source id="birlik-noktalari" type="geojson" data={birlikNoktaGeoJSON}>
          <Layer
            id="birlik-nokta-katman"
            type="circle"
            paint={{ 'circle-radius': 2.5, 'circle-color': RENK.tactical, 'circle-opacity': 0.9 }}
          />
        </Source>

        {/* Aktif birlikler — ikon DETAYI, dinamik boyutlandırma (bkz. yukarıdaki not) */}
        {katmanlar.birlikler &&
          birlikIkonGoster &&
          durum?.aktif_birlikler.map((birlik, i) => {
            const Ikon = birlikIkonuGetir(birlik.tip)
            return (
              <Marker
                key={`birlik-${i}-${birlik.isim}`}
                longitude={birlik.boylam}
                latitude={birlik.enlem}
                anchor="center"
              >
                <TaktikselRozet
                  renk={RENK.tactical}
                  boyutPx={birlikBoyutu}
                  baslik={`${birlik.isim} · ${birlik.tip ?? '—'} · ${birlik.personel ?? '—'} personel`}
                >
                  <Ikon size={Math.round(birlikBoyutu * 0.55)} color="#0a0e17" strokeWidth={2.5} />
                </TaktikselRozet>
              </Marker>
            )
          })}

        {/* Kritik olaylar — nabız (pulse) noktası */}
        {durum?.kritik_olaylar.map((olay, i) => (
          <Marker key={`olay-${i}-${olay.isim}`} longitude={olay.boylam} latitude={olay.enlem} anchor="center">
            <div
              className="karavul-pulse-marker h-4 w-4 rounded-full bg-crisis-500 ring-2 ring-crisis-400/50"
              title={`${olay.isim} · ${olay.tip ?? '—'} · ${olay.siddet ?? '—'}`}
            />
          </Marker>
        ))}
      </Map>

      <TacticalFilters katmanlar={katmanlar} onDegistir={katmanDegistir} />
      <MapLegend />
    </>
  )
}

interface TaktikselRozetProps {
  renk: string
  baslik: string
  boyutPx?: number
  children: ReactNode
}

/** Tüm nokta işaretçilerinin (tesis/yol/birlik) paylaştığı ortak "rozet" çerçevesi. */
function TaktikselRozet({ renk, baslik, boyutPx = 22, children }: TaktikselRozetProps) {
  return (
    <div
      title={baslik}
      className="flex items-center justify-center rounded-full border-2 shadow-lg"
      style={{ width: boyutPx, height: boyutPx, backgroundColor: renk, borderColor: '#0a0e17aa' }}
    >
      {children}
    </div>
  )
}
