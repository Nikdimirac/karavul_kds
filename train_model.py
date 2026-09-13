# -*- coding: utf-8 -*-
"""
train_model.py
===============
Karavul C4ISR - Kurmay Zeka Fine-Tuning Betigi (Faz 8)

Neo4j Karar Motoru tarafindan onaylanmis 'karavul_dataset.jsonl' verisi
uzerinde, Unsloth + 4-bit QLoRA kullanarak Llama 3 8B Instruct modelini
yerel donanimda (RTX 4070 Ti, 12GB VRAM) fine-tune eder.

------------------------------------------------------------------------------
KURULUM (Kurucu'nun bir kere calistirmasi gereken pip komutlari)
------------------------------------------------------------------------------
# ONEMLI: Guncel unsloth (2026.x) + torchao 0.13.x, torch'un int1-int7 alt-byte
# tiplerini gerektirir; bunlar torch 2.6.0'da mevcuttur, torch 2.4.0'da DEGILDIR
# ("AttributeError: module 'torch' has no attribute 'int2'" hatasinin nedeni
# budur). Ayrica torch 2.6.0'in Inductor/triton kodu, triton-windows'un cok
# yeni surumleriyle de kirilir ("cannot import name 'AttrsDescriptor'").
# Asagidaki surumler bu ortamda dogrulanmis, birbiriyle uyumlu bir settir:
#
# 1) PyTorch 2.6.0 (cu124) - torchao/unsloth'un ihtiyac duydugu int1-int7 icerir
# pip install "torch==2.6.0" "torchvision==0.21.0" --index-url https://download.pytorch.org/whl/cu124
#
# 2) xformers - torch==2.6.0 ile tam eslesen surum
# pip install "xformers==0.0.29.post3" --index-url https://download.pytorch.org/whl/cu124
#
# 3) triton (Windows) - torch 2.6.0'in inductor kodunun bekledigi surume sabitle
#    (guncel triton-windows 3.8.x, torch 2.6.0 ile "AttrsDescriptor" hatasi verir)
# pip install "triton-windows==3.2.0.post21"
#
# 4) Unsloth (guncel, Windows/Linux)
# pip install unsloth
#
# 5) Egitim bagimliliklari
# pip install --no-deps trl peft accelerate bitsandbytes
# pip install datasets sentencepiece protobuf huggingface_hub
#
# NOT: Windows'ta bitsandbytes icin guncel bir wheel gerekebilir:
# pip install bitsandbytes --upgrade
------------------------------------------------------------------------------
"""
import os

# Bazi Windows/conda kurulumlarinda SSL_CERT_FILE, gercekte var olmayan bir
# yola isaret ediyor (ornek: "...\\envs\\<env>\\ssl\\cacert.pem" - dogrusu
# "...\\envs\\<env>\\Library\\ssl\\cacert.pem"). Bu durumda huggingface_hub/
# httpx model indirirken "FileNotFoundError" ile patlar. Kirikse certifi'nin
# kendi sertifika demetiyle degistir.
_ssl_cert_file = os.environ.get("SSL_CERT_FILE")
if not _ssl_cert_file or not os.path.isfile(_ssl_cert_file):
    import certifi
    os.environ["SSL_CERT_FILE"] = certifi.where()
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

import json
import re
from pathlib import Path

from datasets import Dataset
from unsloth import FastLanguageModel
from unsloth.chat_templates import get_chat_template
from trl import SFTTrainer
from transformers import TrainingArguments
import torch

# ==============================================================================
# 0) SABITLER / PARAMETRELER
# ==============================================================================

