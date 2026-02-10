
import os
import time
import logging
import threading
import requests

logger = logging.getLogger("door_controller")

class DoorController:
    def __init__(self):
        # Relay control for garage door
        self.RELAY_TYPE = "tuya_local"  # Options: gpio, tasmota, http, tuya_local
        
        # GPIO Config
        self.RELAY_GPIO_PIN = 11  # Orange Pi 4 Pro (Physical Pin 11)
        
        # Tasmota/HTTP Config
        self.RELAY_HTTP_URL = ""

        # Tuya Local Config (Get these from Tuya IoT Platform or tinytuya scan)
        self.TUYA_DEVICE_ID = "PLACEHOLDER_DEVICE_ID"
        self.TUYA_LOCAL_KEY = "PLACEHOLDER_LOCAL_KEY"
        self.TUYA_IP = "192.168.1.PROPER_IP" # Optional but recommended for speed
        
        self.door_state_lock = threading.Lock()
        self.door_state = "closed" # open, closed, opening, closing
        self.mqtt_client = None # To be set by main app
        self.state_topic = "shed/state/door"

    def set_mqtt_client(self, client, topic):
        self.mqtt_client = client
        self.state_topic = topic

    def control_door(self, action: str) -> None:
        """Control garage door relay: OPEN, CLOSE, STOP"""
        logger.info("Controlling door: %s (Type: %s)", action, self.RELAY_TYPE)

        if self.RELAY_TYPE == "gpio":
            try:
                import OPi.GPIO as GPIO
                # Setup GPIO (BOARD or BCM - Orange Pi usually BOARD or SUNXI)
                GPIO.setmode(GPIO.BOARD) 
                GPIO.setup(self.RELAY_GPIO_PIN, GPIO.OUT)
                
                # Pulse logic for garage door (Toggle)
                GPIO.output(self.RELAY_GPIO_PIN, GPIO.HIGH)
                time.sleep(0.5)  # 0.5s pulse
                GPIO.output(self.RELAY_GPIO_PIN, GPIO.LOW)
                
                GPIO.cleanup()
            except ImportError:
                logger.error("OPi.GPIO not installed. Run: pip install OPi.GPIO")
            except Exception as exc:
                logger.error("GPIO control failed: %s", exc)

        elif self.RELAY_TYPE == "tasmota":
            if self.RELAY_HTTP_URL:
                try:
                    # Assumes simple toggle for garage door
                    requests.get(f"{self.RELAY_HTTP_URL}/cm?cmnd=Power%20TOGGLE", timeout=2)
                except Exception as exc:
                    logger.error("Tasmota control failed: %s", exc)

        elif self.RELAY_TYPE == "http":
            if self.RELAY_HTTP_URL:
                try:
                    # Generic HTTP GET request
                    # Example: http://192.168.1.50/control?cmd=open
                    # You can append action to URL if needed, e.g. f"{self.RELAY_HTTP_URL}/{action}"
                    # For now, assumes URL triggers the action directly (toggle)
                    requests.get(self.RELAY_HTTP_URL, timeout=2)
                    logger.info(f"Sent HTTP request to {self.RELAY_HTTP_URL}")
                except Exception as exc:
                    logger.error("HTTP control failed: %s", exc)

        # Simulating state change for UI feedback
        with self.door_state_lock:
            if action == "OPEN":
                self.door_state = "open"
            elif action == "CLOSE":
                self.door_state = "closed"
            
            if self.mqtt_client:
                try:
                    self.mqtt_client.publish(self.state_topic, self.door_state, retain=True)
                except:
                    pass
