#!/usr/bin/env python3
"""Run local webcam people detection with YOLO and optional MQTT publish.

Useful when Frigate/RTSP pipeline is unstable and you want to verify the model
can still detect people directly from a laptop/PC webcam.
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any

import cv2
from ultralytics import YOLO

try:
    import paho.mqtt.client as mqtt
except Exception:  # pragma: no cover - optional dependency in some envs
    mqtt = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Webcam people detector")
    parser.add_argument("--model", default="models/yolo26n.pt", help="YOLO model path")
    parser.add_argument("--camera", type=int, default=0, help="OpenCV webcam index")
    parser.add_argument("--conf", type=float, default=0.35, help="Confidence threshold")
    parser.add_argument("--imgsz", type=int, default=640, help="Inference image size")
    parser.add_argument("--mqtt-host", default="", help="Optional MQTT host to publish count")
    parser.add_argument("--mqtt-port", type=int, default=1883, help="MQTT port")
    parser.add_argument(
        "--mqtt-topic",
        default="shed/state/people_count",
        help="MQTT topic for person count",
    )
    parser.add_argument("--publish-interval", type=float, default=1.0, help="MQTT publish interval")
    return parser.parse_args()


def build_mqtt_client(args: argparse.Namespace):
    if not args.mqtt_host or mqtt is None:
        return None
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.connect(args.mqtt_host, args.mqtt_port, 30)
    client.loop_start()
    return client


def detect_people(result: Any, conf: float) -> int:
    names = result.names
    count = 0
    if result.boxes is None:
        return 0
    for box in result.boxes:
        cls_idx = int(box.cls[0].item())
        score = float(box.conf[0].item())
        label = str(names.get(cls_idx, cls_idx)).lower()
        if label == "person" and score >= conf:
            count += 1
    return count


def main() -> None:
    args = parse_args()
    model = YOLO(args.model)
    cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open webcam index {args.camera}")

    mqtt_client = build_mqtt_client(args)
    last_publish = 0.0

    print("[webcam] Press 'q' to quit")
    while True:
        ok, frame = cap.read()
        if not ok:
            print("[webcam] Failed to read frame")
            break

        results = model.predict(source=frame, conf=args.conf, imgsz=args.imgsz, verbose=False)
        result = results[0]
        people_count = detect_people(result, args.conf)

        plotted = result.plot()
        cv2.putText(
            plotted,
            f"People: {people_count}",
            (12, 28),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.9,
            (0, 255, 255),
            2,
            cv2.LINE_AA,
        )
        cv2.imshow("Webcam People Counter", plotted)

        now = time.time()
        if mqtt_client and now - last_publish >= args.publish_interval:
            mqtt_client.publish(args.mqtt_topic, str(people_count), retain=True)
            mqtt_client.publish(
                "shed/state/webcam_people_debug",
                json.dumps({"people_count": people_count, "ts": now}),
                retain=False,
            )
            last_publish = now

        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()
    if mqtt_client:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()


if __name__ == "__main__":
    main()
