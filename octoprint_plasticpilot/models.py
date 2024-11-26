from dataclasses import dataclass
from typing import Optional, Tuple, Deque
from collections import deque
import math
import time

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


@dataclass
class MovementVector:
	"""Represents a movement vector with direction and magnitude"""
	x: float = 0.0
	y: float = 0.0
	magnitude: float = 0.0
	direction: float = 0.0  # In radians
	timestamp: float = 0.0


@dataclass
class AccelerationProfile:
	"""Represents acceleration parameters for movement"""
	acceleration: float  # mm/s²
	max_speed: float    # mm/s
	current_speed: float = 0.0
	target_speed: float = 0.0

	def calculate_movement(self, time_delta: float) -> Tuple[float, float]:
		"""Calculate distance and new speed based on acceleration profile"""
		if abs(self.current_speed - self.target_speed) < 0.001:
			return self.current_speed * time_delta, self.current_speed

		accel_dir = 1 if self.target_speed > self.current_speed else -1
		accel = accel_dir * self.acceleration

		time_to_target = abs(self.target_speed - self.current_speed) / self.acceleration

		if time_to_target <= time_delta:
			# We reach target speed during this period
			dist1 = self.current_speed * time_to_target + 0.5 * accel * time_to_target * time_to_target
			dist2 = self.target_speed * (time_delta - time_to_target)
			return dist1 + dist2, self.target_speed
		else:
			# Still accelerating
			new_speed = self.current_speed + accel * time_delta
			distance = self.current_speed * time_delta + 0.5 * accel * time_delta * time_delta
			return distance, new_speed

class MovementCoordinator:
	"""Coordinates smooth movement with acceleration profiles"""
	def __init__(self, acceleration: float = 1200, max_speed: float = 50):
		self.x_profile = AccelerationProfile(acceleration, max_speed)
		self.y_profile = AccelerationProfile(acceleration, max_speed)
		self.movement_buffer: Deque[MovementVector] = deque(maxlen=100)
		self.last_update_time = time.time()
		self.min_movement = 0.01  # mm

	def add_movement(self, x: float, y: float, speed: float):
		"""Add a movement vector to the buffer"""
		magnitude = math.sqrt(x*x + y*y)
		if magnitude < self.min_movement:
			return

		direction = math.atan2(y, x)
		self.movement_buffer.append(MovementVector(
			x=x, y=y,
			magnitude=magnitude,
			direction=direction,
			timestamp=time.time()
		))

		# Update target speeds
		self.x_profile.target_speed = speed * abs(math.cos(direction))
		self.y_profile.target_speed = speed * abs(math.sin(direction))

	def process_movement(self, time_delta: float) -> Optional[MovementCommand]:
		"""Process buffered movements and generate coordinated command"""
		if not self.movement_buffer:
			return None

		# Calculate smooth movement with acceleration
		x_dist, new_x_speed = self.x_profile.calculate_movement(time_delta)
		y_dist, new_y_speed = self.y_profile.calculate_movement(time_delta)

		self.x_profile.current_speed = new_x_speed
		self.y_profile.current_speed = new_y_speed
		self.last_update_time = time.time()  # Update the last update time

		# Generate movement command
		if abs(x_dist) > self.min_movement or abs(y_dist) > self.min_movement:
			return MovementCommand(
				x=x_dist,
				y=y_dist,
				f=math.sqrt(new_x_speed*new_x_speed + new_y_speed*new_y_speed) * 60
			)
		return None
