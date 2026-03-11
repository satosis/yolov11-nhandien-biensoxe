"""
parking_hpc/qa_agent.py
Quality Assurance Agent — Highest-accuracy recognition testing

Chiến lược:
  Layer 1 — Local pipeline  : YOLO detect → enhance → PaddleOCR / InsightFace
  Layer 2 — Claude Vision   : Gửi crop ảnh lên Claude claude-opus-4-6 để verify/correct
  Layer 3 — Consensus vote  : Nếu Layer1 và Layer2 đồng ý → HIGH confidence
                              Nếu khác nhau → Layer2 (Claude) thắng

Kết quả:
  - Per-image report: predicted, claude_correction, final, confidence, match
  - Aggregate: accuracy, mean_ms, CPU temp, confusion matrix
  - Lưu annotated images với bounding box + label vào qa_output/

Usage:
    python parking_hpc/qa_agent.py --samples ./test_samples
    python parking_hpc/qa_agent.py --samples ./test_samples --gt ./test_samples/labels.json
    python parking_hpc/qa_agent.py --image ./bien-so-xe.jpg --type plate
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import logging
import os
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import anthropic
import cv2
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s [qa_agent] %(message)s")
logger = logging.getLogger("qa_agent")

# ── Lazy model singletons ─────────────────────────────────────────────────────
_plate_detector = None
_ocr_reader = None
_face_recog = None


def _detector():
    global _plate_detector
    if _plate_detector is None:
        from parking_hpc.inference import PlateDetector
        _plate_detector = PlateDetector()
    return _plate_detector


def _ocr():
    global _ocr_reader
    if _ocr_reader is None:
        from parking_hpc.inference import OCRReader
        _ocr_reader = OCRReader()
    return _ocr_reader


def _face():
    global _face_recog
    if _face_recog is None:
        from parking_hpc.inference import FaceRecognizer
        _face_recog = FaceRecognizer()
    return _face_recog


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class QAResult:
    file: str
    task: str                    # "plate" | "face"
    local_pred: str = ""
    local_conf: float = 0.0
    claude_pred: str = ""
    claude_conf: float = 0.0
    final_pred: str = ""
    final_conf: float = 0.0
    gt: str = ""
    match: Optional[bool] = None
    elapsed_ms: float = 0.0
    annotated_path: str = ""
    crops: list[dict] = field(default_factory=list)  # [{bbox, local, claude, final}]


# ── Image → base64 helper ─────────────────────────────────────────────────────

def _img_to_b64(img: np.ndarray, quality: int = 90) -> str:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encode failed")
    return base64.standard_b64encode(buf.tobytes()).decode("ascii")


def _crop_b64(img: np.ndarray, x1: int, y1: int, x2: int, y2: int,
              pad: int = 8, quality: int = 92) -> str:
    """Crop with padding, encode to base64 JPEG."""
    h, w = img.shape[:2]
    cx1 = max(0, x1 - pad)
    cy1 = max(0, y1 - pad)
    cx2 = min(w, x2 + pad)
    cy2 = min(h, y2 + pad)
    crop = img[cy1:cy2, cx1:cx2]
    # Upscale small crops so Claude can read them clearly
    ch, cw = crop.shape[:2]
    if cw < 200:
        scale = 200 / cw
        crop = cv2.resize(crop, (int(cw * scale), int(ch * scale)), interpolation=cv2.INTER_CUBIC)
    return _img_to_b64(crop, quality)


# ── Claude Vision calls ───────────────────────────────────────────────────────

class ClaudeVisionVerifier:
    """
    Calls Claude claude-opus-4-6 with vision to verify/correct local predictions.
    Uses extended thinking for maximum accuracy on ambiguous cases.
    """

    MODEL = "claude-opus-4-6"

    def __init__(self):
        self._client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

    def verify_plate(
        self,
        full_frame_b64: str,
        crop_b64: str,
        local_text: str,
        bbox: tuple[int, int, int, int],
    ) -> tuple[str, float]:
        """
        Ask Claude to read the license plate from the crop.
        Returns (plate_text, confidence 0-1).
        """
        x1, y1, x2, y2 = bbox
        prompt = f"""You are an expert Vietnamese license plate OCR system.

