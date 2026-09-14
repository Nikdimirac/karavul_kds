import { AlertTriangle, Ban, Radio, Users } from 'lucide-react'
import MetricCard from './MetricCard'
import type { AltyapiDurumYaniti } from '../types/domain'

interface TopBarProps {
  durum: AltyapiDurumYaniti | null
  baglantiSaglikli: boolean
}

export default function TopBar({ durum, baglantiSaglikli }: TopBarProps) {
  return (
    <div className="pointer-events-none absolute inset-x-0 top-0 z-20 flex items-start justify-between p-4">
      <div className="pointer-events-auto relative flex items-center gap-3 overflow-hidden rounded-lg border border-cmd-border/80 bg-cmd-900/75 px-4 py-2.5 shadow-lg shadow-black/40 backdrop-blur-md">
        <span className="absolute inset-x-0 top-0 h-[2px] bg-gradient-to-r from-tactical-500/80 via-tactical-500/20 to-transparent" />
        <Radio size={19} className="text-tactical-400 drop-shadow-[0_0_6px_rgba(56,189,248,0.55)]" strokeWidth={2} />
        <div className="leading-none">
          <div className="font-display text-base font-bold tracking-[0.2em] text-ink-100">KARAVUL</div>
          <div className="mt-1 text-[10px] uppercase tracking-wider text-ink-500">
            Taktiksel Komuta Merkezi
          </div>
        </div>
        <span
          className={`ml-1 h-1.5 w-1.5 shrink-0 rounded-full ${
            baglantiSaglikli ? 'bg-ready-500 shadow-[0_0_6px_rgba(34,197,94,0.7)]' : 'animate-pulse bg-crisis-500'
          }`}
          title={baglantiSaglikli ? 'API bağlı' : 'API bağlantısı yok'}
        />
      </div>

      <div className="pointer-events-auto flex items-center gap-2.5">
        <MetricCard icon={AlertTriangle} label="Aktif Olay" value={durum?.kritik_olaylar.length ?? 0} vurgu="kriz" />
        <MetricCard icon={Ban} label="Kapalı Yol" value={durum?.kapali_yollar.length ?? 0} vurgu="kriz" />
        <MetricCard icon={Users} label="Görevdeki Birlik" value={durum?.aktif_birlikler.length ?? 0} vurgu="hazir" />
      </div>
    </div>
  )
}
