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
    connected: bool = True  # Track if light is currently connected
    
    @classmethod
    def from_bulb_state(cls, bulb, state, previous_light=None):
        """Create a LightInfo instance from a bulb and its state."""
        mac = bulb.mac.lower()
        
        # Safely get model and firmware version with fallbacks
        try:
            model = state.get('module_name') or (previous_light.model if previous_light else None)
        except (AttributeError, KeyError):
            model = previous_light.model if previous_light else None
            
        try:
            fw_version = state.get('fw_version') or (previous_light.firmware_version if previous_light else None)
        except (AttributeError, KeyError):
            fw_version = previous_light.firmware_version if previous_light else None
        
        # Get the current state and brightness
        light_state = state.get('state', False)
        brightness = state.get('dimming', 100) if light_state else 0
        
        # Check for circadian mode or other special modes
        scene_id = state.get('sceneId')
        is_circadian = scene_id and 'circadian' in str(scene_id).lower()
        
        # If in circadian mode, ensure light is reported as on with the current brightness
        if is_circadian and brightness > 0:
            light_state = True
        # Only force state to off if not in a special mode and brightness is 0
        elif brightness == 0 and light_state and not is_circadian:
            light_state = False
            
        return cls(
            ip=bulb.ip,
            mac=mac,
            state=light_state,
            brightness=brightness if light_state else 0,
            color=state.get('rgb', (255, 255, 255)) if light_state else None,
            model=model,
            firmware_version=fw_version,
            connected=True
        )

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
            
            # Get connected and total light counts
            connected_lights = sum(1 for light in lights if light.connected)
            
            # Publish discovery message
            discovery_payload = {
                "total_lights": len(self.lights),
                "connected_lights": connected_lights,
                "timestamp": int(time.time())
            }
            
            self.client.publish(
                self.discovery_topic,
                json.dumps(discovery_payload),
                qos=1,
                retain=True
            )
            
            # Publish each light's state
            for light in lights:
                try:
                    light_mac = light.mac.replace(':', '').lower()
                    light_topic = f"{self.base_topic}/{light_mac}"
                    
                    # Create a dictionary with light info
                    light_dict = {
                        "ip": light.ip,
                        "mac": light.mac,
                        "state": "ON" if light.connected and light.state else "OFF",
                        "brightness": light.brightness if light.connected else 0,
                        "color": light.color if light.connected else None,
                        "model": light.model,
                        "firmware": light.firmware_version,
                        "connected": light.connected,
                        "last_seen": int(time.time()) if light.connected else None,
                        "status": "online" if light.connected else "offline"
                    }
                    
                    # Publish to the light's topic with retain=True to ensure status persists
                    try:
                        self.client.publish(
                            light_topic,
                            json.dumps({k: v for k, v in light_dict.items() if v is not None}, default=str),
                            qos=1,
                            retain=True
                        )
                        
                        # Explicitly publish the connection status with retain=True
                        self.client.publish(
                            f"{light_topic}/connected",
                            "online" if light.connected else "offline",
                            qos=1,
                            retain=True
                        )
                    except Exception as e:
                        print(f"❌ Error publishing to MQTT for light {light.mac}: {e}")
                    
                    # Publish individual states
                    self._publish_individual_states(light)
                    
                except Exception as e:
                    print(f"❌ Error publishing light {light.mac}: {e}")
                    
            print(f"\n✅ Finished publishing light data ({connected_lights}/{len(self.lights)} connected)")
            
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
                'state': ('ON' if light.connected and light.state else 'OFF', 'State'),
                'brightness': (light.brightness if light.connected and light.brightness is not None else 0, 'Brightness'),
                'color': (light.color if light.connected else None, 'Color'),
                'model': (light.model, 'Model'),
                'firmware': (light.firmware_version, 'Firmware'),
                'connected': ('online' if light.connected else 'offline', 'Connection Status')
            }
            
            for key, (value, name) in states.items():
                try:
                    topic = f"{base_topic}/{key}"
                    # Skip None values
                    if value is None:
                        continue
                        
                    # Convert value to JSON string if it's a complex type
                    if isinstance(value, (list, tuple, dict)):
                        payload = json.dumps(value, default=str)
                    else:
                        payload = value
                    
                    # Ensure payload is a string for MQTT
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

