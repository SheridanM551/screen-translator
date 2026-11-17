"""Thin ctypes wrapper around the Windows 11 Snipping Tool OCR engine (oneocr.dll).

The three runtime files (oneocr.dll, oneocr.onemodel, onnxruntime.dll) are copied
from the locally installed Snipping Tool by setup.ps1 into ./ocr. They are
Microsoft binaries: use them locally, do not redistribute them.
"""
from __future__ import annotations

import ctypes as C
import os
from dataclasses import dataclass, field

_MODEL_KEY = b'kj)TGtrK>f]b[Piow.gU+nC@s""""""4'


class _Img(C.Structure):
    _fields_ = [
        ("t", C.c_int32),      # 3 = BGRA 8-bit
        ("col", C.c_int32),
        ("row", C.c_int32),
        ("_unk", C.c_int32),
        ("step", C.c_int64),
        ("data", C.c_void_p),
    ]


class _BBox(C.Structure):
    _fields_ = [(n, C.c_float) for n in ("x1", "y1", "x2", "y2", "x3", "y3", "x4", "y4")]


@dataclass
class OcrLine:
    text: str
    # 4 corner points (x, y) in image pixels: top-left, top-right, bottom-right, bottom-left
    # as reported by the engine (for vertical text the engine's "reading frame" is rotated).
    poly: list[tuple[float, float]]
    conf: float  # mean word confidence
    words: list[tuple[str, tuple[float, float, float, float]]] = field(default_factory=list)

    @property
    def rect(self) -> tuple[float, float, float, float]:
        xs = [p[0] for p in self.poly]
        ys = [p[1] for p in self.poly]
        return min(xs), min(ys), max(xs), max(ys)


class OneOcr:
    def __init__(self, ocr_dir: str, max_lines: int = 1000):
        ocr_dir = os.path.abspath(ocr_dir)
        dll_path = os.path.join(ocr_dir, "oneocr.dll")
        model_path = os.path.join(ocr_dir, "oneocr.onemodel")
        if not os.path.exists(dll_path) or not os.path.exists(model_path):
            raise FileNotFoundError(f"oneocr files missing in {ocr_dir}; run setup.ps1")
        self._dll_dir_handle = os.add_dll_directory(ocr_dir)
        d = C.WinDLL(dll_path)
        self._d = d

        i64p = C.POINTER(C.c_int64)
        for name, args in {
            "CreateOcrInitOptions": [i64p],
            "OcrInitOptionsSetUseModelDelayLoad": [C.c_int64, C.c_char],
            "CreateOcrPipeline": [C.c_char_p, C.c_char_p, C.c_int64, i64p],
            "CreateOcrProcessOptions": [i64p],
            "OcrProcessOptionsSetMaxRecognitionLineCount": [C.c_int64, C.c_int64],
            "RunOcrPipeline": [C.c_int64, C.POINTER(_Img), C.c_int64, i64p],
            "GetImageAngle": [C.c_int64, C.POINTER(C.c_float)],
            "GetOcrLineCount": [C.c_int64, i64p],
            "GetOcrLine": [C.c_int64, C.c_int64, i64p],
            "GetOcrLineContent": [C.c_int64, C.POINTER(C.c_char_p)],
            "GetOcrLineBoundingBox": [C.c_int64, C.POINTER(C.POINTER(_BBox))],
            "GetOcrLineWordCount": [C.c_int64, i64p],
            "GetOcrWord": [C.c_int64, C.c_int64, i64p],
            "GetOcrWordContent": [C.c_int64, C.POINTER(C.c_char_p)],
            "GetOcrWordBoundingBox": [C.c_int64, C.POINTER(C.POINTER(_BBox))],
            "GetOcrWordConfidence": [C.c_int64, C.POINTER(C.c_float)],
            "ReleaseOcrResult": [C.c_int64],
        }.items():
            fn = getattr(d, name)
            fn.argtypes = args
            fn.restype = C.c_int64

        init = C.c_int64()
        self._check(d.CreateOcrInitOptions(C.byref(init)), "CreateOcrInitOptions")
        self._check(d.OcrInitOptionsSetUseModelDelayLoad(init, b"\x00"), "SetUseModelDelayLoad")
        pipe = C.c_int64()
        self._check(
            d.CreateOcrPipeline(model_path.encode("utf-8"), _MODEL_KEY, init, C.byref(pipe)),
            "CreateOcrPipeline",
        )
        opt = C.c_int64()
        self._check(d.CreateOcrProcessOptions(C.byref(opt)), "CreateOcrProcessOptions")
        self._check(d.OcrProcessOptionsSetMaxRecognitionLineCount(opt, max_lines), "SetMaxLines")
        self._init, self._pipe, self._opt = init, pipe, opt

    @staticmethod
    def _check(rc: int, what: str) -> None:
        if rc != 0:
            raise RuntimeError(f"oneocr {what} failed with code {rc}")

    def recognize_bgra(self, buf, width: int, height: int, stride: int | None = None,
                       want_words: bool = False) -> list[OcrLine]:
        """buf: any object exposing a writable/readable buffer of BGRA pixels
        (numpy array, bytes, bytearray, mss raw). Returns lines in image pixels."""
        d = self._d
        if isinstance(buf, (bytes, bytearray, memoryview)):
            cbuf = (C.c_char * len(buf)).from_buffer_copy(buf) if isinstance(buf, bytes) else \
                (C.c_char * len(buf)).from_buffer(buf)
            ptr = C.addressof(cbuf)
        else:  # numpy
            ptr = buf.ctypes.data
        img = _Img(3, width, height, 0, stride or width * 4, ptr)
        res = C.c_int64()
        self._check(d.RunOcrPipeline(self._pipe, C.byref(img), self._opt, C.byref(res)),
                    "RunOcrPipeline")
        try:
            n = C.c_int64()
            d.GetOcrLineCount(res, C.byref(n))
            out: list[OcrLine] = []
            for i in range(n.value):
                line = C.c_int64()
                if d.GetOcrLine(res, i, C.byref(line)) != 0 or not line.value:
                    continue
                txt = C.c_char_p()
                d.GetOcrLineContent(line, C.byref(txt))
                bb = C.POINTER(_BBox)()
                d.GetOcrLineBoundingBox(line, C.byref(bb))
                b = bb.contents
                poly = [(b.x1, b.y1), (b.x2, b.y2), (b.x3, b.y3), (b.x4, b.y4)]
                wc = C.c_int64()
                d.GetOcrLineWordCount(line, C.byref(wc))
                confs = []
                words = []
                for j in range(wc.value):
                    w = C.c_int64()
                    if d.GetOcrWord(line, j, C.byref(w)) != 0:
                        continue
                    cf = C.c_float()
                    d.GetOcrWordConfidence(w, C.byref(cf))
                    confs.append(cf.value)
                    if want_words:
                        wt = C.c_char_p()
                        d.GetOcrWordContent(w, C.byref(wt))
                        wb = C.POINTER(_BBox)()
                        d.GetOcrWordBoundingBox(w, C.byref(wb))
                        q = wb.contents
                        xs = (q.x1, q.x2, q.x3, q.x4)
                        ys = (q.y1, q.y2, q.y3, q.y4)
                        words.append(((wt.value or b"").decode("utf-8", "replace"),
                                      (min(xs), min(ys), max(xs), max(ys))))
                text = (txt.value or b"").decode("utf-8", "replace")
                conf = sum(confs) / len(confs) if confs else 0.0
                out.append(OcrLine(text, poly, conf, words))
            return out
        finally:
            d.ReleaseOcrResult(res)