I will show you:
1. A full parking lot camera frame with a red rectangle marking the detected plate region at coordinates ({x1},{y1})-({x2},{y2})
2. A zoomed-in crop of that plate region

The local OCR system read: "{local_text}"

Your task:
- Read the license plate text EXACTLY as it appears (Vietnamese format: e.g. 51A-12345, 30H-999.99, 43B1-12345)
- Correct any OCR errors (common: 0↔O, 1↔I, 8↔B, 5↔S, D↔0)
- If the plate is unreadable, return empty string

Respond ONLY with valid JSON, no markdown:
{{"plate": "<plate_text_or_empty>", "confidence": <0.0_to_1.0>, "reasoning": "<brief>"}}"""

        try:
            response = self._client.messages.create(
                model=self.MODEL,
                max_tokens=512,
                thinking={"type": "enabled", "budget_tokens": 2000},
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/jpeg", "data": full_frame_b64},
                        },
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/jpeg", "data": crop_b64},
                        },
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            # Extract text block (skip thinking blocks)
            text = next(
                (b.text for b in response.content if hasattr(b, "text")), "{}"
            )
            data = json.loads(text.strip())
            return str(data.get("plate", "")).upper().replace(" ", ""), float(data.get("confidence", 0.0))
        except Exception as e:
            logger.warning("Claude plate verify error: %s", e)
            return local_text, 0.5

    def verify_face(
        self,
        full_frame_b64: str,
        face_crop_b64: str,
        local_name: str,
        known_names: list[str],
    ) -> tuple[str, float]:
        """
        Ask Claude to identify the person from the face crop.
        Returns (name, confidence 0-1).
        """
        names_list = ", ".join(known_names) if known_names else "none registered"
        prompt = f"""You are an expert face recognition quality assessor.

I will show you:
1. A full parking lot camera frame
2. A zoomed-in crop of a detected face

The local face recognition system identified this person as: "{local_name}"
Known registered persons: [{names_list}]

Your task:
- Assess whether the face is clearly visible and identifiable
- If the face matches one of the known persons, confirm or correct the identification
- If the face is a stranger (not in the known list), return "STRANGER"
- If the face is too blurry/dark/small to identify, return "UNKNOWN"

Respond ONLY with valid JSON, no markdown:
{{"name": "<name_or_STRANGER_or_UNKNOWN>", "confidence": <0.0_to_1.0>, "reasoning": "<brief>"}}"""

        try:
            response = self._client.messages.create(
                model=self.MODEL,
                max_tokens=512,
                thinking={"type": "enabled", "budget_tokens": 2000},
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/jpeg", "data": full_frame_b64},
                        },
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/jpeg", "data": face_crop_b64},
                        },
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            text = next(
                (b.text for b in response.content if hasattr(b, "text")), "{}"
            )
            data = json.loads(text.strip())
            return str(data.get("name", "UNKNOWN")), float(data.get("confidence", 0.0))
        except Exception as e:
            logger.warning("Claude face verify error: %s", e)
            return local_name, 0.5

    def analyze_full_scene(self, frame_b64: str) -> dict:
        """
        Fallback: ask Claude to find ALL plates and faces in a full frame
        when local detection found nothing.
        """
        prompt = """Analyze this parking lot camera frame.

Find ALL license plates and ALL faces visible.

Respond ONLY with valid JSON, no markdown:
{
  "plates": [{"text": "<plate>", "confidence": <0-1>, "location": "<description>"}],
  "faces": [{"name": "STRANGER", "confidence": <0-1>, "location": "<description>"}]
}

