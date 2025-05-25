import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional, Tuple, Callable, Awaitable

# Configure logging
def setup_logging(level=logging.INFO):
    """Set up basic logging configuration.
    
    Args:
        level: Logging level (e.g., logging.INFO, logging.DEBUG)
    """
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    logging.basicConfig(
        level=level,
        format=log_format,
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )
    # Set log level for external libraries
    logging.getLogger('asyncio').setLevel(logging.WARNING)
    logging.getLogger('paho').setLevel(logging.WARNING)
    return logging.getLogger(__name__)

# Get log level from environment variable
log_level = getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO)
logger = setup_logging(log_level)

# Global flag to control the main loop
running = True

import paho.mqtt.client as mqtt
from dotenv import load_dotenv
from pywizlight import discovery, wizlight, PilotBuilder

# Load environment variables from .env file
load_dotenv()

def get_broadcast_addresses():
    """Get broadcast addresses from environment variable or use defaults."""
    default_addresses = [
        "10.102.255.255",  # Your specific network
    ]
    
    env_addresses = os.getenv('BROADCAST_ADDRESSES')
    if env_addresses:
        return [addr.strip() for addr in env_addresses.split(',')]
    return default_addresses

@dataclass
class LightInfo:
    ip: str
    mac: str
    state: bool
    brightness: Optional[int]
    color: Optional[tuple]
    model: Optional[str] = None
    firmware_version: Optional[str] = None

