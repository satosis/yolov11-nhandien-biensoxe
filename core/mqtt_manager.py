
import os
import json
import logging
import threading
import time
import paho.mqtt.client as mqtt
from datetime import datetime

logger = logging.getLogger("mqtt_manager")

class MQTTManager:
    def __init__(self, door_controller=None):
        self.host = os.getenv("MQTT_HOST", "localhost")
        self.port = int(os.getenv("MQTT_PORT", "1883"))
        self.username = os.getenv("MQTT_USERNAME")
        self.password = os.getenv("MQTT_PASSWORD")
        self.topic = os.getenv("MQTT_TOPIC", "frigate/events")
        
        self.door_controller = door_controller
        self.client = mqtt.Client()
        
        if self.username:
            self.client.username_pw_set(self.username, self.password)
            
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_disconnect = self.on_disconnect
        
        self.command_topics = {
            "shed/cmd/gate_open",
            "shed/cmd/gate_closed",
            "shed/cmd/door"
        }
        
        self.state_topics = {
            "door": "shed/state/door"
        }

    def start(self):
        threading.Thread(target=self._run_loop, daemon=True).start()

    def _run_loop(self):
        while True:
            try:
                logger.info(f"Connecting to MQTT broker at {self.host}:{self.port}...")
                self.client.connect(self.host, self.port, keepalive=60)
                if self.door_controller:
                    self.door_controller.set_mqtt_client(self.client, self.state_topics["door"])
                self.client.loop_forever()
            except Exception as e:
                logger.error(f"MQTT connection failed: {e}")
                time.sleep(5)

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("MQTT Connected!")
            client.subscribe(self.topic)
            for topic in self.command_topics:
                client.subscribe(topic)
            # Publish initial states
            if self.door_controller:
                 client.publish(self.state_topics["door"], self.door_controller.door_state, retain=True)
        else:
            logger.error(f"MQTT Connect failed with code {rc}")

    def publish_state(self, people_count, vehicle_count, door_open):
        try:
            self.client.publish("shed/state/people_count", str(people_count), retain=True)
            self.client.publish("shed/state/vehicle_count", str(vehicle_count), retain=True)
            
            # Publish visual door state to sync with HA
            state = "open" if door_open else "closed"
            self.client.publish(self.state_topics["door"], state, retain=True)
        except Exception as e:
            logger.error(f"Failed to publish state: {e}")

    def publish_trigger_open(self):
        try:
            self.client.publish("shed/trigger/open", "OPEN", qos=1)
            logger.info("Published shed/trigger/open")
        except Exception as e:
            logger.error(f"Failed to publish trigger: {e}")

    def on_disconnect(self, client, userdata, rc):
        logger.warning(f"MQTT Disconnected (rc={rc})")

    def on_message(self, client, userdata, msg):
        try:
            topic = msg.topic
            payload = msg.payload.decode("utf-8")
            
            if topic == "shed/cmd/door" and self.door_controller:
                self.door_controller.control_door(payload)
                
            elif topic == "shed/cmd/gate_open":
                # Logic for gate open (if moved from event_bridge)
                pass
            elif topic == "shed/cmd/gate_closed":
                pass
                
        except Exception as e:
            logger.error(f"Error handling message: {e}")
