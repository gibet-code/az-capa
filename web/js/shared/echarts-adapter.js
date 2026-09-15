function createEchartsAdapter({ onClick = null } = {}) {
  let chart = null;
  let container = null;
  let resizeObserver = null;
  let windowResizeHandler = null;

  function dispose() {
    if (resizeObserver) resizeObserver.disconnect();
    if (windowResizeHandler) window.removeEventListener("resize", windowResizeHandler);
    if (chart) chart.dispose();
    chart = null;
    container = null;
    resizeObserver = null;
    windowResizeHandler = null;
  }

  function initialize(nextContainer) {
    if (chart && container === nextContainer) return chart;
    if (chart) dispose();
    if (!nextContainer || typeof echarts === "undefined") return null;
    container = nextContainer;
    chart = echarts.init(container, null, { renderer: "canvas" });
    if (onClick) chart.on("click", onClick);
    if (typeof ResizeObserver !== "undefined") {
      resizeObserver = new ResizeObserver(() => chart && chart.resize());
      resizeObserver.observe(container);
    } else {
      windowResizeHandler = () => chart && chart.resize();
      window.addEventListener("resize", windowResizeHandler);
    }
    return chart;
  }

  return Object.freeze({
    get instance() {
      return chart;
    },

    render(nextContainer, option) {
      const instance = initialize(nextContainer);
      if (!instance) return false;
      instance.setOption(option, { notMerge: true });
      instance.resize();
      return true;
    },

    clear() {
      if (chart) chart.clear();
    },

    resize() {
      if (chart) chart.resize();
    },

    dispose,
  });
}