class MQTTClient:
    def __init__(self):
        self.client = mqtt.Client()
        self.broker = os.getenv('MQTT_BROKER', 'localhost')
        self.port = int(os.getenv('MQTT_PORT', 1883))
        self.username = os.getenv('MQTT_USERNAME')
        self.password = os.getenv('MQTT_PASSWORD')
        self.base_topic = os.getenv('MQTT_TOPIC', 'wiz/lights').rstrip('/')
        self.discovery_topic = f"{self.base_topic}/discovery"
        self.connected = False
        self.lights = {}  # Store light information by MAC
        self.loop = asyncio.new_event_loop()
        self._task = None
        self._stop_event = asyncio.Event()
        self.logger = logging.getLogger(__name__)
        try:
            self._connect()
            self.connected = True
        except Exception as e:
            logger.warning(f"Could not connect to MQTT broker: {e}")
            logger.warning("Will continue without MQTT functionality")
    
    def _connect(self):
        # Set up callbacks
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.on_publish = self._on_publish
        
        if self.username and self.password:
            self.client.username_pw_set(self.username, self.password)
        logger.info(f"Connecting to MQTT broker at {self.broker}:{self.port}...")
        self.client.connect(self.broker, self.port, 60)
        self.client.loop_start()  # Start the network loop in a separate thread
        
        # Start the event loop in a separate thread if not already running
        if not hasattr(self, '_loop_thread') or not self._loop_thread.is_alive():
            self._stop_event.clear()
            self._loop_thread = threading.Thread(target=self._run_event_loop, daemon=True)
            self._loop_thread.start()
    
    def _run_event_loop(self):
        """Run the asyncio event loop in a separate thread."""
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
    
    def stop(self):
        """Stop the MQTT client and event loop."""
        if hasattr(self, 'client'):
            self.client.loop_stop()
            self.client.disconnect()
        if hasattr(self, 'loop') and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        if hasattr(self, '_loop_thread') and self._loop_thread.is_alive():
            self._loop_thread.join()
        
    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("Successfully connected to MQTT broker")
            # Subscribe to command topics
            command_topic = f"{self.base_topic}/+/set/#"  # Subscribe to all set commands
            logger.debug(f"Subscribing to command topic: {command_topic}")
            try:
                result = client.subscribe(command_topic, qos=1)
                logger.debug(f"Subscription result: {result}")
            except Exception as e:
                logger.error(f"Failed to subscribe to command topic: {e}")
        else:
            logger.error(f"Failed to connect to MQTT broker with code: {rc}")
            
    def _on_disconnect(self, client, userdata, rc):
        if rc != 0:
            logger.warning(f"Unexpected MQTT disconnection (code: {rc}). Will attempt to reconnect.")
            
    def _on_publish(self, client, userdata, mid):
        logger.debug(f"Message published (mid: {mid})")
        
    def _on_message(self, client, userdata, msg):
        """Handle incoming MQTT messages."""
        try:
            logger.debug(f"\nINCOMING MESSAGE:")
            logger.debug(f"  Topic: {msg.topic}")
            logger.debug(f"  QoS: {msg.qos}")
            logger.debug(f"  Retain: {msg.retain}")
            
            # Try to parse JSON payload
            try:
                payload = msg.payload.decode('utf-8')
                logger.debug(f"  Payload: {payload}")
                try:
                    json_payload = json.loads(payload)
                    logger.debug("  JSON Parsed:")
                    for k, v in json_payload.items():
                        logger.debug(f"    {k}: {v}")
                except json.JSONDecodeError:
                    logger.debug("  Payload is not valid JSON")
            except UnicodeDecodeError:
                logger.debug(f"  Payload (binary): {msg.payload}")
                
            # Process the message based on topic
            self._process_message(msg.topic, msg.payload)
            
        except Exception as e:
            logger.error(f"Error processing incoming message: {e}", exc_info=True)
    
    def _process_message(self, topic, payload):
        """Process incoming MQTT messages and react accordingly."""
        try:
            logger.debug(f"Processing message for topic: {topic}")
            
            # Extract MAC address and command from topic
            parts = topic.split('/')
            if len(parts) >= 4 and parts[-2] == 'set' and parts[-1] == 'command':
                mac = parts[-3]  # Extract MAC from topic
                logger.debug(f"Command for light {mac}")
                
                # Get payload as string
                try:
                    payload_str = payload.decode('utf-8').strip().upper()
                    logger.debug(f"Command: {payload_str}")
                    
                    # Check if we know about this light
                    if mac not in self.lights:
                        logger.error(f"Unknown light with MAC: {mac}")
                        return
                        
                    light_info = self.lights[mac]
                    
                    # Run the coroutine in the event loop
                    async def _run_control():
                        try:
                            if payload_str in ['ON', 'OFF']:
                                await self._control_light(light_info.ip, 'on' if payload_str == 'ON' else 'off')
                                logger.info(f"Turned light {mac} {payload_str}")
                            elif payload_str.isdigit():
                                brightness = int(payload_str)
                                if 0 <= brightness <= 255:
                                    await self._control_light(light_info.ip, 'brightness', brightness=brightness)
                                    logger.info(f"Set brightness for light {mac} to {brightness}")
                                else:
                                    logger.error(f"Brightness {brightness} out of range (0-255)")
                            else:
                                logger.error(f"Unsupported command: {payload_str}")
                        except Exception as e:
                            logger.error(f"Error in control task: {e}", exc_info=True)
                    
                    # Schedule the coroutine to run in the event loop
                    asyncio.run_coroutine_threadsafe(_run_control(), self.loop)
                        
                except UnicodeDecodeError:
                    logger.debug(f"Binary payload: {payload}")
                    return
                except Exception as e:
                    logger.error(f"Error processing payload: {e}", exc_info=True)
                    return
            else:
                logger.debug(f"Unsupported topic format: {topic}")
            
            logger.debug("Message processing complete")
            
        except Exception as e:
            print(f"❌ Error in message processing: {e}")
            import traceback
            traceback.print_exc()
    
    async def _control_light(self, ip, action, **kwargs):
        """Control a light with error handling."""
        light = wizlight(ip)
        try:
            if action == 'on':
                await light.turn_on()
                print(f"✅ Turned ON light at {ip}")
            elif action == 'off':
                await light.turn_off()
                print(f"✅ Turned OFF light at {ip}")
            elif action == 'brightness' and 'brightness' in kwargs:
                brightness = kwargs['brightness']
                await light.turn_on(PilotBuilder(brightness=brightness))
                print(f"✅ Set brightness to {brightness} for light at {ip}")
            elif action == 'color' and 'rgb' in kwargs:
                rgb = kwargs['rgb']
                await light.turn_on(PilotBuilder(rgb=rgb))
                print(f"✅ Set color to {rgb} for light at {ip}")
        except Exception as e:
            print(f"❌ Error controlling light at {ip}: {e}")
        finally:
            await light.async_close()
    
    def publish_lights(self, lights: List[LightInfo]) -> None:
        """Publish discovered lights to MQTT topics."""
        if not self.connected or not hasattr(self, 'client') or not self.client.is_connected():
            print("⚠️ Not connected to MQTT broker. Cannot publish lights.")
            return
            
        # Update our internal registry of lights
        for light in lights:
            self.lights[light.mac.replace(':', '').lower()] = light
            
        try:
            print("\n" + "="*50)
            print(f"PUBLISHING {len(lights)} LIGHTS TO MQTT")
            print("="*50)
            
            # Publish to the main discovery topic with all lights
            payload = {
                'lights': [asdict(light) for light in lights],
                'timestamp': int(time.time() * 1000)  # Current time in milliseconds
            }
            
            print(f"\n📤 PUBLISHING TO MAIN TOPIC: {self.discovery_topic}")
            
            self.client.publish(
                self.discovery_topic,
                json.dumps(payload, default=str),
                qos=1,
                retain=True
            )
            
            if not lights:
                print("⚠️ No lights to publish.")
                return
                
            # Publish each light to its own topic
            for light in lights:
                try:
                    light_mac = light.mac.replace(':', '').lower()
                    light_topic = f"{self.base_topic}/{light_mac}"
                    light_dict = asdict(light)
                    
                    # Publish full light info
                    self.client.publish(
                        light_topic,
                        json.dumps(light_dict, default=str),
                        qos=1,
                        retain=True
                    )
                    
                    # Publish individual states
                    self._publish_individual_states(light)
                    
                except Exception as e:
                    print(f"❌ Error publishing light {light.mac}: {e}")
                    
            print("\n✅ Finished publishing light data")
            
        except Exception as e:
            print(f"❌ Error in publish_lights: {e}")
            
    def _publish_individual_states(self, light: LightInfo) -> None:
        """Publish individual state topics for each light attribute."""
        if not self.client.is_connected():
            return
            
        try:
            light_mac = light.mac.replace(':', '').lower()
            base_topic = f"{self.base_topic}/{light_mac}"
            
            # Publish individual state updates
            states = {
                'state': ('ON' if light.state else 'OFF', 'State'),
                'brightness': (light.brightness if light.brightness is not None else 0, 'Brightness'),
                'color': (light.color if light.color else [255, 255, 255], 'Color'),
                'model': (light.model or 'unknown', 'Model'),
                'firmware': (light.firmware_version or 'unknown', 'Firmware'),
                'online': ('true' if light.ip else 'false', 'Online')
            }
            
            for attr, (value, name) in states.items():
                topic = f"{base_topic}/{attr}"
                try:
                    # For Home Assistant compatibility
                    if attr in ['state', 'brightness', 'color']:
                        payload = str(value).lower() if isinstance(value, str) else value
                    else:
                        payload = value
                        
                    if not isinstance(payload, (str, int, float, bool)):
                        payload = json.dumps(payload, default=str)
                        
                    self.client.publish(
                        topic,
                        payload=payload,
                        qos=1,
                        retain=True
                    )
                    
                except Exception as e:
                    print(f"❌ Error publishing {name} to {topic}: {e}")
                    
        except Exception as e:
            print(f"❌ Error in _publish_individual_states: {e}")

