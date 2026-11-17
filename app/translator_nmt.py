"""Non-LLM neural machine translation backend (Japanese -> Traditional Chinese, Taiwan).

Runs CTranslate2 seq2seq models (int8 on CPU by default) with plain SentencePiece
tokenization, then normalises the output with OpenCC ``s2twp`` (Simplified ->
Traditional, Taiwan phrasing).

Supported ``kind`` values and the files expected in ``model_dir``:

* ``"marian"`` - OPUS-MT / MarianMT converted with ct2-transformers-converter.
  Needs ``source.spm`` and ``target.spm`` next to ``model.bin``.
* ``"mt5"``    - mT5 fine-tunes that use the ``<-ja2zh->`` prompt prefix
  (iryneko571/mt5-translation-ja_zh-game-*). Needs ``spiece.model``.
* ``"nllb"``   - NLLB-200. Needs ``sentencepiece.bpe.model``.

Heavy dependencies (ctranslate2, sentencepiece, opencc) are imported lazily in
``__init__`` so importing this module is cheap.
"""

from __future__ import annotations

import os
import re
import threading

__all__ = ["NmtTranslator"]

_KINDS = ("marian", "mt5", "nllb")

# Prompt prefix the iryneko571 mT5 game models were trained with.
_MT5_PREFIX = "<-ja2zh-> "

# SentencePiece (nmt_nfkc) normalises full-width punctuation to ASCII; put the
# usual Chinese full-width forms back when they sit next to CJK text.
_CJK = r"　-ヿ㐀-䶿一-鿿豈-﫿＀-￯"
_HALF_TO_FULL = {"?": "？", "!": "！", ",": "，", ":": "：", ";": "；", "(": "（", ")": "）"}
_PUNCT_RE = re.compile(
    r"(?<=[" + _CJK + r"])\s*([?!,:;()])|([?!,:;()])\s*(?=[" + _CJK + r"])"
)
_CJK_SPACE_RE = re.compile(r"(?<=[" + _CJK + r"])\s+(?=[" + _CJK + r"])")
_ELLIPSIS_RE = re.compile(r"(?<=[" + _CJK + r"])\s*(?:\.{2,}|…+)|(?:\.{2,}|…+)\s*(?=[" + _CJK + r"])")
_PERIOD_RE = re.compile(r"(?<=[" + _CJK + r"])\.(?!\d)")
_QUOTES = str.maketrans({"“": "「", "”": "」", "‘": "『", "’": "』"})


def _fullwidth_punct(text: str) -> str:
    """Normalise ASCII punctuation in CJK output to Taiwan-style full-width forms."""
    text = _CJK_SPACE_RE.sub("", text)
    text = _ELLIPSIS_RE.sub("……", text)
    text = _PERIOD_RE.sub("。", text)
    text = _PUNCT_RE.sub(lambda m: _HALF_TO_FULL[m.group(1) or m.group(2)], text)
    # "?!" right after another full-width mark (e.g. "嗎？!" -> "嗎？！", "……?" -> "……？").
    text = re.sub(r"(?<=[？！…])([?!])", lambda m: _HALF_TO_FULL[m.group(1)], text)
    return text.translate(_QUOTES)


class NmtTranslator:
    """CTranslate2-based ja -> zh-TW translator.

    Calls are serialised with a lock, so one instance may be shared by threads,
    although it is intended to be driven by a single worker thread.
    """

    def __init__(
        self,
        model_dir: str,
        kind: str,
        device: str = "cpu",
        threads: int = 4,
        *,
        beam_size: int = 1,
        compute_type: str | None = None,
        max_decoding_length: int = 256,
        nllb_target: str = "zho_Hant",
        repetition_penalty: float | None = None,
    ) -> None:
        if kind not in _KINDS:
            raise ValueError(f"kind must be one of {_KINDS}, got {kind!r}")
        import ctranslate2
        import opencc
        import sentencepiece as spm

        self.kind = kind
        self.device = device
        self.beam_size = max(1, int(beam_size))
        self.max_decoding_length = max_decoding_length
        self.nllb_target = nllb_target
        if repetition_penalty is None:
            # The mT5 model cards recommend 1.4; it stops "啊啊啊啊..." loops.
            repetition_penalty = 1.4 if kind == "mt5" else 1.0
        self.repetition_penalty = repetition_penalty

        if compute_type is None:
            if device == "cpu":
                compute_type = "int8"
            else:
                # mT5 on CUDA: bf16 degenerates ("deg deg deg ...", esp. beam > 1) and
                # fp16 drifts on mt5-large; int8_float32 is correct and ~as fast.
                compute_type = "int8_float32" if kind == "mt5" else "int8_float16"
        self.compute_type = compute_type

        def sp_load(name: str):
            path = os.path.join(model_dir, name)
            if not os.path.isfile(path):
                raise FileNotFoundError(f"{kind} model needs {path}")
            return spm.SentencePieceProcessor(model_file=path)

        if kind == "marian":
            self._sp_src = sp_load("source.spm")
            self._sp_tgt = sp_load("target.spm")
        elif kind == "mt5":
            self._sp_src = self._sp_tgt = sp_load("spiece.model")
        else:
            self._sp_src = self._sp_tgt = sp_load("sentencepiece.bpe.model")

        self._translator = ctranslate2.Translator(
            model_dir,
            device=device,
            compute_type=compute_type,
            inter_threads=1,
            intra_threads=threads if device == "cpu" else 0,
        )
        self._cc = opencc.OpenCC("s2twp")  # resolves to the bundled s2twp.json
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ helpers
    def _encode(self, text: str) -> list[str]:
        if self.kind == "mt5":
            return self._sp_src.encode(_MT5_PREFIX + text, out_type=str) + ["</s>"]
        tokens = self._sp_src.encode(text, out_type=str) + ["</s>"]
        if self.kind == "nllb":
            tokens = ["jpn_Jpan"] + tokens
        return tokens

    def _decode(self, tokens: list[str]) -> str:
        if self.kind == "nllb" and tokens and tokens[0] == self.nllb_target:
            tokens = tokens[1:]
        tokens = [t for t in tokens if t not in ("</s>", "<pad>", "<s>", "<unk>")]
        text = self._sp_tgt.decode(tokens)
        return _fullwidth_punct(self._cc.convert(text.strip()))

    # --------------------------------------------------------------------- API
    def translate_batch(self, texts: list[str]) -> list[str]:
        """Translate Japanese strings; returns Traditional Chinese (Taiwan)."""
        out = [""] * len(texts)
        idx = [i for i, t in enumerate(texts) if t and t.strip()]
        if not idx:
            return out
        source = [self._encode(texts[i].strip()) for i in idx]
        kwargs = dict(
            beam_size=self.beam_size,
            max_decoding_length=self.max_decoding_length,
            repetition_penalty=self.repetition_penalty,
            max_batch_size=32,
        )
        if self.kind == "nllb":
            kwargs["target_prefix"] = [[self.nllb_target]] * len(source)
        with self._lock:
            results = self._translator.translate_batch(source, **kwargs)
        for i, res in zip(idx, results):
            out[i] = self._decode(res.hypotheses[0])
        return out

    def translate(self, text: str) -> str:
        return self.translate_batch([text])[0]
