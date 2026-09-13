/**
 * KARAVUL — Taktiksel Komuta Merkezi
 * ===================================
 * `api.py` (bkz. proje kökü — FastAPI, `uvicorn api:app --reload --port
 * 8000` ile çalışır) ile konuşan TEK servis katmanı. Bu dosya DIŞINDA
 * hiçbir bileşen doğrudan `fetch` ÇAĞIRMAZ — backend URL'i/hata biçimi
 * DEĞİŞİRSE tek değişim noktası burasıdır.
 *
 * TABAN URL: `VITE_API_BASE_URL` ortam değişkeninden okunur (bkz.
 * `.env.example`); verilmezse `http://localhost:8000` varsayılır — `api.py`
 * ile AYNI portu (bkz. o dosyanın CORS izinli origin listesi, `vite.config.
 * ts`teki `server.port: 5173`).
 */

import type {
  AltyapiDurumYaniti,
  KrizRaporuYaniti,
  OlayCozumYaniti,
  SifirlamaYaniti,
} from '../types/domain'

const API_BASE_URL: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? 'http://localhost:8000'

/** `api.py`nin `HTTPException(detail=...)` biçimindeki hata gövdesi. */
interface ApiHataGovdesi {
  detail?: string
}

/**
 * TÜM API çağrılarının aynı hata sözleşmesini paylaşmasını sağlayan ortak
 * yardımcı: HTTP durumu 2xx DEĞİLSE, `api.py`nin döndürdüğü `detail` metnini
 * (varsa) içeren bir `Error` fırlatır — hiçbir hata SESSİZCE yutulmaz,
 * çağıran bileşen bunu kullanıcıya AÇIKÇA gösterir (bkz. `App.tsx`daki
 * hata durumu render'ı).
 */
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
      // Govde JSON degilse HTTP durum koduyla yetinilir — sessizce yutulmaz,
      // yine de bir Error firlatilacak (asagida).
    }
    throw new Error(detay)
  }

  return (await yanit.json()) as T
}

export const karavulApi = {
  /** POST /api/analyze-crisis */
  analizEt(raporMetni: string): Promise<KrizRaporuYaniti> {
    return istekAt<KrizRaporuYaniti>('/api/analyze-crisis', {
      method: 'POST',
      body: JSON.stringify({ rapor_metni: raporMetni }),
    })
  },

  /** GET /api/infrastructure/status */
  altyapiDurumu(): Promise<AltyapiDurumYaniti> {
    return istekAt<AltyapiDurumYaniti>('/api/infrastructure/status')
  },

  /** POST /api/reset-scenario */
  senaryoSifirla(): Promise<SifirlamaYaniti> {
    return istekAt<SifirlamaYaniti>('/api/reset-scenario', { method: 'POST' })
  },

  /**
   * POST /api/resolve-event/{olay_ismi} — TEK bir olayı isimle kapatır.
   * `encodeURIComponent`: olay isimleri Türkçe karakter/boşluk içerebilir
   * (ör. "Kahramanmaraş Depremi") — URL yoluna ÇIPLAK gömülürse istek
   * bozulur/yanlış eşleşir.
   */
  olayiKapat(olayIsmi: string): Promise<OlayCozumYaniti> {
    return istekAt<OlayCozumYaniti>(`/api/resolve-event/${encodeURIComponent(olayIsmi)}`, {
      method: 'POST',
    })
  },
}
