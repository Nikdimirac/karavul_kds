import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'

// KARAVUL — Taktiksel Komuta Merkezi Arayüzü.
// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
  },
  // "SESSİZ SİYAH ZEMİN" KÖK NEDENİ (kullanıcı talebi — Kurucu tespiti:
  // "harita JSON'u başarılı dönse bile zemin yok"): CSS import'u VE
  // container boyutlandırması ZATEN doğruydu (bkz. `MapView.tsx`) — GERÇEK
  // sorun Vite'ın dep-optimizer'ının (esbuild ön-paketleme) `maplibre-gl`i
  // işlerken onun DAHİLİ Web Worker'ını (vektör karo AYRIŞTIRMA işini ana
  // thread DIŞINDA yapan `maplibre-gl-worker.mjs`) BOZMASIYDI — dev sunucu
  // logunda AÇIKÇA görüldü: "The file does not exist at .../maplibre-gl-
  // worker.mjs ... Try adding it to optimizeDeps.exclude". Sonuç: stil/
  // sprite/glyph (ana thread'de) SORUNSUZ yükleniyordu (bu yüzden "JSON
  // başarılı dönüyor" gözlemi DOĞRUYDU) ama GERÇEK vektör karo verisi hiç
  // AYRIŞTIRILAMIYORDU — canvas boş/siyah kalıyordu, React `<Marker>`
  // noktaları ise (worker'a bağımlı OLMADIĞI için) normal render oluyordu
  // — TAM OLARAK tarif edilen "uzay boşluğunda süzülen noktalar" budur.
  // `maplibre-gl`i dep-optimizer'dan HARİÇ TUTMAK (worker'ı KENDİ native
  // ESM yükleme mekanizmasıyla, bozulmadan çalıştırması için) standart/
  // bilinen çözümdür.
  optimizeDeps: {
    exclude: ['maplibre-gl'],
  },
})
