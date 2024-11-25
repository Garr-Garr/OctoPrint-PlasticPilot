# OctoPrint Plastic Pilot
An OctoPrint plugin that lets you control your 3D printer using a USB game controller

> ⚠️ **Current Status**: The movement is still somewhat janky, but the core functionality works! This is an early release, so expect some rough edges.

## Features

### Movement Control
- Dual-stick control scheme:
  - Left stick: X-axis movement
  - Right stick: Y-axis movement
- Three-speed movement system:
  - Precision mode (low speed) for detailed work
  - Walking mode (medium speed) for regular movements
  - Running mode (high speed) for rapid positioning
- Configurable movement smoothing to reduce jerkiness
- Adjustable deadzone thresholds to prevent drift

### Extrusion Control
- Pressure-sensitive triggers for filament control:
  - Right trigger: Extrude filament
  - Left trigger: Retract filament
- Adjustable extrusion and retraction:
  - Speed control
  - Amount per trigger press
  - Configurable minimum/maximum feedrates

### Button Controls
- A Button: Toggle drawing mode (pen up/down)
- B Button: Return to home position
- Left/Right Buttons: Decrease/Increase feedrate
- Multiple Z-height positions for drawing and travel

### Advanced Configuration
- Extensive settings for fine-tuning:
  - Movement speeds and thresholds
  - Response timing
  - Movement smoothing
  - Deadzone settings
  - Extrusion parameters
  - Debug mode for testing

## Controller Support
- Currently tested with:
  - Xbox One controller via USB cable
- [Py Inputs claims it works:](https://inputs.readthedocs.io/en/latest/user/hardwaresupport.html#hardwaresupport)
  - Xbox 360 Controller via USB cable
  - PS4 Controller via USB cable
  - PS3 Controller via USB cable (press PS button if controller is not awake)
  - Pi-Hut SNES Style USB Gamepad
- Planned future support:
  - Custom profiles for popular controllers
  - Bluetooth controller support
  - Additional controller types (WIP):
    - Flight sticks
    - Wii remotes
    - Xbox Kinect (maybe?)

## Requirements
- OctoPrint 1.4.0 or newer
- Python 3.7 or newer
- `inputs` Python package (≥0.5)
- Compatible USB game controller

## Installation

Install via the bundled [Plugin Manager](https://docs.octoprint.org/en/master/bundledplugins/pluginmanager.html)
or manually using this URL:

    https://github.com/Garr-Garr/OctoPrint-PlasticPilot/archive/refs/heads/master.zip

## Configuration

1. Open OctoPrint's settings
2. Navigate to the "Plastic Pilot" plugin settings
3. Configure the following sections:
   - Controller Settings
   - Movement Settings
   - Responsiveness Settings
   - Extrusion Settings
   - Speed Threshold Settings
   - Speed Multiplier Settings

### Key Settings to Adjust
- Base Speed: Overall movement speed (100-6000 mm/min)
- Movement Check Interval: How often to check for controller input (10-100ms)
- Smoothing Factor: Movement smoothing amount (0-100%)
- Deadzone Threshold: Minimum stick movement required (5-20%)
- Walking/Running Thresholds: Speed transition points
- Speed Multipliers: Fine-tune different movement speeds

## Known Issues
- Movement can be jerky at certain speeds
  - Input commands and command buffer needs to be trunkated
- Limited controller support currently
- No automatic controller profile detection

## Future Plans
- Improved movement smoothing
- Better controller profiles
- Twitch integration (hahahaha)
- More controller type support
- Custom button mapping

## Contributing
Pull requests are welcome! For major changes, please open an issue first to discuss what you would like to change.

## License
[AGPLv3](https://www.gnu.org/licenses/agpl-3.0.en.html)
