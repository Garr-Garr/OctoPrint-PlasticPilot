# coding=utf-8
from __future__ import absolute_import
import octoprint.plugin
import flask
from flask import jsonify, request
from octoprint.server import app
import subprocess
from time import sleep
from threading import Thread, Lock, Event
import threading
from inputs import get_gamepad
from collections import deque
from dataclasses import dataclass
from typing import List, Deque
from queue import Queue
import math
import time
import logging
import json

@dataclass
class ControllerState:
	"""Represents a single controller state snapshot"""
	x_axis: float = 0.0  # Left stick X
	y_axis: float = 0.0  # Right stick Y
	extrusion: float = 0.0  # Right trigger
	retraction: float = 0.0  # Left trigger
	timestamp: float = 0.0

@dataclass
class MovementCommand:
	"""Represents a consolidated movement command"""
	x: float = 0.0
	y: float = 0.0
	e: float = 0.0
	f: float = 0.0  # Feedrate

class BufferedController:
	"""Handles buffered input processing and movement coordination"""
	def __init__(self, plugin, max_buffer_size=100):
		self.plugin = plugin
		self.logger = plugin._logger
		self._input_buffer: Deque[ControllerState] = deque(maxlen=max_buffer_size)
		self._command_queue: Queue[MovementCommand] = Queue()
		self._movement_thread = None
		self._input_thread = None
		self._stop_event = threading.Event()

		# Movement state
		self.current_x = 0.0
		self.current_y = 0.0
		self.current_e = 0.0
		self.last_extrude_position = None

		# Threading control
		self._buffer_lock = threading.Lock()
		self._state_lock = threading.Lock()

		# Movement parameters
		self.min_movement = 0.01  # mm
		self.max_extrusion_per_mm = 0.1  # mm of filament per mm of travel
		self.input_poll_rate = 0.001  # 1ms
		self.movement_update_rate = 0.1  # 100ms

		# Initialize settings from plugin
		self.update_settings(plugin._settings)

	def update_settings(self, settings):
		"""Update controller settings from plugin settings"""
		with self._state_lock:
			self.base_speed = float(settings.get(["base_speed"]))
			self.min_movement = float(settings.get(["min_movement"]))
			self.extrusion_speed = float(settings.get(["extrusion_speed"]))
			self.retraction_speed = float(settings.get(["retraction_speed"]))
			self.max_extrusion_per_mm = float(settings.get(["extrusion_amount"]))

			# Update timing parameters
			self.movement_update_rate = float(settings.get(["movement_check_interval"])) / 1000.0  # Convert to seconds

			# Update debugging
			if hasattr(self.plugin, 'joy') and self.plugin.joy:
				self.plugin.joy.debug_mode = settings.get_boolean(["debug_mode"])

	def start(self):
		"""Start the input and movement processing threads"""
		self._stop_event.clear()

		# Start input polling thread
		self._input_thread = threading.Thread(target=self._input_polling_loop)
		self._input_thread.daemon = True
		self._input_thread.start()

		# Start movement processing thread
		self._movement_thread = threading.Thread(target=self._movement_processing_loop)
		self._movement_thread.daemon = True
		self._movement_thread.start()

		self.logger.info("Buffered controller system started")

	def stop(self):
		"""Stop all processing threads"""
		self._stop_event.set()

		if self._input_thread:
			self._input_thread.join(timeout=1.0)
		if self._movement_thread:
			self._movement_thread.join(timeout=1.0)

		self._input_buffer.clear()
		while not self._command_queue.empty():
			self._command_queue.get()

		self.logger.info("Buffered controller system stopped")

	def _input_polling_loop(self):
		"""Rapidly poll controller input and add to buffer"""
		last_poll_time = time.time()

		while not self._stop_event.is_set():
			current_time = time.time()
			if current_time - last_poll_time >= self.input_poll_rate:
				try:
					if self.plugin.joy and self.plugin.joy.read():
						# Handle button presses
						self._handle_buttons()

						with self._buffer_lock:
							state = ControllerState(
								x_axis=self.plugin.joy.left_x / self.plugin.joy.max_analog_val,
								y_axis=-self.plugin.joy.right_y / self.plugin.joy.max_analog_val,  # Invert Y
								extrusion=self.plugin.joy.right_trigger,
								retraction=self.plugin.joy.left_trigger,
								timestamp=current_time
							)
							self._input_buffer.append(state)
					last_poll_time = current_time
				except Exception as e:
					self.logger.error(f"Error polling input: {str(e)}")

			# Small sleep to prevent CPU thrashing
			time.sleep(0.0005)  # 0.5ms sleep

	def _movement_processing_loop(self):
		"""Process buffered input and generate movement commands"""
		last_process_time = time.time()

		while not self._stop_event.is_set():
			current_time = time.time()
			if current_time - last_process_time >= self.movement_update_rate:
				try:
					self._process_buffered_input()
					last_process_time = current_time
				except Exception as e:
					self.logger.error(f"Error processing movement: {str(e)}")

			# Small sleep to prevent CPU thrashing
			time.sleep(0.001)  # 1ms sleep

	def _process_buffered_input(self):
		"""Process accumulated input buffer into movement commands"""
		if not self._input_buffer:
			return

		with self._buffer_lock:
			buffer_snapshot = list(self._input_buffer)
			self._input_buffer.clear()

		if not buffer_snapshot:
			return

		# Average the movements over the buffer period
		avg_x = sum(state.x_axis for state in buffer_snapshot) / len(buffer_snapshot)
		avg_y = sum(state.y_axis for state in buffer_snapshot) / len(buffer_snapshot)
		avg_extrusion = sum(state.extrusion for state in buffer_snapshot) / len(buffer_snapshot)
		avg_retraction = sum(state.retraction for state in buffer_snapshot) / len(buffer_snapshot)

		# Calculate movement distances
		movement_time = self.movement_update_rate  # seconds
		x_distance = avg_x * (self.base_speed / 60) * movement_time
		y_distance = avg_y * (self.base_speed / 60) * movement_time

		# Check if movement is significant
		total_distance = math.sqrt(x_distance**2 + y_distance**2)
		if total_distance < self.min_movement:
			return

		# Calculate new position
		new_x = max(0, min(self.plugin.maxX, self.current_x + x_distance))
		new_y = max(0, min(self.plugin.maxY, self.current_y + y_distance))

		# Calculate extrusion based on movement distance
		e_distance = 0.0
		if avg_extrusion > 0.1:  # Extrusion threshold
			e_distance = total_distance * self.max_extrusion_per_mm * avg_extrusion
		elif avg_retraction > 0.1:  # Retraction threshold
			e_distance = -total_distance * self.max_extrusion_per_mm * avg_retraction

		# Create movement command
		command = MovementCommand(
			x=new_x,
			y=new_y,
			e=self.current_e + e_distance,
			f=self.base_speed
		)

		# Update current positions
		self.current_x = new_x
		self.current_y = new_y
		self.current_e += e_distance

		# Send command to printer
		self._send_movement_command(command)

	def _send_movement_command(self, command: MovementCommand):
		"""Send consolidated movement command to printer"""
		try:
			# Generate coordinated movement GCode
			gcode = f"G1 X{command.x:.3f} Y{command.y:.3f}"
			if abs(command.e) > 0:
				gcode += f" E{command.e:.4f}"
			gcode += f" F{command.f}"

			self.plugin.send(gcode)

		except Exception as e:
			self.logger.error(f"Error sending movement command: {str(e)}")

	def get_current_position(self):
		"""Get current position information"""
		return {
			'x': self.current_x,
			'y': self.current_y,
			'e': self.current_e
		}
	def _handle_buttons(self):
		"""Handle button press actions"""
		try:
			# Check if buttons were just pressed (edge detection)
			if self.plugin.joy.a_pressed:
				self.plugin.drawing = not self.plugin.drawing
				z_height = self.plugin.z_drawing if self.plugin.drawing else self.plugin.z_travel
				gcode = f'G1 Z{z_height} F1000'
				self.logger.info(f"Toggling drawing mode: {'Drawing' if self.plugin.drawing else 'Travel'}")
				self.plugin.send(gcode)
				time.sleep(0.1)  # Debounce

			if self.plugin.joy.b_pressed:
				self.logger.info("Homing XY axes")
				self.plugin.send("G28 X Y")
				self.current_x = 0.0
				self.current_y = 0.0
				self.plugin.current_x = 0.0
				self.plugin.current_y = 0.0
				time.sleep(0.1)  # Debounce

			# Handle feedrate adjustments
			self._handle_feedrate()

		except Exception as e:
			self.logger.error(f"Error handling buttons: {str(e)}")

	def _handle_feedrate(self):
		"""Handle feedrate adjustments"""
		try:
			if self.plugin.joy.right_button:
				self.plugin.current_e_feedrate = min(
					self.plugin.current_e_feedrate + self.plugin._settings.get_float(["feedrate_increment"]),
					self.plugin._settings.get_float(["max_feedrate"])
				)
				if self.plugin.joy.debug_mode:
					self.logger.info(f"Increased feedrate to: {self.plugin.current_e_feedrate:.1f} mm/s")
				time.sleep(0.1)

			elif self.plugin.joy.left_button:
				self.plugin.current_e_feedrate = max(
					self.plugin.current_e_feedrate - self.plugin._settings.get_float(["feedrate_increment"]),
					self.plugin._settings.get_float(["min_feedrate"])
				)
				if self.plugin.joy.debug_mode:
					self.logger.info(f"Decreased feedrate to: {self.plugin.current_e_feedrate:.1f} mm/s")
				time.sleep(0.1)

		except Exception as e:
			self.logger.error(f"Error handling feedrate: {str(e)}")


