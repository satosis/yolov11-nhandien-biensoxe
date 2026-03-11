"""
parking_hpc/test_bench.py
Self-Testing Module

Feeds a folder of sample images through the inference pipeline and reports:
  - Per-image: detected plate text, OCR confidence, inference time
  - Aggregate: Accuracy (vs ground-truth labels), Mean Inference Time, CPU Temperature

Usage:
    python parking_hpc/test_bench.py --samples ./test_samples --gt ./test_samples/labels.json

Folder layout expected:
    test_samples/
        plate_001.jpg
        plate_002.jpg
        face_001.jpg
        ...
        labels.json   ← {"plate_001.jpg": "51A12345", "face_001.jpg": "Nguyen Van A", ...}

If labels.json is absent, accuracy is skipped and only timing/temp are reported.
"""
import argparse
import json
import os
import time
import glob
import statistics
import logging

import cv2
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("test_bench")


# ── CPU temperature reader ────────────────────────────────────────────────────

def read_cpu_temp() -> float:
    """Read CPU temperature from sysfs (Linux). Returns °C or -1 if unavailable."""
    thermal_zones = glob.glob("/sys/class/thermal/thermal_zone*/temp")
    temps = []
    for path in thermal_zones:
        try:
            with open(path) as f:
                temps.append(int(f.read().strip()) / 1000.0)
        except Exception:
            pass
    return max(temps) if temps else -1.0


# ── Lazy model loader (reuse across images) ───────────────────────────────────

_plate_detector = None
_ocr_reader = None
_face_recog = None


def _get_plate_detector():
    global _plate_detector
    if _plate_detector is None:
        from parking_hpc.inference import PlateDetector
        _plate_detector = PlateDetector()
    return _plate_detector


def _get_ocr():
    global _ocr_reader
    if _ocr_reader is None:
        from parking_hpc.inference import OCRReader
        _ocr_reader = OCRReader()
    return _ocr_reader


def _get_face_recog():
    global _face_recog
    if _face_recog is None:
        from parking_hpc.inference import FaceRecognizer
        _face_recog = FaceRecognizer()
    return _face_recog


# ── Per-image inference ───────────────────────────────────────────────────────

def run_plate_inference(img: np.ndarray) -> tuple[str, float, float]:
    """Returns (plate_text, confidence, elapsed_ms)."""
    from parking_hpc.inference import enhance_plate
    t0 = time.perf_counter()
    detector = _get_plate_detector()
    ocr = _get_ocr()
    boxes = detector.detect(img)
    best_text, best_conf = "", 0.0
    for x1, y1, x2, y2, det_conf in boxes:
        crop = img[y1:y2, x1:x2]
        if crop.size == 0:
            continue
        enhanced = enhance_plate(crop)
        text, ocr_conf = ocr.read(enhanced)
        combined = ocr_conf * det_conf
        if combined > best_conf:
            best_conf = combined
            best_text = text
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return best_text, best_conf, elapsed_ms


def run_face_inference(img: np.ndarray) -> tuple[str, float, float]:
    """Returns (name, similarity, elapsed_ms)."""
    t0 = time.perf_counter()
    recog = _get_face_recog()
    name, sim = recog.identify(img)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    return name, sim, elapsed_ms


# ── Main bench ────────────────────────────────────────────────────────────────

def run_bench(samples_dir: str, gt_path: str | None, max_images: int = 20):
    image_paths = sorted(
        p for p in glob.glob(os.path.join(samples_dir, "*"))
        if p.lower().endswith((".jpg", ".jpeg", ".png"))
    )[:max_images]

    if not image_paths:
        logger.error("No images found in %s", samples_dir)
        return

    ground_truth: dict[str, str] = {}
    if gt_path and os.path.isfile(gt_path):
        with open(gt_path) as f:
            ground_truth = json.load(f)
        logger.info("Loaded %d ground-truth labels", len(ground_truth))
    else:
        logger.info("No labels.json — accuracy will be skipped")

    results = []
    temp_before = read_cpu_temp()
    logger.info("CPU temp before bench: %.1f°C", temp_before)
    logger.info("Running %d images…\n", len(image_paths))

    header = f"{'File':<30} {'Predicted':<15} {'GT':<15} {'Conf':>6} {'Time(ms)':>10} {'Match'}"
    print(header)
    print("-" * len(header))

    for path in image_paths:
        fname = os.path.basename(path)
        img = cv2.imread(path)
        if img is None:
            logger.warning("Cannot read %s — skipping", path)
            continue

        # Decide plate vs face by filename convention
        is_face = "face" in fname.lower()

        if is_face:
            pred, conf, elapsed = run_face_inference(img)
        else:
            pred, conf, elapsed = run_plate_inference(img)

        gt = ground_truth.get(fname, "")
        match = (pred.upper() == gt.upper()) if gt else None
        match_str = ("✓" if match else "✗") if match is not None else "—"

        results.append({
            "file": fname,
            "predicted": pred,
            "gt": gt,
            "conf": conf,
            "elapsed_ms": elapsed,
            "match": match,
        })
        print(f"{fname:<30} {pred:<15} {gt:<15} {conf:>6.3f} {elapsed:>10.1f} {match_str}")

    # ── Summary ───────────────────────────────────────────────────────────────
    temp_after = read_cpu_temp()
    times = [r["elapsed_ms"] for r in results]
    labeled = [r for r in results if r["match"] is not None]
    correct = sum(1 for r in labeled if r["match"])

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Images processed  : {len(results)}")
    print(f"  Mean inference    : {statistics.mean(times):.1f} ms")
    print(f"  Median inference  : {statistics.median(times):.1f} ms")
    print(f"  Min / Max         : {min(times):.1f} / {max(times):.1f} ms")
    if labeled:
        accuracy = correct / len(labeled) * 100
        print(f"  Accuracy          : {correct}/{len(labeled)} = {accuracy:.1f}%")
    else:
        print("  Accuracy          : N/A (no labels)")
    print(f"  CPU temp before   : {temp_before:.1f}°C")
    print(f"  CPU temp after    : {temp_after:.1f}°C")
    print(f"  Temp delta        : {temp_after - temp_before:+.1f}°C")
    print("=" * 60)

    # Save JSON report
    report_path = os.path.join(samples_dir, "bench_report.json")
    with open(report_path, "w") as f:
        json.dump({
            "results": results,
            "summary": {
                "count": len(results),
                "mean_ms": statistics.mean(times),
                "median_ms": statistics.median(times),
                "accuracy": (correct / len(labeled) * 100) if labeled else None,
                "temp_before": temp_before,
                "temp_after": temp_after,
            }
        }, f, indent=2, ensure_ascii=False)
    logger.info("Report saved → %s", report_path)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Parking HPC — inference test bench")
    parser.add_argument("--samples", default="./test_samples", help="Folder with test images")
    parser.add_argument("--gt", default=None, help="Path to labels.json (optional)")
    parser.add_argument("--max", type=int, default=20, help="Max images to process (default 20)")
    args = parser.parse_args()

    gt_file = args.gt or os.path.join(args.samples, "labels.json")
    run_bench(args.samples, gt_file if os.path.isfile(gt_file) else None, args.max)
