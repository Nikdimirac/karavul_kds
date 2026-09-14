
import os


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


DATASET_PATH = "mlops/golden_dataset/golden_dataset.jsonl"

OUTPUT_LORA_DIR = "./karavul_kurmay_lora"

MODEL_NAME = "unsloth/llama-3-8b-Instruct-bnb-4bit"

MAX_SEQ_LENGTH = 2048
LOAD_IN_4BIT = True
DTYPE = None  

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


MILITARIZE_PATTERNS = [
    
    (
        re.compile(
            r"Bu birlik,\s*KRİZ NOKTASINA\s*([\d.,]+)\s*km uzaklıktadır\s*"
            r"ve rotası AÇIKTIR\.?",
            re.IGNORECASE,
        ),
        r"Hedefe \1 km mesafeden, mevcut güzergah üzerinden intikal edilecektir.",
    ),
 
    (
        re.compile(
            r"KRİZ NOKTASINA\s*([\d.,]+)\s*km uzaklıktadır\s*ve rotası AÇIKTIR\.?",
            re.IGNORECASE,
        ),
        r"hedefe \1 km mesafeden mevcut güzergah üzerinden intikal edecektir.",
    ),
    (re.compile(r"rotası\s+AÇIKTIR\.?", re.IGNORECASE), "güzergah müsaittir, intikal onaylıdır."),
    (re.compile(r"rotası\s+AÇIK\b\.?", re.IGNORECASE), "güzergahı müsaittir"),

    (
        re.compile(r"en uygun birlik olarak değerlendirilir\.?", re.IGNORECASE),
        "İCRA BİRLİĞİ OLARAK GÖREVLENDİRİLMİŞTİR.",
    ),

    (re.compile(r"^Lojistik analiz:\s*", re.IGNORECASE), "EMİR: "),
    (re.compile(r"\bLojistik analiz:\s*", re.IGNORECASE), ""),

    (
        re.compile(r"BULUNAMADI\s*[-—]\s*", re.IGNORECASE),
        "ERİŞİM SORUNU TESPİT EDİLDİ: ",
    ),

    (re.compile(r"[ \t]+"), " "),
    (re.compile(r"\n{2,}"), "\n"),
    (re.compile(r"\s+([.,])"), r"\1"),
]


def militarize(text: str) -> str:
    result = text.strip()
    for pattern, replacement in MILITARIZE_PATTERNS:
        result = pattern.sub(replacement, result)
    result = result.strip()
    if result and not result.endswith((".", "!", ":")):
        result += "."
    return result


def pick_best_suggestion(taktiksel_oneriler: list) -> str:
    if not taktiksel_oneriler:
        return ""
    return taktiksel_oneriler[0]


def load_and_format_dataset(path: str) -> Dataset:
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



def load_model_and_tokenizer():
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=MODEL_NAME,
        max_seq_length=MAX_SEQ_LENGTH,
        dtype=DTYPE,
        load_in_4bit=LOAD_IN_4BIT,
    )

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
        use_gradient_checkpointing="unsloth", 
        random_state=3407,
        use_rslora=False,
        loftq_config=None,
    )

    return model, tokenizer


prompts_func(tokenizer):
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
        gradient_accumulation_steps=4,   
        warmup_steps=10,
        num_train_epochs=3,              
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