DATASET_PATH = "mlops/golden_dataset/golden_dataset.jsonl"
"""2026-09-11'DE DEĞİŞTİRİLDİ (kullanıcı onayıyla, Golden Dataset Faz 2
tamamlanınca): ESKİ `karavul_dataset.jsonl` (347 satır, war_gaming.py
çıktısı, `militarize()` ile emir-kipine çevrilen) YERİNE SADECE Golden
Dataset (1000 satır, `kaynak: golden_v1`, "KDS aktör değil danışman"
şablonuyla yazılmış) kullanılıyor — 1. QLoRA turunun üretim modelinden
KÖTÜ çıkmasının nedeni (bkz. `mlops/reports/aday_1_vs_uretim.md`,
"BULUNAMADI sadakati" farkı) kısmen bu eski verinin gürültüsüydü; bu tur
o gürültüyü TAMAMEN dışarıda bırakıyor. Eski dosya hâlâ diskte duruyor,
istenirse `mlops/orchestrator.py`nin otomatik döngüsü onu ayrıca besler."""
OUTPUT_LORA_DIR = "./karavul_kurmay_lora"

# 12GB VRAM icin Llama-3-8B-Instruct'in Unsloth 4-bit onceden nicelenmis surumu.
MODEL_NAME = "unsloth/llama-3-8b-Instruct-bnb-4bit"

MAX_SEQ_LENGTH = 2048
LOAD_IN_4BIT = True
DTYPE = None  # Unsloth otomatik tespit etsin (bf16 destekleniyorsa bf16, yoksa fp16)

SYSTEM_PROMPT = (
    "Sen Karavul Kriz ve Afet Yönetimi Karar Destek Sistemi'nin kurmay "
    "zekasısın. Bir EYLEM SİSTEMİ DEĞİLSİN — yetkili insan karar vericiye "
    "seçenek sunan bir DANIŞMANSIN. ASLA emir verme, ASLA bir eylemi kendin "
    "gerçekleştirdiğini iddia etme ('sevk ettim', 'söndürdüm' gibi). Uygun "
    "birlik/kaynak bulunamıyorsa bunu DÜRÜSTÇE belirt, ASLA uydurma. "
    "Yanıtını her zaman şu 4 başlık altında ver: DURUM SENTEZİ, HAREKET "
    "TARZLARI (ALPHA/BRAVO SEÇENEKLERİ), KRİTİK DARBOĞAZLAR, KARAR/ONAY "
    "NOKTASI."
)
"""2026-09-11'DE DEĞİŞTİRİLDİ: eski prompt ('...askeri lojistik emirleri
üret') 1. tur eğitiminin militarize() edilmiş war_gaming verisine
UYGUNDU, ama Golden Dataset'in "KDS aktör değil danışman" ilkesiyle
DOĞRUDAN ÇELİŞİYORDU (bkz. `mlops/golden_dataset/scenario_generator.py`
BÖLÜM 3) — artık eğitim verisi SADECE Golden Dataset olduğundan (bkz.
`DATASET_PATH`), sistem promptu o verinin gerçek üslubuyla (4 başlıklı
danışman formatı) eşleşecek şekilde güncellendi."""

# ==============================================================================
# 1) VERI ISLEME (PREPROCESSING) - JSONL -> ChatML
# ==============================================================================

