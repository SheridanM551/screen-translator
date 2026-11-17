"""Convert a local HuggingFace seq2seq checkpoint to CTranslate2 for app/translator_nmt.py.

Needs torch + transformers + ctranslate2, so run it from the throwaway conversion venv
(downloads/convert-venv), NOT the runtime .venv:

    downloads\\convert-venv\\Scripts\\python.exe -I tools\\convert_nmt.py SRC OUT int8 [copy_files...] [--marian-keep-pad]

How models/nmt/* were built (SRC = HF snapshot downloaded under downloads/hf/):
    opus-mt-ja-zh       shun89/opus-mt-ja-zh                         int8 source.spm target.spm --marian-keep-pad
    mt5-game-small      iryneko571/mt5-translation-ja_zh-game-small  int8 spiece.model
    mt5-small-ccmatrix  iryneko571/mt5-small-translation-ja_zh       int8 spiece.model
    mt5-game-large      iryneko571/mt5-translation-ja_zh-game-large  int8 spiece.model
                        (repo ships no spiece.model; its tokenizer.json is byte-identical to
                         game-small's, so game-small's spiece.model was copied in first)
    nllb-600m           JustFrederik/nllb-200-distilled-600M-ct2-int8 is already CT2: copy
                        model.bin, config.json, shared_vocabulary.txt, sentencepiece.bpe.model

--marian-keep-pad: stock CT2 drops Marian's <pad> token and starts the decoder from a zero
vector. That is right for the original OPUS-MT checkpoints, but shun89/opus-mt-ja-zh was
fine-tuned with a non-zero <pad> embedding (norm ~1.17), so the stock conversion outputs
garbage ("?", "!"). This keeps <pad> as the real decoder start token and forbids generating
it with a -1e4 logit bias (same as HF's bad_words_ids=[[pad]]). Output then matches HF.
"""
import sys
import time

import numpy as np
from ctranslate2.converters import transformers as ct2t


class MarianKeepPadLoader(ct2t.BartLoader):
    @property
    def architecture_name(self):
        return "MarianMTModel"

    def get_model_spec(self, model):
        model.config.normalize_before = False
        model.config.normalize_embedding = False
        spec = super().get_model_spec(model)
        proj = spec.decoder.projection
        bias = np.zeros(proj.weight.shape[0], dtype=np.float32)
        if proj.has_bias():
            b = proj.bias
            bias = (b.numpy() if hasattr(b, "numpy") else np.asarray(b)).astype(np.float32).copy()
        bias[model.config.pad_token_id] = -1e4
        proj.bias = bias
        return spec

    def set_config(self, config, model, tokenizer):
        config.eos_token = tokenizer.eos_token
        config.unk_token = tokenizer.unk_token
        config.decoder_start_token = tokenizer.pad_token


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 3:
        sys.exit(__doc__)
    src, out, quant, copy = args[0], args[1], args[2], args[3:]
    if "--marian-keep-pad" in sys.argv:
        ct2t._MODEL_LOADERS["MarianConfig"] = MarianKeepPadLoader()
    t = time.perf_counter()
    conv = ct2t.TransformersConverter(src, copy_files=copy or None, low_cpu_mem_usage=True)
    conv.convert(out, quantization=quant, force=True)
    print(f"converted {src} -> {out} ({quant}) in {time.perf_counter() - t:.1f}s")


if __name__ == "__main__":
    main()
