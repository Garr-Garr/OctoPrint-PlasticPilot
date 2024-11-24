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

class MovementCoordinator:
	"""
	Coordinates movement commands between UserController input and printer output.
	Integrates with existing PlasticPilot architecture.
	"""
	def __init__(self, plugin):
		self._plugin = plugin
		self._logger = plugin._logger
		self._printer = plugin._printer
		
		# Movement state
		self.target_x = 0.0
		self.target_y = 0.0
		self.target_e = 0.0
		self.current_e = 0.0
		self.last_movement_time = time.time()
		self.movement_queue = []
		
		# Configuration
		self.chunk_size = 0.5  # mm per movement chunk
		self.min_chunk = 0.1   # minimum chunk size
		self.update_interval = 0.04  # 40ms between updates
		
		# Locks
		self._movement_lock = Lock()
	
	def update_settings(self, settings):
		"""Update movement parameters from settings"""
		min_movement = float(settings.get(["min_movement"]))
		# Scale chunk size based on minimum movement setting
		self.chunk_size = min(0.5, max(0.1, min_movement * 10))
		self.min_chunk = min_movement
		
		# Update timing from settings
		self.update_interval = float(settings.get(["movement_check_interval"])) / 1000.0
	
	def should_update(self):
		"""Check if enough time has passed for next movement update"""
		current_time = time.time()
		if current_time - self.last_movement_time >= self.update_interval:
			self.last_movement_time = current_time
			return True
		return False
	
	def process_movement(self, movement_data, current_speed, time_delta):
		"""
		Process movement data from UserController and generate coordinated movement
		Returns True if movement was processed, False otherwise
		"""
		if current_speed <= 0:
			return False
			
		try:
			with self._movement_lock:
				# Calculate potential movements
				x_movement = movement_data['x_speed'] * current_speed * time_delta / 60
				y_movement = movement_data['y_speed'] * current_speed * time_delta / 60
				
				# Calculate new target positions
				new_x = max(0, min(self._plugin.maxX, self._plugin.current_x + x_movement))
				new_y = max(0, min(self._plugin.maxY, self._plugin.current_y + y_movement))
				
				# Check if movement is significant
				x_delta = new_x - self._plugin.current_x
				y_delta = new_y - self._plugin.current_y
				
				if abs(x_delta) < self.min_chunk and abs(y_delta) < self.min_chunk:
					return False
				
				# Calculate movement chunks
				chunks = self._calculate_chunks(
					self._plugin.current_x, self._plugin.current_y,
					new_x, new_y,
					current_speed
				)
				
				# Send movement chunks
				for chunk in chunks:
					self._send_chunk(chunk)
					
				# Update current position
				self._plugin.current_x = new_x
				self._plugin.current_y = new_y
				
				return True
				
		except Exception as e:
			self._logger.error(f"Error processing movement: {str(e)}")
			return False
	
	def _calculate_chunks(self, start_x, start_y, end_x, end_y, speed):
		"""Break movement into smaller chunks"""
		chunks = []
		
		# Calculate total distance
		dx = end_x - start_x
		dy = end_y - start_y
		distance = math.sqrt(dx*dx + dy*dy)
		
		if distance <= self.chunk_size:
			# Movement is small enough to be one chunk
			chunks.append({
				'x': end_x,
				'y': end_y,
				'speed': speed
			})
		else:
			# Break into multiple chunks
			num_chunks = math.ceil(distance / self.chunk_size)
			for i in range(1, num_chunks + 1):
				fraction = i / num_chunks
				chunks.append({
					'x': start_x + dx * fraction,
					'y': start_y + dy * fraction,
					'speed': speed
				})
		
		return chunks
	
	def _send_chunk(self, chunk):
		"""Send a movement chunk to the printer"""
		try:
			gcode = f'G1 X{chunk["x"]:.3f} Y{chunk["y"]:.3f} F{chunk["speed"]}'
			self._printer.commands([gcode])
			# Use very small delay between chunks
			time.sleep(0.001)
		except Exception as e:
			self._logger.error(f"Error sending movement chunk: {str(e)}")
	
	def process_extrusion(self, amount, feedrate):
		"""Handle extrusion with movement coordination"""
		try:
			with self._movement_lock:
				self.current_e += amount
				gcode = f'G1 E{self.current_e:.3f} F{feedrate}'
				self._printer.commands([gcode])
				return True
		except Exception as e:
			self._logger.error(f"Error processing extrusion: {str(e)}")
			return False

	def emergency_stop(self):
		"""Emergency stop all movement"""
		try:
			with self._movement_lock:
				self._printer.commands(['M410'])  # Emergency stop
				# Reset targets to current position
				self.target_x = self._plugin.current_x
				self.target_y = self._plugin.current_y
		except Exception as e:
			self._logger.error(f"Error during emergency stop: {str(e)}")

