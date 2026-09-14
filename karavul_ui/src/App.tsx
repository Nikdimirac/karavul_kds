import { useCallback, useEffect, useState } from 'react'
import MapView from './components/MapView'
import TopBar from './components/TopBar'
import FloatingPanel from './components/FloatingPanel'
import { karavulApi } from './services/api'
import type { AltyapiDurumYaniti } from './types/domain'


const OTOMATIK_YENILEME_MS = 4_000

export default function App() {
  const [durum, setDurum] = useState<AltyapiDurumYaniti | null>(null)
  const [baglantiSaglikli, setBaglantiSaglikli] = useState(true)

  const durumuYenile = useCallback(() => {
    karavulApi
      .altyapiDurumu()
      .then((yanit) => {
        setDurum(yanit)
        setBaglantiSaglikli(true)
      })
      .catch(() => {
        setBaglantiSaglikli(false)
      })
  }, [])

  useEffect(() => {
    durumuYenile()
    const zamanlayici = setInterval(durumuYenile, OTOMATIK_YENILEME_MS)
    return () => clearInterval(zamanlayici)
  }, [durumuYenile])

  return (
    <div className="relative h-screen w-screen overflow-hidden bg-cmd-950">
      <div className="absolute inset-0 z-0">
        <MapView durum={durum} />
      </div>

    
      <div className="hud-topscrim pointer-events-none absolute inset-x-0 top-0 z-[5] h-36" />
      <div className="hud-vignette pointer-events-none absolute inset-0 z-[5]" />

      <TopBar durum={durum} baglantiSaglikli={baglantiSaglikli} />
      <FloatingPanel aktifOlaylar={durum?.kritik_olaylar ?? []} onVeriDegisti={durumuYenile} />
    </div>
  )
}
