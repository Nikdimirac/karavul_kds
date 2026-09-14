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

const KARANLIK_STIL = 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json'

const KENDI_ETIKET_KATMAN_IDLERI = new Set(['yerlesim-il-etiket', 'yerlesim-ilce-etiket'])

function temelHaritaEtiketleriniGizle(harita: MaplibreMap): void {

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

export default function MapView({ durum }: MapViewProps) {
  const [katmanlar, setKatmanlar] = useState<KatmanGorunurlugu>(VARSAYILAN_KATMAN_GORUNURLUGU)

  const [zoom, setZoom] = useState(BASLANGIC_GORUNUM.zoom)

  function katmanDegistir(anahtar: keyof KatmanGorunurlugu) {
    setKatmanlar((k) => ({ ...k, [anahtar]: !k[anahtar] }))
  }

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

  const olayDaireleriGeoJSON = useMemo(
    () => ({
      type: 'FeatureCollection' as const,
      features: (durum?.kritik_olaylar ?? [])
        .filter((olay) => !!olay.etki_alani_km)
        .map((olay) => daireOlustur(olay.boylam, olay.enlem, olay.etki_alani_km as number)),
    }),
    [durum],
  )

  const birlikIkonGoster = zoom >= 6.5
  const birlikBoyutu = zoom < 8 ? 16 : zoom < 10 ? 20 : 24
  const ilIkonGoster = zoom >= 5.5
  const ilceIkonGoster = zoom >= 7.5
  const yerlesimBoyutu = zoom < 8 ? 14 : zoom < 10 ? 18 : 22

  const yerlesimNoktaGeoJSON = useMemo(
    () => ({
      type: 'FeatureCollection' as const,
      features: katmanlar.yerlesimBolgeleri
        ? (durum?.yerlesimler ?? []).map((y) => ({
            type: 'Feature' as const,
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
