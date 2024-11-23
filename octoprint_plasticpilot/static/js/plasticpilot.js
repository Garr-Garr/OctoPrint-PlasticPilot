$(function() {
	function PlasticPilotViewModel(parameters) {
		var self = this;

		// Get settings view model and settings
		self.settingsViewModel = parameters[0];

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

		// Initialize settings
		self.onBeforeBinding = function() {
			self.settings = self.settingsViewModel.settings.plugins.plasticpilot;
		};

		// Add periodic refresh functions
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