async def discover_lights(previous_lights: Dict[str, LightInfo] = None) -> List[LightInfo]:
    print("Scanning for WiZ lights...")
    # Get broadcast addresses from .env file or use defaults
    broadcast_addresses = get_broadcast_addresses()
    discovered_lights = []
    current_macs = set()
    
    bulbs = []
    for addr in broadcast_addresses:
        try:
            print(f"Trying broadcast address: {addr}")
            found = await discovery.discover_lights(broadcast_space=addr)
            if found:
                bulbs.extend(found)
                print(f"Found {len(found)} lights on {addr}")
        except Exception as e:
            print(f"  Error discovering on {addr}: {e}")
    
    if not bulbs:
        print("No WiZ lights found on the network.")
        # Mark all previously discovered lights as disconnected
        if previous_lights:
            for mac, light in previous_lights.items():
                if light.connected:  # Only update if was previously connected
                    light.connected = False
                    print(f"Light {mac} is now disconnected")
                    discovered_lights.append(light)
        return discovered_lights
    
    print(f"\nFound {len(bulbs)} WiZ lights. Getting details...")
    
    # Track which MACs we've found in this discovery
    found_macs = set()
    
    # Process each discovered bulb
    for bulb in bulbs:
        try:
            # Get the state of the bulb
            state = await bulb.updateState()
            mac = bulb.mac.lower()
            found_macs.add(mac)
            
            # Debug: Print complete state object info
            print("\n" + "="*80)
            print(f"BULB STATE INSPECTION - IP: {bulb.ip}, MAC: {mac}")
            print("-" * 80)
            
            # 1. Print state object type and attributes
            print(f"State object type: {type(state).__name__}")
            if hasattr(state, '__dict__'):
                print("State object attributes:")
                for attr, value in state.__dict__.items():
                    print(f"  {attr}: {value} ({type(value).__name__})")
            
            # 2. Try to get state dict in different ways
            state_dict = {}
            
            # First check for pilotResult which contains the actual state
            if hasattr(state, 'pilotResult'):
                pilot_result = getattr(state, 'pilotResult', {})
                if isinstance(pilot_result, dict):
                    print("\nState from pilotResult:")
                    for key, value in pilot_result.items():
                        print(f"  {key}: {value} ({type(value).__name__})")
                    state_dict.update(pilot_result)
            
            # Then check _state
            if hasattr(state, '_state'):
                _state = getattr(state, '_state', {})
                print("\nState from _state attribute:")
                for key, value in _state.items():
                    if key not in state_dict:  # Don't override pilotResult values
                        print(f"  {key}: {value} ({type(value).__name__})")
                        state_dict[key] = value
            
            # 3. Try to get state using get_state() if it exists
            if hasattr(state, 'get_state'):
                try:
                    state_method_dict = state.get_state()
                    print("\nState from get_state() method:")
                    if isinstance(state_method_dict, dict):
                        for key, value in state_method_dict.items():
                            print(f"  {key}: {value} ({type(value).__name__})")
                        state_dict.update(state_method_dict)
                except Exception as e:
                    print(f"Error calling get_state(): {e}")
            
            # 4. Try direct attribute access for known important fields
            print("\nDirect attribute access:")
            for attr in ['state', 'get_state', 'brightness', 'dimming', 'sceneId', 'scene', 'scene_name', 'get_scene']:
                if hasattr(state, attr):
                    try:
                        value = getattr(state, attr)
                        if callable(value):
                            try:
                                value = value()
                                print(f"  {attr} (method result): {value} ({type(value).__name__})")
                                if isinstance(value, dict):
                                    state_dict.update(value)
                            except Exception as e:
                                print(f"  {attr} (method call failed): {e}")
                        else:
                            print(f"  {attr}: {value} ({type(value).__name__})")
                            state_dict[attr] = value
                    except Exception as e:
                        print(f"  Error accessing {attr}: {e}")
            
            print("\nFinal state_dict:", state_dict)
            print("=" * 80 + "\n")
            
            # Check if we already know about this light
            previous_light = previous_lights.get(mac) if previous_lights else None
            
            # Create or update light info using the new factory method
            light_info = LightInfo.from_bulb_state(bulb, state_dict, previous_light)
            
            if previous_light:
                if not previous_light.connected:
                    print(f"Light {mac} has reconnected")
                # Preserve previous values if not available in current state
                light_info.model = light_info.model or previous_light.model
                light_info.firmware_version = light_info.firmware_version or previous_light.firmware_version
            else:
                print(f"Discovered new light: {mac}")
            
            discovered_lights.append(light_info)
            
            # Print light info
            status = "CONNECTED" if light_info.connected else "DISCONNECTED"
            print("\n" + "="*50)
            print(f"Light: {light_info.model or 'Unknown model'} ({status})")
            print(f"  IP: {light_info.ip}")
            print(f"  MAC: {light_info.mac}")
            if light_info.firmware_version:
                print(f"  Firmware: {light_info.firmware_version}")
            print(f"  State: {'On' if light_info.state else 'Off'}")
            print(f"  Brightness: {light_info.brightness}%" if light_info.brightness is not None else "  Brightness: N/A")
            print(f"  Color: {light_info.color}" if light_info.color else "  Color: White")
            print("-" * 50)
            
            # Close the connection to prevent resource warnings
            await bulb.async_close()
            
        except Exception as e:
            print(f"  Error getting state for bulb {bulb.ip}: {e}")
    
    # Check for lights that were previously connected but not found in this discovery
    if previous_lights:
        for mac, light in previous_lights.items():
            if mac not in found_macs and light.connected:
                # Only update if the light was previously connected to avoid duplicate updates
                light.connected = False
                print(f"Light {mac} is now disconnected")
                # Add to discovered_lights to ensure the status update is published
                if light not in discovered_lights:
                    discovered_lights.append(light)
    
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
        # Get discovery interval from environment variable, default to 300 seconds (5 minutes)
        discovery_interval = int(os.getenv('DISCOVERY_INTERVAL', '300'))
        print(f"🔧 Discovery interval set to {discovery_interval} seconds")
        previous_lights = {}
        
        while running:
            current_time = time.time()
            
            # Run discovery if it's time
            if current_time - last_discovery >= discovery_interval or not previous_lights:
                print(f"\n🔍 Running discovery...")
                try:
                    # Pass the previous lights to track connection status
                    lights = await discover_lights(previous_lights)
                    
                    # Update our previous_lights dictionary with the current state
                    previous_lights = {light.mac.lower(): light for light in lights}
                    
                    if mqtt_client and mqtt_client.connected:
                        mqtt_client.publish_lights(lights)
                    last_discovery = current_time
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
