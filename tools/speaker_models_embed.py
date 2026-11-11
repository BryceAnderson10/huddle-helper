"""Fingerprint the cached pieces of each kept recording with other speaker models.

Writes lab/cache_models/<model>/<meeting>.json/.npz in the layout final_lab.py reads."""
import json
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import sherpa_onnx

from huddle_helper.audio import SAMPLE_RATE
from huddle_helper.audio.buffer import read_audio

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "lab" / "models" / "speaker"
CACHE = ROOT / "lab" / "cache"
OUT = ROOT / "lab" / "cache_models"


def main(only: list[str]) -> None:
    metas = sorted(CACHE.glob("*.json"))
    audio = {m.stem: read_audio(ROOT / "lab" / "src" / m.stem / "audio" / "system.flac") for m in metas}
    for model in sorted(MODELS.glob("*.onnx")):
        if only and model.stem not in only:
            continue
        dest = OUT / model.stem
        dest.mkdir(parents=True, exist_ok=True)
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model), num_threads=4)
        ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        for m in metas:
            if (dest / (m.stem + ".npz")).exists():
                continue
            info = json.loads(m.read_text(encoding="utf-8"))
            old = dict(np.load(m.with_suffix(".npz")))
            a = audio[m.stem]
            n = len(old["system_weights"])
            vecs, missing, t0 = [None] * n, 0, time.perf_counter()
            for p in info["pieces"]["system"]:
                if p["vec"] < 0:
                    continue
                clip = a[p["start_ms"] * SAMPLE_RATE // 1000:p["end_ms"] * SAMPLE_RATE // 1000].astype(np.float32)
                st = ex.create_stream()
                st.accept_waveform(SAMPLE_RATE, clip)
                st.input_finished()
                v = np.asarray(ex.compute(st), dtype=np.float32) if ex.is_ready(st) else None
                if v is None or not np.linalg.norm(v):
                    missing += 1
                    v = np.zeros(ex.dim, np.float32)
                else:
                    v = v / np.linalg.norm(v)
                vecs[p["vec"]] = v
            secs = time.perf_counter() - t0
            np.savez(dest / (m.stem + ".npz"), system_vecs=np.stack(vecs), system_weights=old["system_weights"],
                     system_idx=old["system_idx"])
            info["timings_s"] = {"embed_pieces": round(secs, 2)}
            info["model"] = model.stem
            (dest / m.name).write_text(json.dumps(info, indent=1), encoding="utf-8")
            print(f"{model.stem} | {m.stem}: {n} pieces, {missing} without a fingerprint, dim {ex.dim}, "
                  f"{secs:.1f} s for {float(old['system_weights'].sum()):.0f} s of speech", flush=True)
    # the shipped model, as cached by final_lab.py embed (1 thread)
    base = OUT / "wespeaker_en_voxceleb_resnet34_LM"
    base.mkdir(parents=True, exist_ok=True)
    for m in metas:
        for f in (m, m.with_suffix(".npz")):
            if not (base / f.name).exists():
                shutil.copy(f, base / f.name)


if __name__ == "__main__":
    main(sys.argv[1:])