async def discover_lights() -> List[LightInfo]:
    print("Scanning for WiZ lights...")
    # Get broadcast addresses from .env file or use defaults
    broadcast_addresses = get_broadcast_addresses()
    discovered_lights = []
    
    bulbs = []
    for addr in broadcast_addresses:
        try:
            print(f"Trying broadcast address: {addr}")
            found = await discovery.discover_lights(broadcast_space=addr)
            if found:
                bulbs.extend(found)
                print(f"Found {len(found)} lights on {addr}")
        except Exception as e:
            print(f"Error scanning {addr}: {e}")
    
    if not bulbs:
        print("No WiZ lights found on the network.")
        return []
    
    print(f"\nFound {len(bulbs)} WiZ light(s):")
    print("-" * 50)
    
    discovered_lights = []
    for i, bulb in enumerate(bulbs, 1):
        try:
            # Get the bulb's state and config
            state = await bulb.updateState()
            config = await bulb.getBulbConfig()
            
            light_info = LightInfo(
                ip=bulb.ip,
                mac=bulb.mac,
                state=state.get_state(),
                brightness=state.get_brightness(),
                color=state.get_rgb(),
                model=config.get('module_name') if config else None,
                firmware_version=config.get('fwVersion') if config else None
            )
            
            discovered_lights.append(light_info)
            
            # Print light info
            print(f"Light {i}:")
            print(f"  IP: {light_info.ip}")
            print(f"  MAC: {light_info.mac}")
            print(f"  Model: {light_info.model or 'N/A'}")
            print(f"  Firmware: {light_info.firmware_version or 'N/A'}")
            print(f"  State: {'On' if light_info.state else 'Off'}")
            print(f"  Brightness: {light_info.brightness}%" if light_info.brightness is not None else "  Brightness: N/A")
            print(f"  Color: {light_info.color}" if light_info.color else "  Color: White")
            print("-" * 50)
            
            # Close the connection to prevent resource warnings
            await bulb.async_close()
            
        except Exception as e:
            print(f"  Error getting state for bulb {bulb.ip}: {e}")
    
    return discovered_lights

