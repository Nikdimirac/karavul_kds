# KARAVUL — "Sürekli Öğrenme" MLOps Boru Hattı

Bu paket, `karavul-kurmay` modelinin zamanla daha fazla senaryo görerek
kendini geliştirmesini **yarı-otomatik** hale getirir: veri üretimi, eğitim,
merge/quantize ve değerlendirme **tamamen otomatiktir**; canlıya geçiş
**her zaman insan onayı ister**.

## Neden "yarı-otomatik" (tam otonom değil)?

Bu projenin kendi geçmişinde (bkz. proje kökü `AI_MEMORY.md` §4) bir
fine-tuning/merge turu, KISA testlerde fark edilmeyen ama gerçek kullanımda
modeli sonsuz tekrara (`"AAAA ALELELE"`) sokan sessiz bir regresyon
(`rope_theta` alanının kaybolması) üretmişti. Otomatik/insansız bir canlıya-
alma adımı, aynı sınıf bir hatanın bir **sunum ortasında** fark edilmeden
canlıya sızmasını yapısal olarak mümkün kılardı. Bu yüzden:

- `orchestrator.py` **hiçbir zaman** `karavul-kurmay` (üretim) Ollama
  etiketine yazmaz — en fazla ayrı bir `karavul-kurmay-candidate` etiketine
  kadar gider.
- Canlıya geçiş **sadece** `promote_candidate.py` ile, interaktif bir
  "EVET" onayından sonra olur.
- `promote_candidate.py`, geçişten önce mevcut üretim modelini otomatik
  yedekler (`karavul-kurmay-backup-<UTC zaman damgası>`) — tek komutla geri
  alınabilir.

Ayrıca kullanıcı talebiyle **hiçbir Windows Görev Zamanlayıcı kaydı
eklenmedi** — tüm döngü elle tetiklenir.

## Bir turun akışı

```
python -m mlops.orchestrator
   │
   ├─ [A] api.py / Neo4j / Ollama ayakta mı? (değilse İPTAL, sessizce atlanmaz)
   ├─ [B] war_gaming.py ile N yeni GEÇERLİ senaryo üret → karavul_dataset.jsonl'e ekle
   ├─ [C] Son eğitimden bu yana yeterince yeni örnek birikti mi? (eşik: config.YENI_ORNEK_ESIGI)
   │        Hayır → dur (bu NORMAL bir çıktı, hata değil)
   ├─ [D] GPU gerçekten boşta mı? (nvidia-smi) — Kurucu'nun GPU'sunu (oyun vb.) ASLA bölmez
   │        Hayır → dur (veri toplama ZATEN tamamlandı, kayıp yok)
   ├─ [E] train_model.py (conda:karavul) → test_merge_infer.py (rope_theta düzeltmesi OTOMATİK)
   │        → ollama create ile F16 içe aktarma → q4_K_M requantize → 'karavul-kurmay-candidate'
   ├─ [F] eval_harness.py: aday vs üretim, bilinen regresyon sınıflarına karşı otomatik kontrol
   └─ [G] state.json + CYCLE_LOG.md güncellenir, "sıradaki adım senin" özeti basılır
```

## Komutlar

```bash
# Bir tur çalıştır (varsayılan: 20 yeni senaryo hedefi, eşik dolarsa eğit)
python -m mlops.orchestrator

# Bu turda daha çok veri topla
python -m mlops.orchestrator --senaryo-sayisi 50

# SADECE veri topla, eğitimi hiç deneme (GPU'yu hiç meşgul etmez)
python -m mlops.orchestrator --sadece-veri

# Eşik dolmamış olsa bile eğitimi zorla tetikle
python -m mlops.orchestrator --egitimi-zorla

# Herhangi iki etiketi manuel karşılaştır (ör. iki farklı aday sürümü)
python -m mlops.eval_harness --etiket karavul-kurmay-candidate --karsilastir karavul-kurmay

# Değerlendirmeyi okuduktan SONRA, adayı canlıya al (insan onaylı gate)
python -m mlops.promote_candidate
```

## Değerlendirme neyi kontrol eder?

`eval_harness.py`, proje kökü `AI_MEMORY.md` §7'deki 4 "demo-güvenli" gerçek
senaryoyu + 3 sentetik "MÜDAHALE EDECEK BİRLİK: BULUNAMADI" köşe durumunu
(Lojistik/Tahliye/Sevk) hem adaya hem üretime karşı çalıştırır ve şunları
otomatik arar:

| Kontrol | Hangi geçmiş hatayı yakalar |
|---|---|
| Boş değil | Motorun hiç öneri üretememesi |
| Tekrar döngüsü yok | `rope_theta` kaynaklı "AAAA ALELELE" sonsuz tekrarı |
| Türkçe kilidi | Modelin İngilizceye kayması |
| BULUNAMADI sadakati | **Bu oturumda eklenen kural** — uygun birlik yokken isim uydurma |

Bu kontroller **öznel kaliteyi puanlamaz** — sadece mekanik/nesnel
regresyonları arar. Rapor, `mlops/reports/` altına yazılır; nihai "canlıya
alınsın mı" kararı her zaman insana aittir.

## Dosyalar

| Dosya | Görev |
|---|---|
| `config.py` | Merkezi ayarlar (yollar, etiketler, eşikler) |
| `state.py` | `state.json` okuma/yazma (turlar arası hafıza) |
| `gpu_check.py` | `nvidia-smi` tabanlı "GPU boşta mı?" kontrolü |
| `eval_harness.py` | Aday/üretim karşılaştırmalı otomatik değerlendirme |
| `orchestrator.py` | Bir turun TAMAMINI yöneten ana script |
| `promote_candidate.py` | **TEK** canlıya-yazan adım (insan onaylı) |
| `state.json`, `CYCLE_LOG.md`, `reports/` | Çalışma zamanı çıktıları — **git'e girmez** (bkz. `.gitignore`) |

## Sınırlar / bilinçli kapsam dışı bırakmalar

- Otomatik zamanlama (cron/Görev Zamanlayıcı) **yok** — kullanıcı tercihiyle.
- `eval_harness.py` bir dil modelinin çıktısını "iyi" diye onaylamaz, sadece
  bilinen hata sınıflarını arar — insan incelemesinin yerine GEÇMEZ.
- Eğitim adımı (`train_model.py`) tek bir GPU'yu (RTX 4070 Ti, Kurucu'nun
  diğer uygulamalarıyla paylaşılan) uzun süre meşgul eder — bu yüzden `[D]`
  adımı GPU boşta değilse eğitimi ATLAR, ZORLAMAZ.