# "Ezber" / rapor-dili kaliplarini net askeri emir kalibina ceviren regex
# tabanli donusum kurallari. Sirali olarak uygulanir (once en spesifik olanlar).
MILITARIZE_PATTERNS = [
    # "Bu birlik, KRİZ NOKTASINA 37.8 km uzaklıktadır ve rotası AÇIKTIR."
    #   -> "Hedefe 37.8 km mesafeden, mevcut güzergah üzerinden intikal edilecektir."
    (
        re.compile(
            r"Bu birlik,\s*KRİZ NOKTASINA\s*([\d.,]+)\s*km uzaklıktadır\s*"
            r"ve rotası AÇIKTIR\.?",
            re.IGNORECASE,
        ),
        r"Hedefe \1 km mesafeden, mevcut güzergah üzerinden intikal edilecektir.",
    ),
    # "... KRİZ NOKTASINA 28.2 km uzaklıktadır ve rotası AÇIKTIR."
    #   (birim adi cumlenin oznesi olarak zaten gecmisse)
    (
        re.compile(
            r"KRİZ NOKTASINA\s*([\d.,]+)\s*km uzaklıktadır\s*ve rotası AÇIKTIR\.?",
            re.IGNORECASE,
        ),
        r"hedefe \1 km mesafeden mevcut güzergah üzerinden intikal edecektir.",
    ),
    # Sadece "rotası AÇIKTIR" / "rotası AÇIK" gecen kalinti ifadeler
    (re.compile(r"rotası\s+AÇIKTIR\.?", re.IGNORECASE), "güzergah müsaittir, intikal onaylıdır."),
    (re.compile(r"rotası\s+AÇIK\b\.?", re.IGNORECASE), "güzergahı müsaittir"),

    # "... en uygun birlik olarak değerlendirilir." -> emir kipi
    (
        re.compile(r"en uygun birlik olarak değerlendirilir\.?", re.IGNORECASE),
        "İCRA BİRLİĞİ OLARAK GÖREVLENDİRİLMİŞTİR.",
    ),

    # Analiz/rapor dili ac giris kaliplari
    (re.compile(r"^Lojistik analiz:\s*", re.IGNORECASE), "EMİR: "),
    (re.compile(r"\bLojistik analiz:\s*", re.IGNORECASE), ""),

    # "BULUNAMADI — bu KRİZ NOKTASINA ... HİÇBİR birlik yok ..." erisim sorunu
    (
        re.compile(r"BULUNAMADI\s*[-—]\s*", re.IGNORECASE),
        "ERİŞİM SORUNU TESPİT EDİLDİ: ",
    ),

    # Cift bosluk / fazla noktalama temizligi
    (re.compile(r"[ \t]+"), " "),
    (re.compile(r"\n{2,}"), "\n"),
    (re.compile(r"\s+([.,])"), r"\1"),
]


def militarize(text: str) -> str:
    """Serbest/rapor uslubundaki oneri metnini net askeri emir formatina cevirir."""
    result = text.strip()
    for pattern, replacement in MILITARIZE_PATTERNS:
        result = pattern.sub(replacement, result)
    result = result.strip()
    if result and not result.endswith((".", "!", ":")):
        result += "."
    return result


def pick_best_suggestion(taktiksel_oneriler: list) -> str:
    """taktiksel_oneriler dizisindeki ilk ve en mantiksal oneriyi secer."""
    if not taktiksel_oneriler:
        return ""
    return taktiksel_oneriler[0]


def load_and_format_dataset(path: str) -> Dataset:
    """JSONL dosyasini okuyup ChatML (system/user/assistant) formatina cevirir."""
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"[UYARI] Satir {line_no} atlandi (bozuk JSON): {e}")
                continue

            rapor_metni = row.get("rapor_metni", "").strip()
            durum_ozeti = row.get("durum_ozeti", "").strip()
            taktiksel_oneriler = row.get("taktiksel_oneriler", [])

            if not rapor_metni or not durum_ozeti or not taktiksel_oneriler:
                print(f"[UYARI] Satir {line_no} atlandi (eksik alan).")
                continue

            user_content = f"{rapor_metni}\nİSTİHBARAT:\n{durum_ozeti}"

            ham_oneri = pick_best_suggestion(taktiksel_oneriler)
            # CANLI HATA DUZELTMESI (Golden Dataset, 2026-09-10): militarize()
            # "rotası AÇIKTIR" -> "intikal edilecektir", "en uygun birlik
            # olarak degerlendirilir" -> "ICRA BIRLIGI OLARAK GOREVLENDIRIL-
            # MISTIR" gibi EMIR KIPINE cevirir - bu tam olarak Altin Veri
            # Setinin YASAKLADIGI "KDS karar alir/emir verir" davranisini
            # ESKI (war_gaming.py) verisi icin tasarlanmis bu kod yolu
            # uzerinden GERI SOKAR. Golden kayitlar (bkz. generate_dataset.py)
            # "kaynak": "golden_v1" tasir ve zaten Kusursuz Yanit sablonuyla
            # (KDS sadece secenek sunar) yazildigindan militarize() ATLANIR.
            if row.get("kaynak", "").startswith("golden"):
                assistant_content = ham_oneri.strip()
            else:
                assistant_content = militarize(ham_oneri)

            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
                {"role": "assistant", "content": assistant_content},
            ]
            records.append({"messages": messages})

    print(f"[BILGI] Toplam {len(records)} egitim ornegi hazirlandi.")
    return Dataset.from_list(records)


