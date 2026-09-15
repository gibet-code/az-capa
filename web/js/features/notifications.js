function createNotificationsFeature() {
  return {
    notifications: [],
    notificationDeleting: {},
    notificationsDeletingAll: false,
    notificationDrawerOpen: false,

    async loadNotifications() {
      if (!this.user.capabilities.administer) return;
      try {
        this.notifications = await requestJson("/api/notifications", {}, "notifications unavailable");
      } catch (_) {
        this.notifications = [];
      }
    },

    async discardNotification(notification) {
      if (!this.user.capabilities.administer) return;
      this.notificationDeleting[notification.instanceId] = true;
      try {
        await requestResponse(`/api/notifications/${encodeURIComponent(notification.instanceId)}`, {
          method: "DELETE",
        });
        this.notifications = this.notifications.filter((item) => item.instanceId !== notification.instanceId);
      } catch (error) {
        this.notify(`Could not discard ${notification.label}: ${error && error.message ? error.message : error}`);
      } finally {
        this.notificationDeleting[notification.instanceId] = false;
      }
    },

    async discardAllNotifications() {
      if (!this.user.capabilities.administer) return;
      this.notificationsDeletingAll = true;
      try {
        await requestResponse("/api/notifications", { method: "DELETE" });
        this.notifications = this.notifications.filter((notification) => notification.isRunning);
      } catch (error) {
        this.notify(`Could not discard terminated notifications: ${error && error.message ? error.message : error}`);
      } finally {
        this.notificationsDeletingAll = false;
        this.loadNotifications();
      }
    },

    get runningCount() {
      return this.notifications.filter((notification) => notification.isRunning).length;
    },

    isBusy(kind) {
      return this.notifications.some((notification) => notification.kind === kind && notification.isRunning);
    },
  };
}
