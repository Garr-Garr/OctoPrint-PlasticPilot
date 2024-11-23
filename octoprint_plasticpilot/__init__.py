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
import math
import time
import logging
import json


class UserController:
	def __init__(self):
		self.reset_state()
		self.max_analog_val = math.pow(2, 15)
		self.debug_mode = False
		self._logger = logging.getLogger("octoprint.plugins.plasticpilot")
		self.movement_threshold = 0.15  # Threshold for stick movement
		self.last_processed_time = time.time()
		self.process_interval = 0.05  # Process every 50ms

	def reset_state(self):
		# Analog inputs with explicit zero state
		self.left_x = 0.0
		self.left_y = 0.0
		self.right_x = 0.0
		self.right_y = 0.0
		self.left_trigger = 0.0
		self.right_trigger = 0.0

		# State tracking
		self.has_new_movement = False
		self.last_movement_time = time.time()

		# Buttons
		self.a_pressed = False
		self.b_pressed = False
		self.x_pressed = False
		self.y_pressed = False
		self.start_pressed = False
		self.back_pressed = False
		self.left_bumper = False
		self.right_bumper = False
		self.left_thumb = False
		self.right_thumb = False

	def process_event(self, event):
		"""Process controller events with improved state tracking"""
		try:
			# Always log raw events when debug is enabled
			if self.debug_mode:
				self._logger.info(f"Raw Controller Event - Type: {event.ev_type}, Code: {event.code}, State: {event.state}")

			# Process the event based on its type
			if event.ev_type == "Absolute":  # Analog inputs
				# Normalize and apply deadzone
				if event.code == "ABS_X":
					raw_value = event.state / self.max_analog_val
					new_value = 0.0 if abs(raw_value) < self.movement_threshold else raw_value
					if abs(new_value - self.left_x) > 0.01:  # Only update if change is significant
						self.left_x = new_value
						self.has_new_movement = True
						if not self.debug_mode:
							self._logger.info(f"Left X updated: {self.left_x:.3f}")

				elif event.code == "ABS_Y":
					raw_value = event.state / self.max_analog_val
					new_value = 0.0 if abs(raw_value) < self.movement_threshold else raw_value
					if abs(new_value - self.left_y) > 0.01:
						self.left_y = new_value
						self.has_new_movement = True
						if not self.debug_mode:
							self._logger.info(f"Left Y updated: {self.left_y:.3f}")

				elif event.code == "ABS_RX":
					raw_value = event.state / self.max_analog_val
					new_value = 0.0 if abs(raw_value) < self.movement_threshold else raw_value
					if abs(new_value - self.right_x) > 0.01:
						self.right_x = new_value
						self.has_new_movement = True
						if not self.debug_mode:
							self._logger.info(f"Right X updated: {self.right_x:.3f}")

				elif event.code == "ABS_RY":
					raw_value = event.state / self.max_analog_val
					new_value = 0.0 if abs(raw_value) < self.movement_threshold else raw_value
					if abs(new_value - self.right_y) > 0.01:
						self.right_y = new_value
						self.has_new_movement = True
						if not self.debug_mode:
							self._logger.info(f"Right Y updated: {self.right_y:.3f}")

			elif event.ev_type == "Key":  # Button inputs
				if event.code == "BTN_SOUTH":  # A button
					self.a_pressed = event.state == 1
				elif event.code == "BTN_EAST":  # B button
					self.b_pressed = event.state == 1
				elif event.code == "BTN_WEST":  # X button
					self.x_pressed = event.state == 1
				elif event.code == "BTN_NORTH":  # Y button
					self.y_pressed = event.state == 1

			return True
		except Exception as e:
			self._logger.error(f"Error processing controller event: {str(e)}")
			return False

	def read(self):
		"""Read and process all pending controller events with improved error handling"""
		try:
			events = get_gamepad()
			if not events:  # If no events, maintain current state
				return True

			for event in events:
				if not self.process_event(event):
					self._logger.error("Failed to process event")
					continue

			return True
		except Exception as e:
			self._logger.error(f"Error reading gamepad: {str(e)}")
			return False

	def get_movement(self):
		"""Get current movement values"""
		return {
			'left_x': self.left_x if abs(self.left_x) > self.movement_threshold else 0,
			'left_y': self.left_y if abs(self.left_y) > self.movement_threshold else 0,
			'right_x': self.right_x if abs(self.right_x) > self.movement_threshold else 0,
			'right_y': self.right_y if abs(self.right_y) > self.movement_threshold else 0
		}

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
		self.controller_thread = None  # Initialize the controller thread
		self.active_controller = None  # Initialize the active controller

		self._position_lock = Lock()  # For protecting position updates
		self._state_lock = Lock()     # For protecting state variables
		self._stop_event = Event()    # For clean thread shutdown

		# Add logger
		self._logger = logging.getLogger("octoprint.plugins.plasticpilot")


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
		"""Start the controller input thread"""
		if self.controller_thread is not None and self.controller_thread.is_alive():
			self._logger.info("Controller thread already running")
			return

		self._stop_event.clear()  # Reset the stop event
		try:
			self.joy = UserController()
			# Add explicit debug logging for debug mode status
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

			# Test logging to verify logger functionality
			self._logger.info("Testing logger functionality")

			self.controller_thread = Thread(target=self.threadAcceptInput)
			self.controller_thread.daemon = True
			self.controller_thread.start()
			self._plugin_manager.send_plugin_message(self._identifier, {
				"type": "controller_status",
				"active": True,
				"controller_id": self.active_controller
			})
			self._logger.info(f"Controller thread started (Debug Mode: {debug_mode})")
		except Exception as e:
			self._logger.error(f"Failed to start controller thread: {str(e)}")
			raise

	def stop_controller_thread(self):
		"""Stop the controller input thread with proper cleanup"""
		if self.controller_thread is None:
			return

		self._logger.info("Initiating controller shutdown...")

		try:
			# Signal the thread to stop
			self._stop_event.set()

			# Give the thread time to finish its current iteration
			shutdown_timeout = 3.0  # seconds
			self._logger.info(f"Waiting up to {shutdown_timeout} seconds for thread to stop...")

			# Wait for thread to finish with timeout
			start_time = time.time()
			while self.controller_thread.is_alive():
				if time.time() - start_time > shutdown_timeout:
					self._logger.warning("Thread shutdown timed out, forcing termination")
					break
				time.sleep(0.1)

			# If thread is still alive after timeout, try one more time
			if self.controller_thread.is_alive():
				self._logger.warning("Thread still alive after timeout, attempting final cleanup")
				try:
					self.controller_thread.join(timeout=1.0)
				except Exception as e:
					self._logger.error(f"Error during final thread cleanup: {str(e)}")

			# Clean up resources
			if hasattr(self, 'joy') and self.joy is not None:
				self._logger.info("Cleaning up controller resources...")
				try:
					del self.joy
				except Exception as e:
					self._logger.error(f"Error cleaning up controller object: {str(e)}")
			self.joy = None

			# Reset the thread
			self.controller_thread = None

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
			self.controller_thread = None

	def move_to_position(self):
		"""Enhanced position updates with better error handling"""
		try:
			gcode = f'G1 X{self.current_x:.2f} Y{self.current_y:.2f} F{self.movement_speed}'
			self._logger.info(f"Sending movement: {gcode}")
			self._printer.commands([gcode])
		except Exception as e:
			self._logger.error(f"Error sending movement command: {str(e)}")


	def threadAcceptInput(self):
		"""Enhanced thread function with continuous movement processing"""
		self._logger.info('Etch-A-Sketch mode initialized' +
						 (' (DEBUG MODE)' if self.joy.debug_mode else ''))

		error_count = 0
		max_errors = 10
		last_movement_time = time.time()
		movement_interval = 0.1  # Process movement every 100ms

		while not self._stop_event.is_set():
			try:
				if not self.bConnected or not self.joy:
					error_count += 1
					if error_count >= max_errors:
						self._logger.error("Connection lost")
						break
					continue

				# Read controller state
				if not self.joy.read():
					error_count += 1
					if error_count >= max_errors:
						self._logger.error("Failed to read controller")
						break
					continue

				error_count = 0  # Reset error count on successful read
				current_time = time.time()

				# Process movement if enough time has passed
				if current_time - last_movement_time >= movement_interval:
					movement = self.joy.get_movement()

					# Process X movement
					if abs(movement['left_x']) > self.joy.movement_threshold:
						with self._position_lock:
							move_x = movement['left_x'] * 1.5
							new_x = max(0, min(self.maxX, self.current_x + move_x))
							if new_x != self.current_x:
								self.current_x = new_x
								self._logger.info(f"Moving X to: {self.current_x:.2f}")
								self.move_to_position()
								last_movement_time = current_time

					# Process Y movement
					if abs(movement['right_y']) > self.joy.movement_threshold:
						with self._position_lock:
							move_y = movement['right_y'] * 1.5
							new_y = max(0, min(self.maxY, self.current_y + move_y))
							if new_y != self.current_y:
								self.current_y = new_y
								self._logger.info(f"Moving Y to: {self.current_y:.2f}")
								self.move_to_position()
								last_movement_time = current_time

				# Process button presses (immediate)
				if self.joy.a_pressed:
					self.drawing = not self.drawing
					gcode = f'G1 Z{self.z_drawing if self.drawing else self.z_travel} F1000'
					self._logger.info(f"Sending Z movement: {gcode}")
					self.send(gcode)

				if self.joy.b_pressed:
					self._logger.info("Homing XY")
					self.send("G28 XY")
					self.current_x = 0.0
					self.current_y = 0.0

				if self.joy.y_pressed:
					self._logger.info("Initiating shake clear")
					self.shake_clear()

				# Small sleep to prevent CPU thrashing
				threading.Event().wait(0.01)

			except Exception as e:
				self._logger.error(f"Error in thread: {str(e)}")
				error_count += 1
				if error_count >= max_errors:
					break

		self._logger.info('Etch-A-Sketch mode terminated cleanly')


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

	def shake_clear(self):
		"""Simulate the etch-a-sketch shake clear motion"""
		# Lift the pen
		self.drawing = False
		self.send(f'G1 Z{self.z_travel} F1000')

		# Perform rapid zigzag motion
		for i in range(4):
			self.send(f'G1 X{5} Y{5} F3000')
			self.send(f'G1 X{self.maxX-5} Y{self.maxY-5} F3000')
			self.send(f'G1 X{self.maxX-5} Y{5} F3000')
			self.send(f'G1 X{5} Y{self.maxY-5} F3000')

		# Return to starting position
		self.current_x = 0
		self.current_y = 0
		self.send('G28 X Y')

	def on_after_startup(self):
		self._logger.info("Etch-A-Sketch Controller starting up")
		self._logger.info(f"Available routes: {app.url_map}")
		self.update_printer_dimensions()

	def get_settings_defaults(self):
		return dict(
			max_x=200.0,
			max_y=200.0,
			z_drawing=0.1,
			z_travel=1.0,
			base_speed=1000,
			debug_mode=False
		)

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
					gcode = [gcode]  # Convert single command to list
				self._logger.info(f"Sending GCode command(s): {gcode}")
				self._printer.commands(gcode)
				time.sleep(0.05)  # Small delay after sending commands
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
__plugin_pythoncompat__ = ">=2.7,<4"

def __plugin_load__():
	global __plugin_implementation__
	__plugin_implementation__ = PlasticPilot()

	global __plugin_hooks__
	__plugin_hooks__ = {
		"octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information
	}
