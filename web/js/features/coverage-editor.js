function createCoverageEditorFeature() {
  return {
    coverageEditorVms: [],
    coverageDraft: { decision: null, priority: null, note: "" },
    coverageSaving: false,

    openCoverageEditor(vm) {
      this.openCoverageEditorForVms([vm]);
    },

    openSelectedCoverageEditor() {
      const selected = this.vms.filter((vm) => this.isVmSelected(vm));
      if (selected.length) this.openCoverageEditorForVms(selected);
    },

    openCoverageEditorForVms(vms) {
      const targets = (vms || []).filter((vm) => vm && vm.id);
      if (!targets.length) return;
      const decisionValues = new Set(targets.map((vm) => vm.odcrCoverage && vm.odcrCoverage.decision || null));
      const decision = decisionValues.size === 1 ? [...decisionValues][0] : null;
      const priorityValues = new Set(targets.map((vm) => vm.odcrCoverage && vm.odcrCoverage.priority || null));
      const priority = decision === "required" && priorityValues.size === 1
        ? [...priorityValues][0]
        : null;
      const singleCoverage = targets.length === 1 ? targets[0].odcrCoverage || {} : {};
      this.coverageEditorVms = targets;
      this.coverageDraft = {
        decision,
        priority: priority || (targets.length === 1 && decision === "required" ? "medium" : null),
        note: targets.length === 1 ? singleCoverage.note || "" : "",
      };
    },

    get coverageEditorVm() {
      return this.coverageEditorVms[0] || null;
    },

    get coverageEditorIsBulk() {
      return this.coverageEditorVms.length > 1;
    },

    get coverageEditorHasMarkedDecision() {
      return this.coverageEditorVms.some((vm) => vm.odcrCoverage && vm.odcrCoverage.decision);
    },

    closeCoverageEditor() {
      if (this.coverageSaving) return;
      this.coverageEditorVms = [];
    },

    selectCoverageDecision(decision) {
      this.coverageDraft.decision = decision;
      if (decision === "required" && !this.coverageDraft.priority) {
        this.coverageDraft.priority = "medium";
      } else if (decision !== "required") {
        this.coverageDraft.priority = null;
      }
    },

    async saveCoverageDecision(clear = false) {
      if (!this.coverageEditorVms.length || this.coverageSaving) return;
      const decision = clear ? null : this.coverageDraft.decision;
      const priority = clear ? null : this.coverageDraft.priority;
      const note = clear ? null : this.coverageDraft.note.trim() || null;
      if (!clear && !decision) {
        this.notify("Choose whether ODCR coverage is required.");
        return;
      }
      if (decision === "required" && !priority) {
        this.notify("Choose a priority for required ODCR coverage.");
        return;
      }
      if (note && note.length > 500) {
        this.notify("The ODCR coverage note cannot exceed 500 characters.");
        return;
      }
      this.coverageSaving = true;
      try {
        const resourceIds = this.coverageEditorVms.map((vm) => vm.id);
        for (let start = 0; start < resourceIds.length; start += 500) {
          const data = await requestJson("/api/odcr/coverage/decisions", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ resourceIds: resourceIds.slice(start, start + 500), decision, priority, note }),
          });
          const updatedById = new Map((data.value || []).map((item) => [item.resourceId.toLowerCase(), item.odcrCoverage]));
          this.vms = this.vms.map((vm) => updatedById.has(vm.id.toLowerCase())
            ? { ...vm, odcrCoverage: updatedById.get(vm.id.toLowerCase()) }
            : vm);
        }
        this.rebuildVmView();
        const updatedCount = resourceIds.length;
        this.coverageEditorVms = [];
        this.notify(
          clear
            ? `ODCR coverage decision cleared for ${updatedCount} VM${updatedCount === 1 ? "" : "s"}.`
            : `ODCR coverage decision saved for ${updatedCount} VM${updatedCount === 1 ? "" : "s"}.`,
          "ok"
        );
      } catch (error) {
        this.notify(`Failed to update ODCR coverage: ${error.message || error}`);
      } finally {
        this.coverageSaving = false;
      }
    },
  };
}