class UserController:
	def __init__(self):
		self.reset_state()
		self.max_analog_val = math.pow(2, 15)
		self.debug_mode = False
		self._logger = logging.getLogger("octoprint.plugins.plasticpilot")
	
		# Adjusted threshold configurations for better 180-degree control
		self.deadzone_threshold = 0.10  # Reduced deadzone for more responsive control
		self.walk_threshold = 0.40      # Adjusted for smoother transition
		self.run_threshold = 0.75       # Adjusted for better speed control
	
		# Adjusted speed multipliers for smoother acceleration
		self.walk_speed_multiplier = 0.2   # Slower initial speed for precision
		self.run_speed_multiplier = 0.6    # Medium speed for controlled movement
		self.max_speed_multiplier = 1.0    # Full speed
	
		# Button states
		self.right_trigger = 0.0
		self.left_trigger = 0.0
		self.right_button = False
		self.left_button = False
		
		# Movement smoothing
		self.smoothing_factor = 0.2    # Increased smoothing for more fluid movement
		self.last_x_speed = 0.0
		self.last_y_speed = 0.0

	def reset_state(self):
		# Analog inputs with explicit zero state
		self.left_x = 0.0
		self.left_y = 0.0
		self.right_x = 0.0
		self.right_y = 0.0
		self.left_trigger = 0.0
		self.right_trigger = 0.0
		self.right_trigger = 0.0
		self.left_trigger = 0.0
		
		# Movement state tracking
		self.current_movement_state = "idle"  # idle, walking, running, max_speed
		self.last_movement_time = time.time()
		self.has_new_movement = False
		
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

	def process_movement(self, axis, current_value, last_speed):
		"""Process movement with enhanced smoothing"""
		normalized, multiplier, state = self.calculate_movement_speed(current_value)
		
		# Enhanced smoothing with acceleration curve
		if abs(normalized) > abs(last_speed):
			# Accelerating - use less smoothing for more responsive acceleration
			smoothing = self.smoothing_factor * 0.5
		else:
			# Decelerating - use full smoothing for gentler stops
			smoothing = self.smoothing_factor
			
		smoothed_speed = (normalized * (1 - smoothing) + last_speed * smoothing)
	
		return smoothed_speed, state

	def get_movement(self):
		"""
		Get current movement values with smoothing and state information.
		Left stick controls X-axis (left/right)
		Right stick controls Y-axis (up/down)
		"""
		# Process X movement (left/right on left stick)
		new_x_speed, x_state = self.process_movement('X', self.left_x, self.last_x_speed)
	
		# Process Y movement (up/down on right stick)
		# Invert Y axis because joystick up is negative but we want positive Y movement
		new_y_speed, y_state = self.process_movement('Y', -self.right_y, self.last_y_speed)
	
		# Update last speeds for next iteration
		self.last_x_speed = new_x_speed
		self.last_y_speed = new_y_speed
	
		# Determine overall movement state (use the faster of the two axes)
		states = {"idle": 0, "walking": 1, "running": 2, "max_speed": 3}
		x_state_val = states[x_state]
		y_state_val = states[y_state]
		overall_state = max(x_state, y_state, key=lambda s: states[s])
	
		movement_data = {
			'x_speed': new_x_speed,
			'y_speed': new_y_speed,
			'movement_state': overall_state,
			'x_state': x_state,
			'y_state': y_state
		}
	
		if self.debug_mode:
			self._logger.info(f"Movement data: {movement_data}")
	
		return movement_data

	def process_event(self, event):
		"""
		Process controller events.
		Left stick is exclusively for X movement.
		Right stick is exclusively for Y movement.
		"""
		try:
			if self.debug_mode:
				self._logger.info(f"Raw event: {event.ev_type} - {event.code} - {event.state}")
				
			if event.ev_type == "Absolute":
				if event.code == "ABS_X":  # Left stick X axis only
					self.left_x = event.state
					self.has_new_movement = True
				elif event.code == "ABS_RY":  # Right stick Y axis only
					self.right_y = event.state
					self.has_new_movement = True
				elif event.code == "ABS_Z":  # Left trigger
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
	
	def calculate_movement_speed(self, raw_value):
		"""
		Calculate movement speed with improved 180-degree linear control
		"""
		# Normalize the raw value to -1.0 to 1.0
		normalized = raw_value / self.max_analog_val
		abs_normalized = abs(normalized)
		
		# Apply deadzone
		if abs_normalized < self.deadzone_threshold:
			return (0.0, 0.0, "idle")
		
		# Linear scaling for more predictable movement
		# Map the active range (deadzone to 1.0) to 0.0 to 1.0
		scaled = (abs_normalized - self.deadzone_threshold) / (1.0 - self.deadzone_threshold)
		scaled = min(1.0, max(0.0, scaled))  # Clamp between 0 and 1
		
		# Determine movement state and calculate multiplier
		if scaled < self.walk_threshold:
			# Walking - linear scaling in precision range
			state = "walking"
			multiplier = (scaled / self.walk_threshold) * self.walk_speed_multiplier
		elif scaled < self.run_threshold:
			# Running - linear scaling in medium speed range
			state = "running"
			progress = (scaled - self.walk_threshold) / (self.run_threshold - self.walk_threshold)
			multiplier = self.walk_speed_multiplier + (progress * (self.run_speed_multiplier - self.walk_speed_multiplier))
		else:
			# Maximum speed - linear scaling in high speed range
			state = "max_speed"
			progress = (scaled - self.run_threshold) / (1.0 - self.run_threshold)
			multiplier = self.run_speed_multiplier + (progress * (self.max_speed_multiplier - self.run_speed_multiplier))
		
		# Apply direction while maintaining linear response
		final_speed = math.copysign(scaled * multiplier, normalized)
		
		return (final_speed, multiplier, state)

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
  
		self._extrusion_lock = Lock()  # For protecting extrusion operations
		self.current_e_feedrate = 2.0  # Default extruder feedrate in mm/s

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
   
	def handle_extrusion(self):
		"""Handle extrusion based on trigger states"""
		if not self.joy:
			return

		try:
			with self._extrusion_lock:
				# Track cumulative extrusion amount
				if not hasattr(self, 'current_e_position'):
					self.current_e_position = 0.0

				# Handle extrusion with right trigger
				if self.joy.right_trigger > 0.1:  # Small deadzone
					# Set absolute extrusion mode
					self.send("M82")  # Switch to absolute E movements
					
					# Convert current_e_feedrate from mm/s to mm/min for G-code
					e_feedrate_mmmin = self.current_e_feedrate * 60
					amount = float(self._settings.get(["extrusion_amount"]))
					# Scale amount by trigger pressure
					scaled_amount = amount * self.joy.right_trigger
					# Add to current position
					self.current_e_position += scaled_amount
					
					gcode = f"G1 E{self.current_e_position:.3f} F{e_feedrate_mmmin:.1f}"
					self.send(gcode)
					if self.joy.debug_mode:
						self._logger.info(f"Extruding: {scaled_amount:.3f}mm at {self.current_e_feedrate:.1f}mm/s (Total: {self.current_e_position:.3f}mm)")

				# Handle retraction with left trigger
				elif self.joy.left_trigger > 0.1:  # Small deadzone
					# Set absolute extrusion mode
					self.send("M82")  # Switch to absolute E movements
					
					retraction_speed = float(self._settings.get(["retraction_speed"]))
					# Convert retraction speed from mm/s to mm/min for G-code
					retraction_feedrate = retraction_speed * 60
					amount = float(self._settings.get(["retraction_amount"]))
					# Scale amount by trigger pressure
					scaled_amount = amount * self.joy.left_trigger
					# Subtract from current position
					self.current_e_position -= scaled_amount
					
					gcode = f"G1 E{self.current_e_position:.3f} F{retraction_feedrate:.1f}"
					self.send(gcode)
					if self.joy.debug_mode:
						self._logger.info(f"Retracting: {scaled_amount:.3f}mm at {retraction_speed:.1f}mm/s (Total: {self.current_e_position:.3f}mm)")

		except Exception as e:
			self._logger.error(f"Error during extrusion handling: {str(e)}")


	def handle_feedrate(self):
		"""Handle extruder feedrate adjustments based on button states"""
		if not self.joy:
			return

		try:
			increment = float(self._settings.get(["feedrate_increment"]))
			min_feedrate = float(self._settings.get(["min_feedrate"]))
			max_feedrate = float(self._settings.get(["max_feedrate"]))

			if self.joy.right_button:
				# Increase feedrate
				self.current_e_feedrate = min(self.current_e_feedrate + increment, max_feedrate)
				if self.joy.debug_mode:
					self._logger.info(f"Increased extruder feedrate to: {self.current_e_feedrate:.1f} mm/s")
				time.sleep(0.1)  # Debounce

			elif self.joy.left_button:
				# Decrease feedrate
				self.current_e_feedrate = max(self.current_e_feedrate - increment, min_feedrate)
				if self.joy.debug_mode:
					self._logger.info(f"Decreased extruder feedrate to: {self.current_e_feedrate:.1f} mm/s")
				time.sleep(0.1)  # Debounce

		except Exception as e:
			self._logger.error(f"Error during feedrate handling: {str(e)}")


	def threadAcceptInput(self):
		"""Enhanced thread function with coordinated movement processing"""
		self._logger.info('Initializing controller mode' + 
						(' (DEBUG MODE)' if self.joy.debug_mode else ''))
		
		# Initialize control parameters
		error_count = 0
		max_errors = 10
		last_movement_time = time.time()
		
		# Initialize thread parameters
		self._update_thread_parameters()
		
		# Update UserController thresholds from settings
		self.joy.deadzone_threshold = self._thread_parameters['deadzone_threshold']
		self.joy.walk_threshold = self._thread_parameters['walk_threshold']
		self.joy.run_threshold = self._thread_parameters['run_threshold']
		self.joy.walk_speed_multiplier = self._thread_parameters['walk_speed_multiplier']
		self.joy.run_speed_multiplier = self._thread_parameters['run_speed_multiplier']
		self.joy.max_speed_multiplier = self._thread_parameters['max_speed_multiplier']
		self.joy.smoothing_factor = float(self._settings.get(["smoothing_factor"])) / 100.0
		
		# Initialize movement coordinator
		movement_coordinator = MovementCoordinator(self)
		movement_coordinator.update_settings(self._settings)
		
		# Speed settings for different movement states
		base_speed = self._thread_parameters['base_speed']
		speed_settings = {
			'idle': 0,
			'walking': base_speed * self._thread_parameters['walk_speed_multiplier'],
			'running': base_speed * self._thread_parameters['run_speed_multiplier'],
			'max_speed': base_speed * self._thread_parameters['max_speed_multiplier']
		}
		
		while not self._stop_event.is_set():
			try:
				if not self.bConnected or not self.joy:
					error_count += 1
					if error_count >= max_errors:
						self._logger.error("Connection lost or controller disconnected")
						break
					time.sleep(0.1)
					continue
				
				# Read controller state
				if not self.joy.read():
					error_count += 1
					if error_count >= max_errors:
						self._logger.error("Failed to read controller state")
						break
					continue
				
				error_count = 0  # Reset error count on successful read
				current_time = time.time()
				
				# Get and process movement data
				movement_data = self.joy.get_movement()
				current_speed = speed_settings[movement_data['movement_state']]
				
				# Process movement if coordinator indicates it's time
				if movement_coordinator.should_update():
					time_delta = current_time - last_movement_time
					if movement_coordinator.process_movement(movement_data, current_speed, time_delta):
						if self.joy.debug_mode:
							self._logger.info(
								f"Moving to X:{self.current_x:.2f} Y:{self.current_y:.2f} "
								f"(State: {movement_data['movement_state']}, "
								f"Speed: {current_speed:.1f} mm/min)"
							)
					last_movement_time = current_time

				# Handle extrusion with right trigger
				if self.joy.right_trigger > 0.1:
					e_feedrate_mmmin = self.current_e_feedrate * 60
					amount = float(self._settings.get(["extrusion_amount"])) * self.joy.right_trigger
					movement_coordinator.process_extrusion(amount, e_feedrate_mmmin)
					if self.joy.debug_mode:
						self._logger.info(f"Extruding: {amount:.3f}mm at {self.current_e_feedrate:.1f}mm/s")

				# Handle retraction with left trigger
				elif self.joy.left_trigger > 0.1:
					retraction_speed = float(self._settings.get(["retraction_speed"]))
					retraction_feedrate = retraction_speed * 60
					amount = float(self._settings.get(["retraction_amount"])) * self.joy.left_trigger
					movement_coordinator.process_extrusion(-amount, retraction_feedrate)
					if self.joy.debug_mode:
						self._logger.info(f"Retracting: {amount:.3f}mm at {retraction_speed:.1f}mm/s")

				# Handle feedrate adjustments
				self.handle_feedrate()
				
				# Process button actions
				if self.joy.a_pressed:
					self.drawing = not self.drawing
					z_height = self.z_drawing if self.drawing else self.z_travel
					gcode = f'G1 Z{z_height} F1000'
					self._logger.info(f"Toggling drawing mode: {'Drawing' if self.drawing else 'Travel'}")
					self.send(gcode)
					time.sleep(0.1)  # Debounce
				
				if self.joy.b_pressed:
					self._logger.info("Homing XY axes")
					self.send("G28 X Y")
					self.current_x = 0.0
					self.current_y = 0.0
					movement_coordinator.target_x = 0.0
					movement_coordinator.target_y = 0.0
					time.sleep(0.1)  # Debounce
				
				# if self.joy.y_pressed:
				# 	self._logger.info("Initiating shake clear")
				# 	self.shake_clear()
				# 	movement_coordinator.target_x = 0.0
				# 	movement_coordinator.target_y = 0.0
				# 	time.sleep(0.1)  # Debounce
				
				# Small sleep to prevent CPU thrashing
				threading.Event().wait(0.005)
				
			except Exception as e:
				self._logger.error(f"Error in control thread: {str(e)}")
				error_count += 1
				if error_count >= max_errors:
					self._logger.error("Too many errors, stopping controller thread")
					break
		
		self._logger.info('Controller thread terminated')

	def _update_thread_parameters(self):
		"""Update running thread parameters based on current settings"""
		if not hasattr(self, '_thread_parameters'):
			self._thread_parameters = {}
			
		self._thread_parameters.update({
			'movement_check_interval': float(self._settings.get(["movement_check_interval"])) / 1000.0,
			'command_delay': float(self._settings.get(["command_delay"])) / 1000.0,
			'min_movement': float(self._settings.get(["min_movement"])),
			'base_speed': float(self._settings.get(["base_speed"])),
			'deadzone_threshold': float(self._settings.get(["deadzone_threshold"])) / 100.0,
			'walk_threshold': float(self._settings.get(["walk_threshold"])) / 100.0,
			'run_threshold': float(self._settings.get(["run_threshold"])) / 100.0,
			'walk_speed_multiplier': float(self._settings.get(["walk_speed_multiplier"])) / 100.0,
			'run_speed_multiplier': float(self._settings.get(["run_speed_multiplier"])) / 100.0,
			'max_speed_multiplier': float(self._settings.get(["max_speed_multiplier"])) / 100.0
		})

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

	# def shake_clear(self):
	# 	# Lift the pen
	# 	self.drawing = False
	# 	self.send(f'G1 Z{self.z_travel} F1000')

	# 	# Perform rapid zigzag motion
	# 	for i in range(4):
	# 		self.send(f'G1 X{5} Y{5} F3000')
	# 		self.send(f'G1 X{self.maxX-5} Y{self.maxY-5} F3000')
	# 		self.send(f'G1 X{self.maxX-5} Y{5} F3000')
	# 		self.send(f'G1 X{5} Y{self.maxY-5} F3000')

	# 	# Return to starting position
	# 	self.current_x = 0
	# 	self.current_y = 0
	# 	self.send('G28 X Y')

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
		# Call parent implementation first to save all settings
		octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
		
		try:
			# If we have an active controller, update its settings
			if self.joy is not None:
				# Update thresholds
				self.joy.deadzone_threshold = float(self._settings.get(["deadzone_threshold"])) / 100.0
				self.joy.walk_threshold = float(self._settings.get(["walk_threshold"])) / 100.0
				self.joy.run_threshold = float(self._settings.get(["run_threshold"])) / 100.0
				
				# Update speed multipliers
				self.joy.walk_speed_multiplier = float(self._settings.get(["walk_speed_multiplier"])) / 100.0
				self.joy.run_speed_multiplier = float(self._settings.get(["run_speed_multiplier"])) / 100.0
				self.joy.max_speed_multiplier = float(self._settings.get(["max_speed_multiplier"])) / 100.0
				
				# Update smoothing
				self.joy.smoothing_factor = float(self._settings.get(["smoothing_factor"])) / 100.0
				
				# Update debug mode
				self.joy.debug_mode = self._settings.get_boolean(["debug_mode"])
				
			# Update base movement settings
			self.movement_speed = float(self._settings.get(["base_speed"]))
			self.z_drawing = float(self._settings.get(["z_drawing"]))
			self.z_travel = float(self._settings.get(["z_travel"]))
			
			# Update thread parameters if the thread is running
			if self.controller_thread is not None and self.controller_thread.is_alive():
				self._update_thread_parameters()
				self._logger.info("Updated controller settings successfully")
				
		except Exception as e:
			self._logger.error(f"Error updating settings: {str(e)}")

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