class UserController:
	def __init__(self):
		self.reset_state()
		self.max_analog_val = math.pow(2, 15)
		self.debug_mode = False
		self._logger = logging.getLogger("octoprint.plugins.plasticpilot")

	def reset_state(self):
		# Analog inputs with explicit zero state
		self.left_x = 0.0
		self.left_y = 0.0
		self.right_x = 0.0
		self.right_y = 0.0
		self.left_trigger = 0.0
		self.right_trigger = 0.0

		# Button states
		self.a_pressed = False
		self.b_pressed = False
		self.x_pressed = False
		self.y_pressed = False
		self.right_button = False
		self.left_button = False

	def read(self):
		"""
		Read and process all pending controller events
		Returns True if successful, False if there was an error
		"""
		try:
			events = get_gamepad()
			if not events:  # No events to process
				return True

			for event in events:
				if not self.process_event(event):
					self._logger.error("Failed to process controller event")
					return False

			return True

		except Exception as e:
			self._logger.error(f"Error reading gamepad: {str(e)}")
			return False

	def process_event(self, event):
		"""Process raw controller events into state updates"""
		try:
			if self.debug_mode:
				self._logger.info(f"Raw event: {event.ev_type} - {event.code} - {event.state}")

			if event.ev_type == "Absolute":
				if event.code == "ABS_X":     # Left stick X axis
					self.left_x = event.state
				elif event.code == "ABS_Y":   # Left stick Y axis
					self.left_y = event.state
				elif event.code == "ABS_RX":  # Right stick X axis
					self.right_x = event.state
				elif event.code == "ABS_RY":  # Right stick Y axis
					self.right_y = event.state
				elif event.code == "ABS_Z":   # Left trigger
					self.left_trigger = event.state / 255.0  # Normalize to 0-1
				elif event.code == "ABS_RZ":  # Right trigger
					self.right_trigger = event.state / 255.0  # Normalize to 0-1

			elif event.ev_type == "Key":
				if event.code == "BTN_SOUTH":    # A button
					self.a_pressed = event.state == 1
				elif event.code == "BTN_EAST":   # B button
					self.b_pressed = event.state == 1
				elif event.code == "BTN_WEST":   # X button
					self.x_pressed = event.state == 1
				elif event.code == "BTN_NORTH":  # Y button
					self.y_pressed = event.state == 1
				elif event.code == "BTN_TL":     # Left button
					self.left_button = event.state == 1
				elif event.code == "BTN_TR":     # Right button
					self.right_button = event.state == 1

			return True

		except Exception as e:
			self._logger.error(f"Error processing event: {str(e)}")
			return False

