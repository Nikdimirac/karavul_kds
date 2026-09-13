import type { LucideIcon } from 'lucide-react'

type Vurgu = 'notr' | 'kriz' | 'hazir'

const VURGU_STIL: Record<Vurgu, string> = {
  notr: 'text-ink-100',
  kriz: 'text-crisis-400',
  hazir: 'text-ready-400',
}

// Kart sol kenarındaki ince vurgu çubuğu — HUD panellerindeki tek renkli
// "durum şeridi" konvansiyonuna uyar, kartlar arasındaki tarama (scan)
// hiyerarşisini güçlendirir.
const VURGU_KENAR: Record<Vurgu, string> = {
  notr: 'bg-ink-500/60',
  kriz: 'bg-crisis-500',
  hazir: 'bg-ready-500',
}

interface MetricCardProps {
  icon: LucideIcon
  label: string
  value: number
  vurgu?: Vurgu
}

/** Tek bir taktiksel metrik kartı — ikon + değer + kısa etiket, açıklama YOK. */
export default function MetricCard({ icon: Icon, label, value, vurgu = 'notr' }: MetricCardProps) {
  return (
    <div className="relative flex items-center gap-3 overflow-hidden rounded-lg border border-cmd-border/80 bg-cmd-900/75 py-2 pl-3.5 pr-4 shadow-lg shadow-black/40 backdrop-blur-md">
      <span className={`absolute inset-y-0 left-0 w-[3px] ${VURGU_KENAR[vurgu]}`} />
      <Icon size={18} className={VURGU_STIL[vurgu]} strokeWidth={2} />
      <div className="leading-none">
        <div className={`font-display text-xl font-semibold tabular-nums ${VURGU_STIL[vurgu]}`}>{value}</div>
        <div className="mt-1 text-[10px] uppercase tracking-wider text-ink-500">{label}</div>
      </div>
    </div>
  )
}