async def control_light(ip, action, **kwargs):
    """Control a specific light by IP address."""
    light = wizlight(ip)
    try:
        if action == 'on':
            await light.turn_on()
            print(f"Turned on light at {ip}")
        elif action == 'off':
            await light.turn_off()
            print(f"Turned off light at {ip}")
        elif action == 'brightness' and 'brightness' in kwargs:
            await light.turn_on(PilotBuilder(brightness=kwargs['brightness']))
            print(f"Set brightness to {kwargs['brightness']}% for light at {ip}")
        elif action == 'color' and 'rgb' in kwargs:
            await light.turn_on(PilotBuilder(rgb=kwargs['rgb']))
            print(f"Set color to {kwargs['rgb']} for light at {ip}")
    except Exception as e:
        print(f"Error controlling light at {ip}: {e}")
    finally:
        await light.async_close()

def main():
    # Initialize MQTT client
    mqtt_client = None
    try:
        mqtt_client = MQTTClient()
        # Give it a moment to connect
        time.sleep(1)
    except Exception as e:
        print(f"Warning: Could not initialize MQTT client: {e}")
        print("Continuing without MQTT...")
    
    try:
        if len(sys.argv) > 1:
            # If command line arguments are provided, try to control a light or discover
            import argparse
            parser = argparse.ArgumentParser(description='Control WiZ lights')
            parser.add_argument('action', choices=['on', 'off', 'brightness', 'color', 'discover'], 
                            help='Action to perform')
            parser.add_argument('--ip', help='IP address of the light (not required for discover)')
            parser.add_argument('--brightness', type=int, help='Brightness level (0-255)')
            parser.add_argument('--rgb', nargs=3, type=int, help='RGB values as R G B (0-255 each)')
            
            args = parser.parse_args()
            
            if args.action == 'discover':
                lights = asyncio.run(discover_lights())
                if mqtt_client:
                    mqtt_client.publish_lights(lights)
            else:
                if not args.ip:
                    parser.error(f"IP address is required for action: {args.action}")
                if args.action == 'brightness' and not args.brightness:
                    parser.error("Brightness action requires --brightness argument")
                if args.action == 'color' and not args.rgb:
                    parser.error("Color action requires --rgb argument")
                    
                asyncio.run(control_light(args.ip, args.action, 
                                        brightness=args.brightness, 
                                        rgb=tuple(args.rgb) if args.rgb else None))
        else:
            # If no arguments, just discover and list lights and publish to MQTT
            lights = asyncio.run(discover_lights())
            if mqtt_client:
                mqtt_client.publish_lights(lights)
                
        # Give MQTT time to send all messages
        if mqtt_client:
            print("\nWaiting for all MQTT messages to be delivered...")
            time.sleep(2)  # Give it time to send all messages
            
    except KeyboardInterrupt:
        print("\nScript interrupted by user")
    except Exception as e:
        print(f"\n❌ An error occurred: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if mqtt_client:
            print("\nDisconnecting from MQTT...")
            mqtt_client.client.disconnect()
            mqtt_client.client.loop_stop()
        print("Script finished.")

async def main_loop():
    """Main service loop."""
    global running
    
    # Reset running flag
    running = True
    
    # Initialize MQTT client
    mqtt_client = None
    try:
        mqtt_client = MQTTClient()
        # Give it a moment to connect
        await asyncio.sleep(1)
    except Exception as e:
        print(f"⚠️ Could not initialize MQTT client: {e}")
        print("⚠️ Running in local mode only (no MQTT)")
    
    print("🚀 WiZ Light MQTT Service started. Press Ctrl+C to stop.")
    
    try:
        last_discovery = 0
        discovery_interval = 300  # 5 minutes between discoveries
        
        while running:
            current_time = time.time()
            # Check if it's time for periodic discovery
            if current_time - last_discovery > discovery_interval:
                try:
                    lights = await discover_lights()
                    if mqtt_client:
                        mqtt_client.publish_lights(lights)
                    last_discovery = current_time
                    print(f"✅ Discovered {len(lights)} lights at {time.strftime('%Y-%m-%d %H:%M:%S')}")
                except Exception as e:
                    print(f"❌ Error during discovery: {e}")
            
            # Sleep for a short time to prevent high CPU usage
            await asyncio.sleep(1)
            
    except asyncio.CancelledError:
        print("\nReceived shutdown signal...")
    except Exception as e:
        print(f"\n❌ Fatal error in main loop: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Clean up
        if mqtt_client:
            print("\nDisconnecting from MQTT...")
            if hasattr(mqtt_client, 'status_topic'):
                mqtt_client.client.publish(mqtt_client.status_topic, "offline", retain=True, qos=1)
            mqtt_client.client.disconnect()
            mqtt_client.client.loop_stop()
        print("Service stopped.")

async def shutdown(service, loop):
    """Handle shutdown gracefully."""
    print("\nShutting down...")
    
    # Cancel all running tasks
    tasks = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task(loop)]
    for task in tasks:
        task.cancel()
    
    # Wait for tasks to cancel
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    
    # Stop the loop
    loop.stop()

def run_service():
    """Run the service."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    running = True
    
    async def shutdown_internal():
        nonlocal running
        running = False
        await asyncio.sleep(0.1)  # Allow any pending callbacks to run
        
        # If we get here and the loop is still running, force stop it
        if loop.is_running():
            loop.stop()
    
    def handle_signal(signum, frame):
        print(f"\nReceived signal {signum}, shutting down...")
        asyncio.run_coroutine_threadsafe(shutdown_internal(), loop)
    
    # Set up signal handlers
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle_signal)
    
    try:
        loop.run_until_complete(main_loop())
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nService stopped by user")
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Ensure we clean up any remaining tasks
        pending = asyncio.all_tasks(loop)
        if pending:
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        
        loop.close()
        print("Exiting...")

if __name__ == "__main__":
    run_service()
