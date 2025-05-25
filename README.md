# LocalWiZ-MQTT Bridge

A bridge between WiZ smart lights and MQTT, enabling local control and integration with home automation systems like Home Assistant.

## Features

- 🏠 Local control of WiZ smart lights without cloud dependency
- 🔄 Automatic discovery of WiZ lights on your network
- 🌐 MQTT integration for use with Home Assistant and other smart home systems
- 🐳 Docker and Docker Compose support for easy deployment
- 🔄 Periodic rediscovery of lights to handle network changes
- 🔒 Secure MQTT authentication support

## Prerequisites

- Python 3.9+
- Docker and Docker Compose (optional)
- MQTT broker (e.g., Mosquitto)
- WiZ smart lights connected to your local network

## Installation

### Using Docker (Recommended)


1. Clone this repository:
   ```bash
   git clone https://github.com/jussikin/localwiz-mqtt.git
   cd localwiz-mqtt
   ```

2. Copy the example environment file and edit it:
   ```bash
   cp .env.example .env
   nano .env
   ```

3. Update the `.env` file with your MQTT broker details and network configuration.

4. Start the service using Docker Compose:
   ```bash
   docker-compose up -d
   ```

### Manual Installation

1. Clone this repository:
   ```bash
   git clone https://github.com/yourusername/localwiz-mqtt.git
   cd localwiz-mqtt
   ```

2. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install dependencies:
   ```bash
   pip install -e .
   ```

4. Copy the example environment file and edit it:
   ```bash
   cp .env.example .env
   nano .env
   ```

5. Run the service:
   ```bash
   python main.py
   ```

## Configuration

Edit the `.env` file to configure the bridge:

```ini
# Broadcast addresses for WiZ light discovery (comma-separated)
BROADCAST_ADDRESSES=192.168.1.255,192.168.0.255

# MQTT broker connection details
MQTT_BROKER=your-mqtt-broker
MQTT_PORT=1883
MQTT_USERNAME=your-username
MQTT_PASSWORD=your-password

# Base MQTT topic (will be used as prefix for all topics)
MQTT_TOPIC=wiz/lights

# Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
LOG_LEVEL=INFO

# How often to rediscover lights (in seconds, default: 300 = 5 minutes)
DISCOVERY_INTERVAL=300
```

## MQTT Topics

The bridge uses the following MQTT topic structure:

- `{MQTT_TOPIC}/discovery` - Discovery status messages
- `{MQTT_TOPIC}/{MAC_ADDRESS}/state` - Current state of the light
- `{MQTT_TOPIC}/{MAC_ADDRESS}/set/command` - Send commands to the light

### Commands

Send one of the following commands to the `.../set/command` topic:

- `ON` - Turn the light on
- `OFF` - Turn the light off
- `0-255` - Set brightness (0-255)

## Home Assistant Integration

Home Assistant can automatically discover the WiZ lights if you have MQTT discovery enabled. The bridge will publish the necessary discovery messages to make the lights appear in Home Assistant.

## Logging

The bridge uses Python's built-in logging system with the following log levels:

- `DEBUG`: Detailed information, typically of interest only when diagnosing problems
- `INFO`: Confirmation that things are working as expected
- `WARNING`: An indication that something unexpected happened (but the software is still working as expected)
- `ERROR`: Due to a more serious problem, the software has not been able to perform some function
- `CRITICAL`: A serious error, indicating that the program itself may be unable to continue running

Set the `LOG_LEVEL` environment variable to control the verbosity of the logs. For example:

```bash
# For minimal logs (only warnings and errors)
LOG_LEVEL=WARNING

# For detailed debugging information
LOG_LEVEL=DEBUG
```

## Troubleshooting

1. **Lights not being discovered**
   - Ensure your lights are connected to the same network
   - Check that the broadcast address is correct for your network
   - Verify that UDP broadcast traffic is allowed on your network

2. **MQTT connection issues**
   - Verify your MQTT broker is running and accessible
   - Check the MQTT broker logs for connection attempts
   - Ensure the MQTT credentials are correct

3. **Check container logs**
   ```bash
   docker-compose logs -f
   ```

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