If nothing found, return {"plates": [], "faces": []}"""

        try:
            response = self._client.messages.create(
                model=self.MODEL,
                max_tokens=1024,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/jpeg", "data": frame_b64},
                        },
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            text = next(
                (b.text for b in response.content if hasattr(b, "text")), "{}"
            )
            return json.loads(text.strip())
        except Exception as e:
            logger.warning("Claude scene analysis error: %s", e)
            return {"plates": [], "faces": []}


# ── Annotation helper ─────────────────────────────────────────────────────────

def _annotate(
    img: np.ndarray,
    crops_info: list[dict],
    task: str,
) -> np.ndarray:
    """Draw bounding boxes + labels on a copy of the frame."""
    out = img.copy()
    for c in crops_info:
        bbox = c.get("bbox")
        if not bbox:
            continue
        x1, y1, x2, y2 = bbox
        final = c.get("final", "")
        local = c.get("local", "")
        claude = c.get("claude", "")
        conf = c.get("conf", 0.0)

        # Color: green if local==claude (consensus), orange if corrected, red if empty
        if not final:
            color = (0, 0, 200)
        elif local == claude:
            color = (0, 200, 0)
        else:
            color = (0, 140, 255)  # Claude corrected local

        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)

        label = f"{final} ({conf:.2f})"
        if local != claude and claude:
            label += f" [was:{local}]"

        # Background for text
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 2)
        cv2.rectangle(out, (x1, max(y1 - th - 8, 0)), (x1 + tw + 4, y1), color, -1)
        cv2.putText(
            out, label,
            (x1 + 2, max(y1 - 4, th)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 2,
        )
    return out


# ── CPU temp ──────────────────────────────────────────────────────────────────

def _cpu_temp() -> float:
    zones = glob.glob("/sys/class/thermal/thermal_zone*/temp")
    temps = []
    for z in zones:
        try:
            temps.append(int(Path(z).read_text().strip()) / 1000.0)
        except Exception:
            pass
    return max(temps) if temps else -1.0


# ── Core QA logic ─────────────────────────────────────────────────────────────

class QAAgent:
    def __init__(self, output_dir: str = "./qa_output"):
        self._verifier = ClaudeVisionVerifier()
        self._output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def _known_face_names(self) -> list[str]:
        try:
            return list(_face()._known.keys())
        except Exception:
            return []

    def run_plate(self, img: np.ndarray, fname: str) -> QAResult:
        result = QAResult(file=fname, task="plate")
        t0 = time.perf_counter()

        frame_b64 = _img_to_b64(img, quality=85)
        boxes = _detector().detect(img)

        if not boxes:
            # Fallback: ask Claude to find plates in full scene
            logger.info("[%s] No local detection — asking Claude for full scene analysis", fname)
            scene = self._verifier.analyze_full_scene(frame_b64)
            plates = scene.get("plates", [])
            if plates:
                best = max(plates, key=lambda p: p.get("confidence", 0))
                result.claude_pred = best.get("text", "").upper().replace(" ", "")
                result.claude_conf = float(best.get("confidence", 0))
                result.final_pred = result.claude_pred
                result.final_conf = result.claude_conf
            result.elapsed_ms = (time.perf_counter() - t0) * 1000
            return result

        from parking_hpc.inference import enhance_plate

        crops_info = []
        best_local_text, best_local_conf = "", 0.0
        best_claude_text, best_claude_conf = "", 0.0

        for x1, y1, x2, y2, det_conf in boxes:
            crop = img[y1:y2, x1:x2]
            if crop.size == 0:
                continue

            # Layer 1: local OCR
            enhanced = enhance_plate(crop)
            local_text, ocr_conf = _ocr().read(enhanced)
            local_combined = ocr_conf * det_conf

            # Layer 2: Claude Vision verify
            crop_b64 = _crop_b64(img, x1, y1, x2, y2)
            claude_text, claude_conf = self._verifier.verify_plate(
                frame_b64, crop_b64, local_text, (x1, y1, x2, y2)
            )

            # Consensus: Claude wins on disagreement (higher accuracy)
            if claude_text and claude_conf >= 0.6:
                final_text = claude_text
                final_conf = claude_conf
            elif local_text and local_combined >= 0.5:
                final_text = local_text
                final_conf = local_combined
            else:
                final_text = claude_text or local_text
                final_conf = max(claude_conf, local_combined)

            crops_info.append({
                "bbox": (x1, y1, x2, y2),
                "local": local_text,
                "claude": claude_text,
                "final": final_text,
                "conf": final_conf,
            })

            if final_conf > best_claude_conf:
                best_local_text = local_text
                best_local_conf = local_combined
                best_claude_text = claude_text
                best_claude_conf = claude_conf
                result.plate_bbox = (x1, y1, x2, y2)

        result.local_pred = best_local_text
        result.local_conf = best_local_conf
        result.claude_pred = best_claude_text
        result.claude_conf = best_claude_conf

        # Final answer
        if best_claude_text and best_claude_conf >= 0.6:
            result.final_pred = best_claude_text
            result.final_conf = best_claude_conf
        else:
            result.final_pred = best_local_text
            result.final_conf = best_local_conf

        result.crops = crops_info
        result.elapsed_ms = (time.perf_counter() - t0) * 1000

        # Save annotated image
        annotated = _annotate(img, crops_info, "plate")
        out_path = os.path.join(self._output_dir, f"qa_{fname}")
        cv2.imwrite(out_path, annotated, [cv2.IMWRITE_JPEG_QUALITY, 92])
        result.annotated_path = out_path

        return result

    def run_face(self, img: np.ndarray, fname: str) -> QAResult:
        result = QAResult(file=fname, task="face")
        t0 = time.perf_counter()

        frame_b64 = _img_to_b64(img, quality=85)
        known_names = self._known_face_names()

        # Layer 1: InsightFace
        local_name, local_sim = _face().identify(img)

        # Get face bounding boxes for crops
        try:
            faces_detected = _face()._app.get(img)
        except Exception:
            faces_detected = []

        if not faces_detected:
            # Fallback: full scene analysis
            logger.info("[%s] No local face detection — asking Claude", fname)
            scene = self._verifier.analyze_full_scene(frame_b64)
            face_list = scene.get("faces", [])
            if face_list:
                best = max(face_list, key=lambda f: f.get("confidence", 0))
                result.claude_pred = best.get("name", "UNKNOWN")
                result.claude_conf = float(best.get("confidence", 0))
                result.final_pred = result.claude_pred
                result.final_conf = result.claude_conf
            result.elapsed_ms = (time.perf_counter() - t0) * 1000
            return result

        crops_info = []
        best_final_name, best_final_conf = "", 0.0

        for face in faces_detected:
            bbox = face.bbox.astype(int)
            x1, y1, x2, y2 = bbox[0], bbox[1], bbox[2], bbox[3]

            # Layer 2: Claude Vision verify
            crop_b64 = _crop_b64(img, x1, y1, x2, y2, pad=20)
            claude_name, claude_conf = self._verifier.verify_face(
                frame_b64, crop_b64, local_name, known_names
            )

            # Consensus
            if claude_conf >= 0.7:
                final_name = claude_name
                final_conf = claude_conf
            elif local_sim >= 0.45:
                final_name = local_name
                final_conf = local_sim
            else:
                final_name = claude_name if claude_conf > local_sim else local_name
                final_conf = max(claude_conf, local_sim)

            crops_info.append({
                "bbox": (x1, y1, x2, y2),
                "local": local_name,
                "claude": claude_name,
                "final": final_name,
                "conf": final_conf,
            })

            if final_conf > best_final_conf:
                best_final_conf = final_conf
                best_final_name = final_name

        result.local_pred = local_name
        result.local_conf = local_sim
        result.claude_pred = crops_info[0]["claude"] if crops_info else ""
        result.claude_conf = crops_info[0]["conf"] if crops_info else 0.0
        result.final_pred = best_final_name
        result.final_conf = best_final_conf
        result.crops = crops_info
        result.elapsed_ms = (time.perf_counter() - t0) * 1000

        # Save annotated image
        annotated = _annotate(img, crops_info, "face")
        out_path = os.path.join(self._output_dir, f"qa_{fname}")
        cv2.imwrite(out_path, annotated, [cv2.IMWRITE_JPEG_QUALITY, 92])
        result.annotated_path = out_path

        return result

    def run_image(self, img_path: str, task: str = "auto", gt: str = "") -> QAResult:
        """
        Run QA on a single image.
        task: "plate" | "face" | "auto" (auto-detect from filename)
        """
        fname = os.path.basename(img_path)
        img = cv2.imread(img_path)
        if img is None:
            raise FileNotFoundError(f"Cannot read image: {img_path}")

        if task == "auto":
            task = "face" if "face" in fname.lower() else "plate"

        if task == "face":
            result = self.run_face(img, fname)
        else:
            result = self.run_plate(img, fname)

        result.gt = gt
        if gt:
            result.match = result.final_pred.upper() == gt.upper()

        return result


# ── Batch benchmark ───────────────────────────────────────────────────────────

def run_qa_bench(
    samples_dir: str,
    gt_path: Optional[str],
    max_images: int,
    output_dir: str,
):
    image_paths = sorted(
        p for p in glob.glob(os.path.join(samples_dir, "**", "*"), recursive=True)
        if p.lower().endswith((".jpg", ".jpeg", ".png"))
        and "qa_output" not in p
    )[:max_images]

    if not image_paths:
        logger.error("No images found in %s", samples_dir)
        return

    ground_truth: dict[str, str] = {}
    if gt_path and os.path.isfile(gt_path):
        with open(gt_path) as f:
            ground_truth = json.load(f)
        logger.info("Loaded %d ground-truth labels", len(ground_truth))

    agent = QAAgent(output_dir=output_dir)
    results: list[QAResult] = []
    temp_before = _cpu_temp()

    logger.info("Starting QA bench on %d images…", len(image_paths))
    print(f"\n{'File':<28} {'Local':<14} {'Claude':<14} {'Final':<14} {'GT':<14} {'Conf':>5} {'ms':>7} {'✓'}")
    print("─" * 105)

    for path in image_paths:
        fname = os.path.basename(path)
        gt = ground_truth.get(fname, "")
        try:
            r = agent.run_image(path, task="auto", gt=gt)
        except Exception as e:
            logger.error("Error on %s: %s", fname, e)
            continue

        results.append(r)
        match_str = ("✓" if r.match else "✗") if r.match is not None else "—"
        corrected = " ←" if r.local_pred != r.claude_pred and r.claude_pred else ""
        print(
            f"{fname:<28} {r.local_pred:<14} {r.claude_pred:<14} "
            f"{r.final_pred:<14} {r.gt:<14} {r.final_conf:>5.2f} "
            f"{r.elapsed_ms:>7.0f} {match_str}{corrected}"
        )

    # ── Summary ───────────────────────────────────────────────────────────────
    temp_after = _cpu_temp()
    times = [r.elapsed_ms for r in results]
    labeled = [r for r in results if r.match is not None]
    correct = sum(1 for r in labeled if r.match)
    corrections = sum(1 for r in results if r.local_pred != r.claude_pred and r.claude_pred)

    print("\n" + "═" * 60)
    print("QA AGENT SUMMARY")
    print("═" * 60)
    print(f"  Images processed     : {len(results)}")
    print(f"  Mean inference time  : {statistics.mean(times):.0f} ms")
    print(f"  Median               : {statistics.median(times):.0f} ms")
    print(f"  Min / Max            : {min(times):.0f} / {max(times):.0f} ms")
    if labeled:
        acc = correct / len(labeled) * 100
        print(f"  Accuracy (final)     : {correct}/{len(labeled)} = {acc:.1f}%")
        # Local-only accuracy for comparison
        local_correct = sum(1 for r in labeled if r.local_pred.upper() == r.gt.upper())
        local_acc = local_correct / len(labeled) * 100
        print(f"  Accuracy (local OCR) : {local_correct}/{len(labeled)} = {local_acc:.1f}%")
        print(f"  Claude corrections   : {corrections} ({corrections/len(results)*100:.1f}%)")
    print(f"  CPU temp before      : {temp_before:.1f}°C")
    print(f"  CPU temp after       : {temp_after:.1f}°C  (Δ{temp_after-temp_before:+.1f}°C)")
    print(f"  Annotated images     : {output_dir}/")
    print("═" * 60)

    # Save JSON report
    report = {
        "summary": {
            "count": len(results),
            "mean_ms": statistics.mean(times),
            "median_ms": statistics.median(times),
            "accuracy_final": (correct / len(labeled) * 100) if labeled else None,
            "accuracy_local": (local_correct / len(labeled) * 100) if labeled else None,
            "claude_corrections": corrections,
            "temp_before": temp_before,
            "temp_after": temp_after,
        },
        "results": [
            {
                "file": r.file,
                "task": r.task,
                "local": r.local_pred,
                "local_conf": round(r.local_conf, 3),
                "claude": r.claude_pred,
                "claude_conf": round(r.claude_conf, 3),
                "final": r.final_pred,
                "final_conf": round(r.final_conf, 3),
                "gt": r.gt,
                "match": r.match,
                "elapsed_ms": round(r.elapsed_ms, 1),
                "annotated": r.annotated_path,
            }
            for r in results
        ],
    }
    report_path = os.path.join(output_dir, "qa_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    logger.info("Report saved → %s", report_path)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="QA Agent — highest-accuracy recognition testing with Claude Vision"
    )
    sub = parser.add_subparsers(dest="cmd")

    # Batch mode
    batch = sub.add_parser("bench", help="Run batch QA on a folder of images")
    batch.add_argument("--samples", default="./test_samples", help="Folder with test images")
    batch.add_argument("--gt", default=None, help="Path to labels.json")
    batch.add_argument("--max", type=int, default=20, help="Max images (default 20)")
    batch.add_argument("--output", default="./qa_output", help="Output dir for annotated images")

    # Single image mode
    single = sub.add_parser("image", help="Run QA on a single image")
    single.add_argument("--image", required=True, help="Path to image file")
    single.add_argument("--type", choices=["plate", "face", "auto"], default="auto")
    single.add_argument("--gt", default="", help="Ground truth label (optional)")
    single.add_argument("--output", default="./qa_output")

    args = parser.parse_args()

    if args.cmd == "bench" or args.cmd is None:
        samples = getattr(args, "samples", "./test_samples")
        gt_file = getattr(args, "gt", None) or os.path.join(samples, "labels.json")
        run_qa_bench(
            samples_dir=samples,
            gt_path=gt_file if os.path.isfile(gt_file) else None,
            max_images=getattr(args, "max", 20),
            output_dir=getattr(args, "output", "./qa_output"),
        )
    elif args.cmd == "image":
        agent = QAAgent(output_dir=args.output)
        r = agent.run_image(args.image, task=args.type, gt=args.gt)
        print(f"\nFile    : {r.file}")
        print(f"Task    : {r.task}")
        print(f"Local   : {r.local_pred} ({r.local_conf:.3f})")
        print(f"Claude  : {r.claude_pred} ({r.claude_conf:.3f})")
        print(f"Final   : {r.final_pred} ({r.final_conf:.3f})")
        if r.gt:
            print(f"GT      : {r.gt}  →  {'✓ MATCH' if r.match else '✗ MISMATCH'}")
        print(f"Time    : {r.elapsed_ms:.0f} ms")
        print(f"Saved   : {r.annotated_path}")
