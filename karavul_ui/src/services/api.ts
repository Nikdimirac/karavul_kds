

import type {
  AltyapiDurumYaniti,
  KrizRaporuYaniti,
  OlayCozumYaniti,
  SifirlamaYaniti,
} from '../types/domain'

const API_BASE_URL: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? 'http://localhost:8000'

interface ApiHataGovdesi {
  detail?: string
}


async function istekAt<T>(yol: string, secenekler?: RequestInit): Promise<T> {
  let yanit: Response
  try {
    yanit = await fetch(`${API_BASE_URL}${yol}`, {
      headers: { 'Content-Type': 'application/json' },
      ...secenekler,
    })
  } catch (aglariHatasi) {
    throw new Error(
      `API'ye ulaşılamadı (${API_BASE_URL}) — sunucu çalışmıyor olabilir. ` +
        `Ayrıntı: ${aglariHatasi instanceof Error ? aglariHatasi.message : String(aglariHatasi)}`,
    )
  }

  if (!yanit.ok) {
    let detay = `HTTP ${yanit.status}`
    try {
      const govde = (await yanit.json()) as ApiHataGovdesi
      if (govde.detail) detay = govde.detail
    } catch {
    }
    throw new Error(detay)
  }

  return (await yanit.json()) as T
}

export const karavulApi = {
  analizEt(raporMetni: string): Promise<KrizRaporuYaniti> {
    return istekAt<KrizRaporuYaniti>('/api/analyze-crisis', {
      method: 'POST',
      body: JSON.stringify({ rapor_metni: raporMetni }),
    })
  },

  altyapiDurumu(): Promise<AltyapiDurumYaniti> {
    return istekAt<AltyapiDurumYaniti>('/api/infrastructure/status')
  },

  senaryoSifirla(): Promise<SifirlamaYaniti> {
    return istekAt<SifirlamaYaniti>('/api/reset-scenario', { method: 'POST' })
  },

  olayiKapat(olayIsmi: string): Promise<OlayCozumYaniti> {
    return istekAt<OlayCozumYaniti>(`/api/resolve-event/${encodeURIComponent(olayIsmi)}`, {
      method: 'POST',
    })
  },
}
