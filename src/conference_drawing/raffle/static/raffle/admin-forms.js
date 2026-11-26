// Admin add/change forms: don't lose typed work, and make errors easy to fix.
//
// 1. Leaving a form with unsaved changes asks first (the browser's own
//    "Leave page?" prompt). A form that came back with errors counts as
//    unsaved from the start: nothing in it has been stored yet.
// 2. After a failed save, the first field with an error is scrolled into
//    view and focused. (Django already expands a collapsed section that
//    holds an error, so the field is always visible.)
//
// Loaded with `defer` on every admin page by templates/admin/base_site.html;
// it does nothing on pages without a change form. Tested in a real browser
// by tests/e2e/test_admin_forms.py, which also checks its coverage.
"use strict";
(function () {
  const form = document.querySelector("#content-main form[id$='_form']");
  if (!form) {
    return;
  }
  const firstError = form.querySelector(".errors > .errorlist, td > .errorlist");
  let dirty = firstError !== null;
  form.addEventListener("input", () => {
    dirty = true;
  });
  form.addEventListener("change", () => {
    dirty = true;
  });
  form.addEventListener("submit", () => {
    dirty = false;
  });
  window.addEventListener("beforeunload", (event) => {
    if (dirty) {
      event.preventDefault();
    }
  });
  if (!firstError) {
    return;
  }
  const control = firstError.parentElement.querySelector(
    "input:not([type=hidden]), select, textarea",
  );
  control.scrollIntoView({ block: "center" });
  control.focus({ preventScroll: true });
})();