# ==============================================================================
# 2) MODEL YUKLEME (Unsloth + 4-bit QLoRA)
# ==============================================================================

def load_model_and_tokenizer():
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=MODEL_NAME,
        max_seq_length=MAX_SEQ_LENGTH,
        dtype=DTYPE,
        load_in_4bit=LOAD_IN_4BIT,
    )

    # Llama-3 ChatML/Instruct sablonunu uygula (system/user/assistant destegi icin)
    tokenizer = get_chat_template(
        tokenizer,
        chat_template="llama-3",
        mapping={"role": "role", "content": "content", "user": "user", "assistant": "assistant"},
    )

    model = FastLanguageModel.get_peft_model(
        model,
        r=16,
        target_modules=[
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ],
        lora_alpha=16,
        lora_dropout=0,
        bias="none",
        use_gradient_checkpointing="unsloth",  # 12GB VRAM icin uzun-baglam bellek tasarrufu
        random_state=3407,
        use_rslora=False,
        loftq_config=None,
    )

    return model, tokenizer


# ==============================================================================
# 3) EGITIM (SFTTrainer)
# ==============================================================================

def formatting_prompts_func(tokenizer):
    def _fmt(examples):
        texts = [
            tokenizer.apply_chat_template(
                convo, tokenize=False, add_generation_prompt=False
            )
            for convo in examples["messages"]
        ]
        return {"text": texts}

    return _fmt


def main():
    if not Path(DATASET_PATH).exists():
        raise FileNotFoundError(f"Veri seti bulunamadi: {DATASET_PATH}")

    print("[1/4] Veri seti isleniyor (JSONL -> ChatML)...")
    dataset = load_and_format_dataset(DATASET_PATH)

    print("[2/4] Model ve tokenizer yukleniyor (4-bit QLoRA)...")
    model, tokenizer = load_model_and_tokenizer()

    print("[3/4] Veri seti ChatML metnine donusturuluyor...")
    dataset = dataset.map(
        formatting_prompts_func(tokenizer),
        batched=True,
    )

    training_args = TrainingArguments(
        per_device_train_batch_size=2,
        gradient_accumulation_steps=4,   # efektif batch size = 8, 12GB VRAM icin guvenli
        warmup_steps=10,
        num_train_epochs=3,              # Golden Dataset (1000 satir) icin de makul bir epoch sayisi
        learning_rate=2e-4,
        fp16=not torch.cuda.is_bf16_supported(),
        bf16=torch.cuda.is_bf16_supported(),
        logging_steps=5,
        optim="adamw_8bit",
        weight_decay=0.01,
        lr_scheduler_type="linear",
        seed=3407,
        output_dir="outputs",
        save_strategy="epoch",
        report_to="none",
    )

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=dataset,
        dataset_text_field="text",
        max_seq_length=MAX_SEQ_LENGTH,
        dataset_num_proc=2,
        packing=False,
        args=training_args,
    )

    print("[4/4] Egitim basliyor...")
    trainer.train()

    print(f"Egitim tamamlandi. LoRA adaptorleri kaydediliyor: {OUTPUT_LORA_DIR}")
    model.save_pretrained(OUTPUT_LORA_DIR)
    tokenizer.save_pretrained(OUTPUT_LORA_DIR)
    print("Bitti. Kurmay Zeka LoRA adaptorleri hazir.")


if __name__ == "__main__":
    main()
