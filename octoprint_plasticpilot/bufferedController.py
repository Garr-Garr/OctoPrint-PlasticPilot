from threading import Thread, Lock, Event
import threading
from collections import deque
from queue import Queue
from dataclasses import dataclass
from typing import List, Deque, Optional
import math
import time
import logging

from .models import (
	ControllerState,
	MovementCommand,
	MovementVector,
	AccelerationProfile,
	MovementCoordinator
)

class BufferedController:
	"""Enhanced version of BufferedController with improved movement handling"""
	def __init__(self, plugin, acceleration=1200):
		# Remove super().__init__ call since we don't inherit from anything
		self.plugin = plugin
		self.logger = logging.getLogger("octoprint.plugins.plasticpilot")

		# Initialize threading controls
		self._stop_event = Event()
		self._buffer_lock = Lock()
		self._input_buffer = deque(maxlen=100)
		self._command_queue = Queue()

		# Movement settings (will be updated from plugin settings)
		self.base_speed = 3000  # mm/min
		self.input_poll_rate = 0.025  # 25ms
		self.movement_update_rate = 0.020  # 20ms
		self.min_movement = 0.5  # mm
		self.max_extrusion_per_mm = 0.1  # mm of filament per mm moved
		self.current_x = 0.0
		self.current_y = 0.0
		self.current_e = 0.0

		# Initialize movement coordinator with acceleration profile
		self.movement_coordinator = MovementCoordinator(
			acceleration=acceleration,
			max_speed=self.base_speed/60  # Convert mm/min to mm/s
		)

		# Enhanced movement parameters
		self.input_smoothing = 0.8  # Exponential smoothing factor
		self.last_x = 0.0
		self.last_y = 0.0
		self.last_speed = 0.0

		# Movement accumulation
		self.accumulated_x = 0.0
		self.accumulated_y = 0.0

		# Position prediction
		self.predicted_position = {'x': 0.0, 'y': 0.0}
		self.position_history = deque(maxlen=10)

		# Enhanced threading control
		self._movement_event = threading.Event()
		self._processing_active = False

	def update_settings(self, settings):
		"""Update controller settings from plugin settings"""
		self.base_speed = float(settings.get(["base_speed"]))
		self.input_poll_rate = float(settings.get(["movement_check_interval"])) / 1000.0  # Convert ms to seconds
		self.movement_update_rate = float(settings.get(["command_delay"])) / 1000.0  # Convert ms to seconds
		self.min_movement = float(settings.get(["min_movement"]))
		self.input_smoothing = 1.0 - (float(settings.get(["smoothing_factor"])) / 100.0)  # Convert percentage to factor

		# Update movement coordinator parameters
		self.movement_coordinator.x_profile.max_speed = self.base_speed / 60
		self.movement_coordinator.y_profile.max_speed = self.base_speed / 60
		self.movement_coordinator.min_movement = self.min_movement

	def start(self):
		"""Enhanced start method with improved thread coordination"""
		self._stop_event.clear()
		self._movement_event.clear()
		self._processing_active = True

		# Start input polling thread with higher priority
		self._input_thread = threading.Thread(
			target=self._input_polling_loop,
			name="PlasticPilot_Input"
		)
		self._input_thread.daemon = True
		self._input_thread.start()

		# Start movement processing thread
		self._movement_thread = threading.Thread(
			target=self._movement_processing_loop,
			name="PlasticPilot_Movement"
		)
		self._movement_thread.daemon = True
		self._movement_thread.start()

		self.logger.info("Enhanced buffered controller started with acceleration handling")

	def stop(self):
		"""Enhanced stop method with graceful shutdown"""
		self.logger.info("Initiating enhanced controller shutdown...")
		self._processing_active = False
		self._stop_event.set()
		self._movement_event.set()  # Wake up movement thread

		try:
			# Stop input thread
			if self._input_thread and self._input_thread.is_alive():
				self._input_thread.join(timeout=1.0)
				if self._input_thread.is_alive():
					self.logger.warning("Input thread failed to stop gracefully")

			# Stop movement thread
			if self._movement_thread and self._movement_thread.is_alive():
				self._movement_thread.join(timeout=1.0)
				if self._movement_thread.is_alive():
					self.logger.warning("Movement thread failed to stop gracefully")

			# Clear buffers
			with self._buffer_lock:
				self._input_buffer.clear()
			while not self._command_queue.empty():
				self._command_queue.get()

			# Reset movement state
			self.movement_coordinator.x_profile.current_speed = 0
			self.movement_coordinator.y_profile.current_speed = 0
			self.position_history.clear()

		except Exception as e:
			self.logger.error(f"Error during enhanced controller shutdown: {str(e)}")
		finally:
			self.logger.info("Enhanced controller shutdown complete")

	def _input_polling_loop(self):
			"""Enhanced input polling with better timing control"""
			last_poll_time = time.time()
			polling_interval = self.input_poll_rate
			missed_polls = 0

			while self._processing_active and not self._stop_event.is_set():
				try:
					current_time = time.time()
					elapsed = current_time - last_poll_time

					if elapsed >= polling_interval:
						# Check for missed polls
						if elapsed > polling_interval * 2:
							missed_polls += 1
							if missed_polls > 10:
								self.logger.warning("Input polling is falling behind")
						else:
							missed_polls = 0

						if self.plugin.joy and self.plugin.joy.read():
							# Handle button presses with edge detection
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

								# Signal movement thread if buffer is getting full
								if len(self._input_buffer) > self._input_buffer.maxlen * 0.8:
									self._movement_event.set()

						last_poll_time = current_time

					# Dynamic sleep to maintain target polling rate
					sleep_time = max(0, polling_interval - (time.time() - current_time))
					if sleep_time > 0:
						time.sleep(sleep_time)

				except Exception as e:
					self.logger.error(f"Error in input polling loop: {str(e)}")
					time.sleep(0.1)  # Prevent tight error loop

	def _movement_processing_loop(self):
		"""Enhanced movement processing with adaptive timing"""
		last_process_time = time.time()
		min_process_interval = self.movement_update_rate
		processing_delays = deque(maxlen=10)  # Track processing times

		while self._processing_active and not self._stop_event.is_set():
			try:
				current_time = time.time()
				elapsed = current_time - last_process_time

				if elapsed >= min_process_interval:
					start_time = time.time()
					self._process_buffered_input()

					# Track processing time
					process_time = time.time() - start_time
					processing_delays.append(process_time)

					# Adjust interval if needed
					if len(processing_delays) >= 5:
						avg_delay = sum(processing_delays) / len(processing_delays)
						if avg_delay > min_process_interval * 0.8:
							# Processing is taking too long, increase interval
							min_process_interval = min(0.1, avg_delay * 1.2)
							self.logger.debug(f"Adjusted movement interval to {min_process_interval*1000:.1f}ms")

					last_process_time = current_time
					self._movement_event.clear()
				else:
					# Wait for next processing cycle or buffer signal
					wait_time = min_process_interval - elapsed
					self._movement_event.wait(timeout=wait_time)

			except Exception as e:
				self.logger.error(f"Error in movement processing loop: {str(e)}")
				time.sleep(0.1)  # Prevent tight error loop

	def _handle_buttons(self):
		"""Enhanced button handling with edge detection"""
		try:
			current_time = time.time()

			# Store button states for edge detection
			if not hasattr(self, '_last_button_states'):
				self._last_button_states = {
					'a': False,
					'b': False,
					'x': False,
					'y': False,
					'right': False,
					'left': False
				}
				self._last_button_time = current_time

			# Implement debouncing
			if current_time - self._last_button_time < 0.1:  # 100ms debounce
				return

			if self.plugin.joy.a_pressed and not self._last_button_states['a']:
				self.plugin.drawing = not self.plugin.drawing
				z_height = self.plugin.z_drawing if self.plugin.drawing else self.plugin.z_travel
				gcode = f'G1 Z{z_height} F1000'
				self.logger.info(f"Toggling drawing mode: {'Drawing' if self.plugin.drawing else 'Travel'}")
				self.plugin.send(gcode)
				self._last_button_time = current_time

			if self.plugin.joy.b_pressed and not self._last_button_states['b']:
				self.logger.info("Homing XY axes")
				self.plugin.send("G28 X Y")
				self.current_x = 0.0
				self.current_y = 0.0
				self.plugin.current_x = 0.0
				self.plugin.current_y = 0.0
				self._last_button_time = current_time

			# Update button states
			self._last_button_states = {
				'a': self.plugin.joy.a_pressed,
				'b': self.plugin.joy.b_pressed,
				'x': self.plugin.joy.x_pressed,
				'y': self.plugin.joy.y_pressed,
				'right': self.plugin.joy.right_button,
				'left': self.plugin.joy.left_button
			}

			# Handle feedrate adjustments
			self._handle_feedrate()

		except Exception as e:
			self.logger.error(f"Error handling buttons: {str(e)}")

	def _handle_feedrate(self):
		"""Handle feedrate adjustments from controller buttons"""
		try:
			if self.plugin.joy.right_button and not self._last_button_states['right']:
				# Increase feedrate
				new_feedrate = min(
					self.plugin.current_e_feedrate + float(self.plugin._settings.get(["feedrate_increment"])) / 60,
					float(self.plugin._settings.get(["max_feedrate"]))
				)
				self.plugin.current_e_feedrate = new_feedrate
				self.logger.debug(f"Increased feedrate to {new_feedrate:.1f} mm/s")

			elif self.plugin.joy.left_button and not self._last_button_states['left']:
				# Decrease feedrate
				new_feedrate = max(
					self.plugin.current_e_feedrate - float(self.plugin._settings.get(["feedrate_increment"])) / 60,
					float(self.plugin._settings.get(["min_feedrate"]))
				)
				self.plugin.current_e_feedrate = new_feedrate
				self.logger.debug(f"Decreased feedrate to {new_feedrate:.1f} mm/s")

		except Exception as e:
			self.logger.error(f"Error handling feedrate adjustment: {str(e)}")

	def _process_buffered_input(self):
		"""Enhanced input processing with command aggregation and rate limiting"""
		if not self._input_buffer:
			return

		# Reduce aggregation window for faster response
		aggregation_window = 0.010  # Reduced from 0.05 to 0.010 seconds
		current_time = time.time()

		with self._buffer_lock:
			# Just take the most recent input state
			if len(self._input_buffer) > 0:
				latest_state = self._input_buffer[-1]
				self._input_buffer.clear()
			else:
				return

		# Apply minimal smoothing for faster response
		smoothed_x = 0.1 * self.last_x + 0.9 * latest_state.x_axis
		smoothed_y = 0.1 * self.last_y + 0.9 * latest_state.y_axis

		# Store for next iteration
		self.last_x = smoothed_x
		self.last_y = smoothed_y

		# Convert to target speeds with increased base speed
		x_speed = smoothed_x * (6000 / 60)  # Increased from 3000 to 6000 mm/min
		y_speed = smoothed_y * (6000 / 60)

		# Calculate movement magnitude and normalize if needed
		speed_magnitude = math.sqrt(x_speed*x_speed + y_speed*y_speed)
		if abs(x_speed) > 0.01 or abs(y_speed) > 0.01:  # More sensitive threshold
			# Calculate target position based on aggregated movement
			move_time = aggregation_window
			target_x = self.current_x + (x_speed * move_time)
			target_y = self.current_y + (y_speed * move_time)

			# Apply boundary limits
			target_x = max(0, min(self.plugin.maxX, target_x))
			target_y = max(0, min(self.plugin.maxY, target_y))

			# Calculate actual movement distance
			dx = target_x - self.current_x
			dy = target_y - self.current_y
			distance = math.sqrt(dx*dx + dy*dy)

			if distance >= self.min_movement:
				# Calculate appropriate feedrate with higher minimum speed
				min_feedrate = 2000  # Increased minimum feedrate
				max_feedrate = 6000  # Maximum feedrate

				# Calculate and scale feedrate based on joystick position
				stick_magnitude = math.sqrt(smoothed_x*smoothed_x + smoothed_y*smoothed_y)
				feedrate = min_feedrate + (max_feedrate - min_feedrate) * stick_magnitude

				# Create direct movement command
				command = MovementCommand(
					x=target_x,
					y=target_y,
					e=self.current_e,
					f=feedrate  # Direct feedrate without movement coordinator
				)

				# Update positions
				self.current_x = target_x
				self.current_y = target_y

				# Send immediately with minimal delay
				last_command_time = getattr(self, '_last_command_time', 0)
				time_since_last = current_time - last_command_time

				if time_since_last >= 0.010:  # Reduced from 0.02 to 0.010 seconds
					self._send_movement_command(command)
					self._last_command_time = current_time

	def _calculate_extrusion(self, distance: float, states: List[ControllerState]) -> float:
		"""Calculate extrusion amount based on movement distance and trigger states"""
		if distance < self.min_movement:
			return 0.0

		# Average the extrusion/retraction values
		avg_extrusion = sum(state.extrusion for state in states) / len(states)
		avg_retraction = sum(state.retraction for state in states) / len(states)

		# Calculate extrusion
		if avg_extrusion > 0.1:  # Extrusion threshold
			return distance * self.max_extrusion_per_mm * avg_extrusion
		elif avg_retraction > 0.1:  # Retraction threshold
			return -distance * self.max_extrusion_per_mm * avg_retraction
		return 0.0

	def _estimate_completion_time(self, command: MovementCommand) -> float:
		"""Estimate time to complete a movement command"""
		if not self.position_history:
			return 0.0

		# Calculate average speed from recent history
		total_distance = 0.0
		total_time = 0.0
		prev_pos = None
		prev_time = None

		for pos, timestamp in self.position_history:
			if prev_pos is not None:
				dist = math.sqrt(
					(pos['x'] - prev_pos['x'])**2 +
					(pos['y'] - prev_pos['y'])**2
				)
				total_distance += dist
				total_time += timestamp - prev_time
			prev_pos = pos
			prev_time = timestamp

		if total_time == 0:
			return 0.0

		avg_speed = total_distance / total_time
		if avg_speed == 0:
			return 0.0

		# Calculate distance for this command
		command_distance = math.sqrt(
			(command.x - self.current_x)**2 +
			(command.y - self.current_y)**2
		)

		return command_distance / avg_speed

	def _send_movement_command(self, command: MovementCommand):
		"""Enhanced movement command sending with completion time estimation"""
		try:
			# Estimate completion time
			completion_time = self._estimate_completion_time(command)

			# Generate coordinated movement GCode
			gcode = f"G1 X{command.x:.3f} Y{command.y:.3f}"
			if abs(command.e) > 0:
				gcode += f" E{command.e:.4f}"
			gcode += f" F{command.f}"

			# Log movement details in debug mode
			if self.plugin.joy and self.plugin.joy.debug_mode:
				self.logger.info(
					f"Movement: X={command.x:.3f} Y={command.y:.3f} "
					f"F={command.f:.1f} Est. Time={completion_time:.3f}s"
				)

			self.plugin.send(gcode)

		except Exception as e:
			self.logger.error(f"Error sending movement command: {str(e)}")