class PlasticPilot(octoprint.plugin.SettingsPlugin,
				octoprint.plugin.AssetPlugin,
				octoprint.plugin.ShutdownPlugin,
				octoprint.plugin.StartupPlugin,
				octoprint.plugin.EventHandlerPlugin,
				octoprint.plugin.SimpleApiPlugin,
				octoprint.plugin.TemplatePlugin,
				octoprint.plugin.BlueprintPlugin):

	def __init__(self):
		super().__init__()
		self.bStop = False
		self.bConnected = False
		self.bStarted = False
		self.joy = None
		self.maxX = 0.0  # Will be set from printer profile
		self.maxY = 0.0  # Will be set from printer profile
		self.current_x = 0.0
		self.current_y = 0.0
		self.movement_speed = 3000  # Base movement speed (mm/min)
		self.drawing = False  # Track if we're currently drawing
		self.z_drawing = 0.2  # Z height when drawing
		self.z_travel = 1.0   # Z height when not drawing
		self.active_controller = None  # Initialize the active controller

		self._position_lock = Lock()  # For protecting position updates
		self._state_lock = Lock()     # For protecting state variables
		self._stop_event = Event()    # For clean thread shutdown

		self._extrusion_lock = Lock()  # For protecting extrusion operations
		self.current_e_feedrate = 2.0  # Default extruder feedrate in mm/s

		self.buffered_controller = None

		# Add logger
		self._logger = logging.getLogger("octoprint.plugins.plasticpilot")

	@octoprint.plugin.BlueprintPlugin.route("/defaults", methods=["GET"])
	def get_defaults(self):
		"""Return the default settings values"""
		return flask.jsonify(self.get_settings_defaults())


	@octoprint.plugin.BlueprintPlugin.route("/controllers", methods=["GET"])
	def get_controllers(self):
		"""Enhanced controller detection endpoint"""
		controllers = self.list_available_controllers()
		return flask.jsonify({"controllers": controllers})

	@octoprint.plugin.BlueprintPlugin.route("/activate", methods=["POST"])
	def activate_controller(self):
		if not self._printer.is_operational():
			return flask.jsonify({
				"success": False,
				"error": "Printer is not operational"
			})

		data = flask.request.json
		controller_id = data.get("controller_id")
		if not controller_id:
			return flask.jsonify({
				"success": False,
				"error": "No controller ID provided"
			})

		try:
			self.active_controller = controller_id
			self.start_controller_thread()
			return flask.jsonify({"success": True})
		except Exception as e:
			self._logger.error(f"Failed to activate controller: {str(e)}")
			return flask.jsonify({
				"success": False,
				"error": str(e)
			})

	@octoprint.plugin.BlueprintPlugin.route("/deactivate", methods=["POST"])
	def deactivate_controller(self):
		try:
			self.stop_controller_thread()
			return flask.jsonify({"success": True})
		except Exception as e:
			self._logger.error(f"Failed to deactivate controller: {str(e)}")
			return flask.jsonify({
				"success": False,
				"error": str(e)
			})

	def is_blueprint_csrf_protected(self):
		return True

	def update_printer_dimensions(self):
		"""Update max X/Y dimensions from the active printer profile"""
		try:
			profile = self._printer_profile_manager.get_current_or_default()
			volume = profile.get("volume", {})

			# Get dimensions, defaulting to 200mm if not found
			self.maxX = float(volume.get("width", 200))
			self.maxY = float(volume.get("depth", 200))

			# Get origin to adjust coordinates if needed
			origin = volume.get("origin", "lowerleft")
			if origin == "center":
				# Adjust for center origin
				self.maxX = self.maxX / 2
				self.maxY = self.maxY / 2

			self._logger.info(f"Printer dimensions updated: X={self.maxX}mm, Y={self.maxY}mm, Origin={origin}")

			# Update current position if it's outside new bounds
			self.current_x = min(self.current_x, self.maxX)
			self.current_y = min(self.current_y, self.maxY)

		except Exception as e:
			self._logger.error(f"Error updating printer dimensions: {str(e)}")
			# Fall back to default values
			self.maxX = 200.0
			self.maxY = 200.0

	def start_controller_thread(self):
		"""Start the controller system"""
		try:
			if self.buffered_controller is not None:
				self._logger.info("Controller system already running")
				# Still send status update even if already running
				self._plugin_manager.send_plugin_message(self._identifier, {
					"type": "controller_status",
					"active": True,
					"controller_id": self.active_controller
				})
				return

			self.joy = UserController()
			debug_mode = self._settings.get_boolean(["debug_mode"])
			self._logger.info(f"Starting controller with debug_mode: {debug_mode}")
			self.joy.debug_mode = debug_mode

			# Home all axes before starting
			self._logger.info("Homing all axes...")
			self.send("G28 XY")
			self.send("G28 Z")

			# Reset current position after homing
			self.current_x = 0.0
			self.current_y = 0.0

			# Initialize and start buffered controller
			self.buffered_controller = BufferedController(self)
			self.buffered_controller.update_settings(self._settings)
			self.buffered_controller.start()

			# Send status update AFTER successful startup
			self._plugin_manager.send_plugin_message(self._identifier, {
				"type": "controller_status",
				"active": True,
				"controller_id": self.active_controller
			})

			self._logger.info("Controller system started")

		except Exception as e:
			self._logger.error(f"Failed to start controller: {str(e)}")
			# Send failure status
			self._plugin_manager.send_plugin_message(self._identifier, {
				"type": "controller_status",
				"active": False,
				"controller_id": None,
				"error": str(e)
			})
			raise

	def stop_controller_thread(self):
		"""Stop the controller input thread with proper cleanup"""
		self._logger.info("Initiating controller shutdown...")

		try:
			# Stop the buffered controller first
			if self.buffered_controller is not None:
				self._logger.info("Stopping buffered controller...")
				try:
					self.buffered_controller.stop()
				except Exception as e:
					self._logger.error(f"Error stopping buffered controller: {str(e)}")
				self.buffered_controller = None

			# Signal the thread to stop
			self._stop_event.set()

			# Clean up controller resources
			if hasattr(self, 'joy') and self.joy is not None:
				self._logger.info("Cleaning up controller resources...")
				try:
					del self.joy
				except Exception as e:
					self._logger.error(f"Error cleaning up controller object: {str(e)}")
				self.joy = None

			# Reset positions
			self.current_x = 0.0
			self.current_y = 0.0

			# Send final status update
			self._plugin_manager.send_plugin_message(self._identifier, {
				"type": "controller_status",
				"active": False,
				"controller_id": None
			})

			self._logger.info("Controller shutdown completed successfully")

		except Exception as e:
			self._logger.error(f"Error during controller shutdown: {str(e)}")
		finally:
			# Ensure these are always reset even if there's an error
			self.joy = None
			self.buffered_controller = None
			self.active_controller = None

	def list_available_controllers(self):
		"""Actively scan and list all available controllers"""
		controllers = []
		try:
			# Force reload by reimporting
			import importlib
			import inputs
			importlib.reload(inputs)

			# Get fresh list of controllers
			available_gamepads = inputs.devices.gamepads

			for device in available_gamepads:
				controller_info = {
					"id": device.name,
					"name": device.name
				}
				controllers.append(controller_info)
				self._logger.info(f"Found controller: {device.name}")

			if not controllers:
				self._logger.info("No controllers found during refresh")
			else:
				self._logger.info(f"Found {len(controllers)} controller(s):")
				for ctrl in controllers:
					self._logger.info(f"  - {ctrl['name']}")

			return controllers

		except Exception as e:
			self._logger.error(f"Error scanning for controllers: {str(e)}")
			self._logger.exception("Detailed error information:")
			return []

	def on_after_startup(self):
		self._logger.info("Controller starting up")
		self._logger.info(f"Available routes: {app.url_map}")
		self.update_printer_dimensions()

	def get_settings_defaults(self):
		return dict(
			max_x=200.0,
			max_y=200.0,
			z_drawing=0.1,
			z_travel=1.0,
			base_speed=3000,
			debug_mode=False,
			# Responsiveness settings
			movement_check_interval=25,	# 25ms
			command_delay=20,			# 20ms
			smoothing_factor=20,		# percentage
			min_movement=0.025,			# 0.025mm
			# Speed threshold settings
			deadzone_threshold=10,		# percentage
			walk_threshold=40,			# percentage
			run_threshold=75,			# percentage
			walk_speed_multiplier=40,	# percentage
			run_speed_multiplier=80,	# percentage
			max_speed_multiplier=100,	# percentage
			# Extrusion settings
			extrusion_speed=5.0,        # mm/s for extrusion
			retraction_speed=25.0,      # mm/s for retraction
			extrusion_amount=0.2,       # mm per trigger press
			retraction_amount=1.0,      # mm per trigger press
			# Feedrate settings
			feedrate_increment=100,     # mm/min per button press
			min_feedrate=0.5,           # mm/s minimum feedrate (30 mm/min)
			max_feedrate=15.0,          # mm/s maximum feedrate (500 mm/min)
		)

	def on_settings_save(self, data):
		"""Handle saving and updating all settings"""
		# Call parent implementation first to save all settings
		octoprint.plugin.SettingsPlugin.on_settings_save(self, data)

		try:
			# Update base settings that don't require active controller
			self.movement_speed = float(self._settings.get(["base_speed"]))
			self.z_drawing = float(self._settings.get(["z_drawing"]))
			self.z_travel = float(self._settings.get(["z_travel"]))

			# Update debug mode if controller is active
			if self.joy is not None:
				self.joy.debug_mode = self._settings.get_boolean(["debug_mode"])

			# Update buffered controller if active
			if self.buffered_controller is not None:
				self.buffered_controller.update_settings(self._settings)
				self._logger.info("Controller settings updated successfully")

			# Log general settings update
			self._logger.info("Settings saved and applied successfully")

		except Exception as e:
			self._logger.error(f"Error updating settings: {str(e)}")
			# Re-raise to ensure UI is notified of failure
			raise

	def get_assets(self):
		return dict(
			js=["js/plasticpilot.js"],
			css=["css/plasticpilot.css"],
			less=["less/plasticpilot.less"]
		)

	def on_event(self, event, payload):
		if event == 'Connected':
			self._logger.info('Printer connected')
			self.bConnected = True
			self.bStarted = False
			self.update_printer_dimensions()
			return
		if event == 'PrinterProfileModified':
			self._logger.info('Printer profile modified')
			# Update dimensions when profile changes
			self.update_printer_dimensions()
			return
		if event == 'Disconnected':
			self._logger.info('Printer disconnected')
			self.bConnected = False
			self.bStarted = False
			return
		if event == 'PrintStarted':
			self._logger.info('Print started')
			self.bStarted = True
			return
		if event in ('PrintFailed', 'PrintDone', 'PrintCancelled'):
			self.bStarted = False
			return
		return

	def send(self, gcode):
		"""Enhanced send method with better error handling"""
		if gcode is not None and not (hasattr(self, 'joy') and self.joy.debug_mode):
			try:
				if isinstance(gcode, str):
					gcode = [gcode]	# Convert single command to list
				self._logger.info(f"Sending GCode command(s): {gcode}")
				self._printer.commands(gcode)
				time.sleep(0.05)	# Small delay after sending commands
			except Exception as e:
				self._logger.error(f"Error sending GCode command: {str(e)}")
				raise

	def on_shutdown(self):
		self._logger.info('Shutdown received...')
		self.stop_controller_thread()

	def get_api_commands(self):
		return dict(
			activate=["controller_id"],
			deactivate=[],
			refresh=[],
   			reset_settings=[]
		)

	def on_api_command(self, command, data):
		if command == "activate":
			if not self._printer.is_operational():
				return jsonify({"success": False, "error": "Printer not operational"})

			controller_id = data.get("controller_id")
			if not controller_id:
				return jsonify({"success": False, "error": "No controller ID provided"})

			try:
				self.active_controller = controller_id
				self.start_controller_thread()
				return jsonify({"success": True})
			except Exception as e:
				return jsonify({"success": False, "error": str(e)})

		elif command == "deactivate":
			try:
				self.stop_controller_thread()
				return jsonify({"success": True})
			except Exception as e:
				return jsonify({"success": False, "error": str(e)})

		elif command == "refresh":
			try:
				# Get fresh list of controllers
				controllers = self.list_available_controllers()

				# If currently active controller is no longer available, deactivate it
				if self.active_controller:
					controller_still_available = any(c["id"] == self.active_controller for c in controllers)
					if not controller_still_available:
						self._logger.info(f"Previously active controller {self.active_controller} no longer available")
						self.stop_controller_thread()
					else:
						self._logger.info(f"Active controller {self.active_controller} still available")

				return jsonify({
					"success": True,
					"controllers": controllers
				})

			except Exception as e:
				self._logger.error(f"Error during controller refresh: {str(e)}")
				self._logger.exception("Detailed error information:")
				return jsonify({
					"success": False,
					"error": str(e),
					"controllers": []
				})
		elif command == "reset_settings":
			try:
				# Get default settings
				defaults = self.get_settings_defaults()

				# Update all settings to defaults
				self._settings.set([], defaults)
				self._settings.save()

				# Update current instance settings
				self.on_settings_save(defaults)

				return jsonify({
					"success": True,
					"defaults": defaults
				})
			except Exception as e:
				self._logger.error(f"Error resetting settings: {str(e)}")
				return jsonify({
					"success": False,
					"error": str(e)
				})

		# Handle other commands as before...
		return super().on_api_command(command, data)

	def get_update_information(self):
		return dict(
			plasticpilot=dict(
				displayName="Plastic Pilot",
				displayVersion=self._plugin_version,
				type="github_release",
				user="Garr-Garr",
				repo="OctoPrint-PlasticPilot",
				current=self._plugin_version,
				pip="https://github.com/Garr-Garr/OctoPrint-PlasticPilot/archive/{target_version}.zip"
			)
		)

	def get_template_configs(self):
		return [
			dict(type="settings", custom_bindings=True)
		]

__plugin_name__ = "Plastic Pilot"
__plugin_pythoncompat__ = ">=3.7,<4"

def __plugin_load__():
	global __plugin_implementation__
	__plugin_implementation__ = PlasticPilot()

	global __plugin_hooks__
	__plugin_hooks__ = {
		"octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information
	}
