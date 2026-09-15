const runUuid = String(window.GW_RUN_UUID || "");
const back = document.getElementById("ucBack");
const rerun = document.getElementById("ucRerun");

async function boot() {
  if (!runUuid) return;
  const res = await fetch("/api/gateway/evaluations/runs/" + encodeURIComponent(runUuid));
  if (!res.ok) return;
  const report = await res.json();
  const ucId = (report.usecase && report.usecase.id) || window.GW_USECASE_ID || "";
  if (ucId && back) back.setAttribute("href", "/gateway/usecases/" + ucId);
  if (rerun) {
    rerun.hidden = !ucId;
    rerun.addEventListener("click", async () => {
      const run = await fetch("/api/gateway/usecases/" + encodeURIComponent(ucId) + "/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const body = await run.json().catch(() => ({}));
      if (body.report_url) window.location = body.report_url;
    });
  }
}

boot();
