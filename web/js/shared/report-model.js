function createReportModel(options = {}) {
  const facetMatchers = new Map(Object.entries(options.facetMatchers || {}));
  const listeners = new Set();
  let sourceRecords = [];
  let referenceRecords = [];
  let focusRecords = [];
  let grouping = options.grouping ?? null;
  let selections = new Map();
  let revision = 0;

  function selectionSnapshot() {
    return Object.freeze(Object.fromEntries(
      [...selections.entries()].map(([facet, values]) => [facet, Object.freeze([...values])])
    ));
  }

  function snapshot() {
    return Object.freeze({
      sourceRecords: Object.freeze([...sourceRecords]),
      referenceRecords: Object.freeze([...referenceRecords]),
      focusRecords: Object.freeze([...focusRecords]),
      grouping,
      selections: selectionSnapshot(),
      revision,
    });
  }

  function matchesSelections(record, excludedFacets = null) {
    for (const [facet, values] of selections) {
      if (excludedFacets && excludedFacets.has(facet)) continue;
      if (values.size === 0) continue;
      const matcher = facetMatchers.get(facet);
      if (!matcher) throw new Error(`Unknown report facet: ${facet}`);
      if (![...values].some((value) => matcher(record, value))) return false;
    }
    return true;
  }

  function recomputeFocus() {
    focusRecords = selections.size === 0
      ? [...referenceRecords]
      : referenceRecords.filter((record) => matchesSelections(record));
  }

  function publish() {
    revision += 1;
    const nextSnapshot = snapshot();
    for (const listener of listeners) listener(nextSnapshot);
    return nextSnapshot;
  }

  function updateSelections(actions) {
    const nextSelections = new Map(
      [...selections.entries()].map(([facet, values]) => [facet, new Set(values)])
    );
    for (const action of actions) {
      const facet = action.facet;
      if (!facetMatchers.has(facet)) throw new Error(`Unknown report facet: ${facet}`);
      const values = Array.isArray(action.values) ? action.values : [action.value];
      const current = nextSelections.get(facet) || new Set();
      if (action.operation === "replace") {
        nextSelections.set(facet, new Set(values.filter((value) => value !== undefined)));
      } else if (action.operation === "add") {
        for (const value of values) if (value !== undefined) current.add(value);
        nextSelections.set(facet, current);
      } else if (action.operation === "remove") {
        for (const value of values) current.delete(value);
        nextSelections.set(facet, current);
      } else if (action.operation === "toggle") {
        for (const value of values) {
          if (current.has(value)) current.delete(value);
          else if (value !== undefined) current.add(value);
        }
        nextSelections.set(facet, current);
      } else if (action.operation === "clear") {
        nextSelections.delete(facet);
      } else {
        throw new Error(`Unknown report selection operation: ${action.operation}`);
      }
    }
    selections = new Map([...nextSelections.entries()].filter(([, values]) => values.size > 0));
  }

  return Object.freeze({
    getSnapshot: snapshot,

    getPopulationExcluding(facets = []) {
      const excludedFacets = new Set(Array.isArray(facets) ? facets : [facets]);
      return Object.freeze(referenceRecords.filter((record) =>
        matchesSelections(record, excludedFacets)
      ));
    },

    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },

    setSourceRecords(records, referencePredicate = null) {
      sourceRecords = Array.isArray(records) ? [...records] : [];
      referenceRecords = referencePredicate
        ? sourceRecords.filter(referencePredicate)
        : [...sourceRecords];
      recomputeFocus();
      return publish();
    },

    setReferencePopulation(records) {
      referenceRecords = Array.isArray(records) ? [...records] : [];
      recomputeFocus();
      return publish();
    },

    setGrouping(value) {
      if (Object.is(grouping, value)) return snapshot();
      grouping = value;
      return publish();
    },

    applySelection(actions) {
      const transaction = Array.isArray(actions) ? actions : [actions];
      if (transaction.length === 0) return snapshot();
      updateSelections(transaction);
      recomputeFocus();
      return publish();
    },

    clearSelections() {
      if (selections.size === 0) return snapshot();
      selections = new Map();
      recomputeFocus();
      return publish();
    },
  });
}
