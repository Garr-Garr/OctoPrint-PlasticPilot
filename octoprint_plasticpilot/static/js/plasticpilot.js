$(function() {
	function showConfirmationDialog(options) {
		return new PNotify({
			title: options.title,
			text: options.message,
			icon: "glyphicon glyphicon-question-sign",
			hide: false,
			confirm: {
				confirm: true,
				buttons: [{
					text: 'Proceed',
					addClass: 'btn-warning',
					click: function(notice) {
						notice.remove();
						options.onproceed();
					}
				}, {
					text: 'Cancel',
					click: function(notice) {
						notice.remove();
					}
				}]
			},
			buttons: {
				closer: false,
				sticker: false
			},
			history: {
				history: false
			}
		});
	}

	function PlasticPilotViewModel(parameters) {
		var self = this;

		// Get settings view model and settings
		self.settingsViewModel = parameters[0];
		self.settings = null;

		// Available controllers list
		self.availableControllers = ko.observableArray([]);
		self.selectedController = ko.observable();
		self.isControllerActive = ko.observable(false);
		self.controllerStatusText = ko.computed(function() {
			if (self.isControllerActive()) {
				return "Controller active";
			} else if (self.selectedController()) {
				return "Controller inactive";
			}
			return "No controller selected";
		});

		// Initialize all settings as observables - remove unused ones
		self.baseSpeed = ko.observable();
		self.zDrawing = ko.observable();
		self.zTravel = ko.observable();
		self.debugMode = ko.observable();
		self.movementCheckInterval = ko.observable();
		self.commandDelay = ko.observable();
		self.extrusionSpeed = ko.observable();
		self.retractionSpeed = ko.observable();
		self.extrusionAmount = ko.observable();
		self.retractionAmount = ko.observable();
		self.feedrateIncrement = ko.observable();
		self.minFeedrate = ko.observable();
		self.maxFeedrate = ko.observable();
		self.minMovement = ko.observable();

		// Update initialization - remove unused bindings
		self.onBeforeBinding = function() {
			self.settings = self.settingsViewModel.settings.plugins.plasticpilot;
			self.baseSpeed(self.settings.base_speed());
			self.zDrawing(self.settings.z_drawing());
			self.zTravel(self.settings.z_travel());
			self.debugMode(self.settings.debug_mode());
			self.movementCheckInterval(self.settings.movement_check_interval());
			self.commandDelay(self.settings.command_delay());
			self.extrusionSpeed(self.settings.extrusion_speed());
			self.retractionSpeed(self.settings.retraction_speed());
			self.extrusionAmount(self.settings.extrusion_amount());
			self.retractionAmount(self.settings.retraction_amount());
			self.feedrateIncrement(self.settings.feedrate_increment());
			self.minFeedrate(self.settings.min_feedrate());
			self.maxFeedrate(self.settings.max_feedrate());
			self.minMovement(self.settings.min_movement());
		};

		// Update settings save - remove unused settings
		self.onSettingsBeforeSave = function() {
			self.settings.base_speed(self.baseSpeed());
			self.settings.z_drawing(self.zDrawing());
			self.settings.z_travel(self.zTravel());
			self.settings.debug_mode(self.debugMode());
			self.settings.movement_check_interval(self.movementCheckInterval());
			self.settings.command_delay(self.commandDelay());
			self.settings.extrusion_speed(self.extrusionSpeed());
			self.settings.retraction_speed(self.retractionSpeed());
			self.settings.extrusion_amount(self.extrusionAmount());
			self.settings.retraction_amount(self.retractionAmount());
			self.settings.feedrate_increment(self.feedrateIncrement());
			self.settings.min_feedrate(self.minFeedrate());
			self.settings.max_feedrate(self.maxFeedrate());
			self.settings.min_movement(self.minMovement());
		};

		// Periodic refresh functions
		self.startPeriodicRefresh = function() {
			// Check for new controllers every 30 seconds
			self.refreshInterval = setInterval(function() {
				if (!self.isControllerActive()) {
					self.refreshControllers();
				}
			}, 30000);  // 30 seconds
		};

		self.stopPeriodicRefresh = function() {
			if (self.refreshInterval) {
				clearInterval(self.refreshInterval);
				self.refreshInterval = null;
			}
		};

		// Controller management functions
		self.refreshControllers = function() {
			// Show refresh in progress
			var refreshButton = $("button[data-bind='click: refreshControllers']");
			if (refreshButton.length) {
				refreshButton.prop('disabled', true);
			}

			OctoPrint.simpleApiCommand("plasticpilot", "refresh")
				.done(function(response) {
					if (response.success) {
						// Update the available controllers
						self.availableControllers(response.controllers);

						// If we have controllers but none selected, select the first one
						if (response.controllers.length > 0 && !self.selectedController()) {
							self.selectedController(response.controllers[0].id);
						}

						// If the current selection is no longer available, clear it
						if (self.selectedController() && !response.controllers.some(function(c) {
							return c.id === self.selectedController();
						})) {
							self.selectedController(undefined);
							self.isControllerActive(false);
						}

						// Show success message
						new PNotify({
							title: "Controllers Refreshed",
							text: response.controllers.length + " controller(s) found",
							type: "success"
						});
					} else {
						// Show error message
						new PNotify({
							title: "Refresh Failed",
							text: response.error || "Failed to refresh controllers",
							type: "error"
						});
					}
				})
				.fail(function() {
					new PNotify({
						title: "Refresh Failed",
						text: "Failed to communicate with the server",
						type: "error"
					});
				})
				.always(function() {
					// Re-enable the refresh button
					if (refreshButton.length) {
						refreshButton.prop('disabled', false);
					}
				});
		};

		self.activateController = function() {
			if (!self.selectedController()) return;

			self.stopPeriodicRefresh();  // Stop refresh when controller is active

			OctoPrint.simpleApiCommand("plasticpilot", "activate", {
				controller_id: self.selectedController()
			}).done(function(response) {
				if (response.success) {
					self.isControllerActive(true);
					new PNotify({
						title: "Controller Activated",
						text: "Controller is now active",
						type: "success"
					});
				} else {
					// If activation fails, restart periodic refresh
					self.startPeriodicRefresh();
				}
			}).fail(function() {
				// If request fails, restart periodic refresh
				self.startPeriodicRefresh();
			});
		};

		self.deactivateController = function() {
			OctoPrint.simpleApiCommand("plasticpilot", "deactivate")
				.done(function(response) {
					if (response.success) {
						self.isControllerActive(false);
						new PNotify({
							title: "Controller Deactivated",
							text: "Controller is now inactive",
							type: "info"
						});
						self.startPeriodicRefresh();  // Resume refresh when controller is deactivated
					}
				});
		};

		// Event handler for plugin messages
		self.onDataUpdaterPluginMessage = function(plugin, data) {
			if (plugin !== "plasticpilot") return;

			if (data.type === "controller_status") {
				self.isControllerActive(data.active);
				if (data.controller_id) {
					self.selectedController(data.controller_id);
				}

				// Manage periodic refresh based on controller status
				if (data.active) {
					self.stopPeriodicRefresh();
				} else {
					self.startPeriodicRefresh();
				}
			}
		};

		// Reset settings to default values
		self.resetSettings = function() {
			showConfirmationDialog({
				title: "Reset Settings",
				message: "Are you sure you want to reset all settings to their default values? This cannot be undone.",
				onproceed: function() {
					OctoPrint.simpleApiCommand("plasticpilot", "reset_settings")
						.done(function(response) {
							if (response.success) {
								// Update view model with default values
								self.baseSpeed(response.defaults.base_speed);
								self.zDrawing(response.defaults.z_drawing);
								self.zTravel(response.defaults.z_travel);
								self.debugMode(response.defaults.debug_mode);
								self.movementCheckInterval(response.defaults.movement_check_interval);
								self.commandDelay(response.defaults.command_delay);
								self.smoothingFactor(response.defaults.smoothing_factor);
								self.minMovement(response.defaults.min_movement);
								self.deadzoneThreshold(response.defaults.deadzone_threshold);
								self.walkThreshold(response.defaults.walk_threshold);
								self.runThreshold(response.defaults.run_threshold);
								self.walkSpeedMultiplier(response.defaults.walk_speed_multiplier);
								self.runSpeedMultiplier(response.defaults.run_speed_multiplier);
								self.maxSpeedMultiplier(response.defaults.max_speed_multiplier);

								new PNotify({
									title: "Settings Reset",
									text: "All settings have been reset to their default values",
									type: "success"
								});
							} else {
								new PNotify({
									title: "Reset Failed",
									text: response.error || "Failed to reset settings",
									type: "error"
								});
							}
						})
						.fail(function() {
							new PNotify({
								title: "Reset Failed",
								text: "Failed to reset settings",
								type: "error"
							});
						});
				}
			});
		};

		// Initial setup with periodic refresh
		self.onStartup = function() {
			self.refreshControllers();
			self.startPeriodicRefresh();
		};

		// Clean up when the view model is disposed
		self.onBeforeDispose = function() {
			self.stopPeriodicRefresh();
		};
	}

	OCTOPRINT_VIEWMODELS.push({
		construct: PlasticPilotViewModel,
		dependencies: ["settingsViewModel"],
		elements: ["#settings_plugin_plasticpilot"]
	});
});
