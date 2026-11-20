"use strict";

const state = {
  lang: localStorage.getItem("butler-lang") || "ru",
  dict: { ru: {}, en: {} },
  rule: "",
  projects: [],
  projectId: Number(sessionStorage.getItem("butler-project") || 0),
  project: null,
  tab: "updates",
  updates: [],
  updatesJson: "",
  updatesError: "",
  history: [],
  preview: null,
  run: null,
  modal: null,
  osNotify: localStorage.getItem("butler-notify") === "1",
  busy: false,
  bootError: "",
};

const STEPS = ["validate", "connect", "pack", "upload", "backup", "apply", "restart", "checks", "rollback", "cleanup"];

function t(key, params) {
  const table = state.dict[state.lang] || state.dict.ru || {};
  let text = table[key] || (state.dict.ru && state.dict.ru[key]) || key;
  if (params) {
    for (const [name, value] of Object.entries(params)) {
      text = text.split("{" + name + "}").join(String(value));
    }
  }
  return text;
}

function h(tag, props, kids) {
  const node = document.createElement(tag);
  const attrs = props || {};
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = String(value);
    else if (key === "disabled") node.disabled = Boolean(value);
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else node.setAttribute(key, String(value));
  }
  const list = kids == null ? [] : [].concat(kids);
  for (const kid of list) {
    if (kid == null || kid === false) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

function csrf() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute("content") || "" : "";
}

async function api(method, url, body) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") {
    headers.Origin = location.origin;
    headers["X-CSRF-Token"] = csrf();
    headers["X-Butler-Lang"] = state.lang;
  }
  const response = await fetch(url, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch (_err) { data = { error: "bad_json" }; }
  }
  if (!response.ok) {
    const error = new Error((data && data.error) || response.statusText);
    error.data = data;
    error.status = response.status;
    throw error;
  }
  return data;
}

function toast(kind, text) {
  const host = document.getElementById("toasts");
  const node = h("div", { class: "toast" + (kind === "err" ? " err" : ""), text });
  host.append(node);
  setTimeout(() => node.remove(), 4200);
}

function formatBytes(size) {
  const units = ["unit.bytes", "unit.kb", "unit.mb", "unit.gb"];
  let value = Number(size) || 0;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  const shown = index === 0 ? String(Math.round(value)) : value.toFixed(1);
  return shown + " " + t(units[index]);
}

function formatTime(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return new Intl.DateTimeFormat(state.lang === "ru" ? "ru-RU" : "en-GB", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function linesOf(value) {
  return String(value || "").split(/\n/).map((item) => item.trim()).filter(Boolean);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("ok", t("common.copied"));
  } catch (_err) {
    toast("err", t("toast.copy_fail"));
  }
}

function downloadText(filename, text) {
  const link = h("a", { href: URL.createObjectURL(new Blob([text], { type: "text/plain" })), download: filename });
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

function render() {
  document.documentElement.lang = state.lang;
  const app = document.getElementById("app");
  app.replaceChildren(renderTop(), renderLayout());
  if (state.modal) app.append(renderModal());
}

function renderTop() {
  return h("header", { class: "top" }, [
    h("div", { class: "brand" }, [
      h("div", { class: "mark", "aria-hidden": "true" }),
      h("div", {}, [
        h("div", { class: "word", text: t("app.name") }),
        h("div", { class: "tag", text: t("app.tagline") }),
      ]),
    ]),
    h("div", { class: "langs" }, [
      h("button", { class: state.lang === "ru" ? "on" : "", type: "button", text: t("lang.ru"), onClick: () => setLang("ru") }),
      h("button", { class: state.lang === "en" ? "on" : "", type: "button", text: t("lang.en"), onClick: () => setLang("en") }),
    ]),
  ]);
}

function setLang(lang) {
  state.lang = lang;
  localStorage.setItem("butler-lang", lang);
  document.documentElement.lang = lang;
  render();
}

function renderLayout() {
  return h("div", { class: "layout" }, [renderRail(), h("main", { class: "main" }, [renderMain()])]);
}

function renderRail() {
  const buttons = state.projects.map((project) =>
    h("button", {
      class: "project-btn" + (project.id === state.projectId ? " on" : ""),
      type: "button",
      onClick: () => selectProject(project.id),
    }, [
      h("strong", { text: project.name }),
      h("div", {}, h("small", { text: project.ssh_host || "—" })),
    ])
  );
  return h("aside", { class: "rail" }, [
    h("h2", { text: t("app.name") }),
    buttons.length ? h("div", {}, buttons) : h("p", { class: "muted", text: t("project.empty_title") }),
    h("button", { class: "btn primary", type: "button", text: t("project.new"), onClick: () => openModal({ type: "new" }) }),
  ]);
}

function renderMain() {
  if (state.bootError) return h("div", { class: "empty" }, [h("h2", { text: t("common.error") }), h("p", { text: state.bootError })]);
  if (!state.project) {
    return h("section", { class: "empty" }, [
      h("h2", { text: t("project.empty_title") }),
      h("p", { class: "muted", text: t("project.empty_body") }),
    ]);
  }
  const tabs = ["updates", "settings", "server", "history"].map((tab) =>
    h("button", {
      type: "button",
      class: state.tab === tab ? "on" : "",
      text: t("nav." + tab),
      onClick: () => { state.tab = tab; state.preview = null; render(); },
    })
  );
  let body;
  if (state.tab === "updates") body = renderUpdates();
  else if (state.tab === "settings") body = renderSettings();
  else if (state.tab === "server") body = renderServer();
  else body = renderHistory();
  return h("section", {}, [h("div", { class: "tabs" }, tabs), body]);
}

function renderUpdates() {
  if (state.run) return renderRun();
  if (state.preview) return renderConfirm();
  const head = h("div", { class: "row" }, [
    h("button", { class: "btn", type: "button", text: t("common.refresh"), onClick: () => refreshUpdates(false) }),
    h("span", { class: "muted", text: t("updates.auto") }),
  ]);
  if (state.updatesError === "missing") {
    return h("div", { class: "stack" }, [head, h("div", { class: "blocker", text: t("updates.dir_missing") })]);
  }
  if (!state.updates.length) {
    return h("div", { class: "stack" }, [
      head,
      h("div", { class: "empty" }, [
        h("h2", { text: t("updates.empty_title") }),
        h("p", { class: "muted", text: t("updates.empty_body") }),
      ]),
    ]);
  }
  const cards = state.updates.map((item) => {
    const badge = item.blocked ? "blocked" : item.deployed ? "deployed" : "new";
    const label = item.blocked ? "updates.blocked" : item.deployed ? "updates.deployed" : "updates.new";
    return h("button", { class: "update", type: "button", onClick: () => openPreview(item.name) }, [
      h("strong", { text: item.name }),
      h("span", { class: "badge " + badge, text: t(label) }),
      h("span", { class: "muted", text: formatTime(item.mtime) }),
      h("span", { class: "muted", text: t("updates.files", { count: item.file_count, size: formatBytes(item.size) }) }),
    ]);
  });
  return h("div", { class: "stack" }, [head, h("div", {}, cards)]);
}

function renderConfirm() {
  const preview = state.preview;
  const blockers = preview.blockers || [];
  const disabled = preview.blocked || !preview.hashes_ok || !preview.ready || preview.unreachable || preview.needs_hostkey;
  const rows = (preview.files || []).map((file) =>
    h("tr", {}, [
      h("td", { class: "path", text: file.path }),
      h("td", { class: "mono", title: file.sha256, text: file.sha256_short }),
      h("td", { class: "state-" + file.state, text: t("confirm.state_" + (file.state === "unchanged" ? "same" : file.state)) }),
    ])
  );
  const backupBox = h("input", { id: "backup-db", type: "checkbox" });
  backupBox.checked = Boolean(preview.backup_db_default);
  const notifyBox = h("input", { id: "os-notify", type: "checkbox" });
  notifyBox.checked = state.osNotify;
  return h("div", { class: "stack" }, [
    h("div", { class: "row" }, [
      h("button", { class: "btn", type: "button", text: t("common.back"), onClick: () => { state.preview = null; render(); } }),
      h("h2", { text: t("confirm.title") }),
    ]),
    h("div", { class: "panel stack" }, [
      h("div", { class: "muted", text: t("confirm.sha") }),
      h("div", { class: "mono path", text: preview.update_sha256 || "—" }),
      preview.unreachable ? h("div", { class: "blocker", text: preview.hint || t("confirm.unreachable") }) : null,
      !preview.ready ? h("div", { class: "blocker", text: t("confirm.not_ready") }) : null,
      blockers.length ? h("div", { class: "blocker" }, [
        h("div", { text: t("confirm.blocked") }),
        h("ul", {}, blockers.map((item) => h("li", { text: blockerText(item) }))),
      ]) : null,
      h("div", { class: "table-wrap" }, [
        h("table", {}, [
          h("thead", {}, h("tr", {}, [
            h("th", { text: t("confirm.path") }),
            h("th", { text: t("confirm.hash") }),
            h("th", { text: t("confirm.state") }),
          ])),
          h("tbody", {}, rows),
        ]),
      ]),
      h("label", { class: "row check" }, [
        backupBox,
        h("span", { text: t("confirm.backup_db") }),
      ]),
      h("label", { class: "row check" }, [
        notifyBox,
        h("span", { text: t("confirm.os_notify") }),
      ]),
      h("p", { class: "help", text: t("confirm.go_help") }),
      h("button", {
        class: "btn primary",
        type: "button",
        text: t("confirm.go"),
        disabled,
        onClick: confirmDeploy,
      }),
    ]),
  ]);
}

function blockerText(item) {
  if (item.code === "bad_release") return t("file.bad_release", { name: item.name || "", rule: state.rule });
  return t("file." + item.code, { path: item.path || "", pattern: item.pattern || "" });
}

function renderRun() {
  const run = state.run;
  const seen = new Set(run.events.map((event) => event.step));
  const last = run.events.length ? run.events[run.events.length - 1].step : "validate";
  const failed = run.status === "error" || run.status === "error_before_changes" || run.status === "rolled_back";
  const steps = STEPS.filter((step) => step !== "rollback" || seen.has("rollback")).map((step) => {
    let cls = "step";
    if (run.status === "success" && seen.has(step)) cls += " ok";
    else if (step === last && !run.status) cls += " now";
    else if (seen.has(step) && failed && step === last) cls += " bad";
    else if (seen.has(step)) cls += " ok";
    return h("span", { class: cls, text: t("step." + step) });
  });
  const lines = run.events.filter((event) => event.key).map((event) =>
    h("div", { class: event.level || "", text: t(event.key, event.params) })
  );
  let banner = null;
  if (run.status) {
    const word = run.status === "success" ? "deploy.success" : run.status === "rolled_back" ? "deploy.rolled_back" : "deploy.error";
    banner = h("div", {}, [
      h("div", { class: "banner " + run.status, text: t(word) }),
      run.status === "error_before_changes" ? h("div", { class: "subtitle", text: t("deploy.subtitle_before") }) : null,
      h("div", { class: "muted", text: t("deploy.duration", { ms: run.duration || 0 }) }),
    ]);
  }
  return h("div", { class: "stack" }, [
    h("h2", { text: t("deploy.title") }),
    banner,
    h("div", { class: "steps" }, steps),
    h("div", { class: "log", id: "live-log" }, lines),
  ]);
}

function field(id, label, control) {
  return h("label", { class: "field", for: id }, [h("span", { text: label }), control]);
}

function renderSettings() {
  const project = state.project;
  const input = (id, value, type) => h("input", { id, type: type || "text", value: value == null ? "" : String(value) });
  const area = (id, value) => h("textarea", { id, text: value || "" });
  return h("form", { class: "stack", id: "settings-form", onSubmit: saveSettings }, [
    h("h2", { text: t("settings.title") }),
    h("div", { class: "grid-2" }, [
      field("f-name", t("settings.name"), input("f-name", project.name)),
      field("f-updates", t("settings.updates_dir"), input("f-updates", project.updates_dir)),
      field("f-format", t("settings.update_format"), h("select", { id: "f-format" }, [
        h("option", { value: "folder", selected: project.update_format === "folder" ? "selected" : null, text: t("settings.format_folder") }),
        h("option", { value: "archive", selected: project.update_format === "archive" ? "selected" : null, text: t("settings.format_archive") }),
      ])),
      field("f-mode", t("settings.deploy_mode"), h("select", { id: "f-mode" }, [
        h("option", { value: "inplace", selected: project.deploy_mode === "inplace" ? "selected" : null, text: t("settings.mode_inplace") }),
        h("option", { value: "releases", selected: project.deploy_mode === "releases" ? "selected" : null, text: t("settings.mode_releases") }),
      ])),
      field("f-host", t("settings.ssh_host"), input("f-host", project.ssh_host)),
      field("f-port", t("settings.ssh_port"), input("f-port", project.ssh_port, "number")),
      field("f-user", t("settings.ssh_user"), input("f-user", project.ssh_user)),
      field("f-key", t("settings.ssh_key"), input("f-key", project.ssh_key_path)),
      field("f-app", t("settings.app_dir"), input("f-app", project.app_dir)),
      field("f-service", t("settings.service_type"), h("select", { id: "f-service" }, [
        h("option", { value: "systemd", selected: project.service_type === "systemd" ? "selected" : null, text: t("settings.service_systemd") }),
        h("option", { value: "pm2", selected: project.service_type === "pm2" ? "selected" : null, text: t("settings.service_pm2") }),
        h("option", { value: "none", selected: project.service_type === "none" ? "selected" : null, text: t("settings.service_none") }),
      ])),
      field("f-service-name", t("settings.service_name"), input("f-service-name", project.service_name)),
      field("f-owner", t("settings.app_owner"), input("f-owner", project.app_owner)),
      field("f-backups", t("settings.backups_dir"), input("f-backups", project.backups_dir)),
      field("f-health", t("settings.health_url"), input("f-health", project.healthcheck_url)),
      field("f-codes", t("settings.expected_codes"), input("f-codes", (project.expected_codes || []).join(", "))),
      field("f-expect", t("settings.expected_text"), input("f-expect", project.expected_text)),
      field("f-hint", t("settings.hint"), input("f-hint", project.unreachable_hint)),
    ]),
    field("f-db", t("settings.db_paths"), area("f-db", (project.db_backup_paths || []).join("\n"))),
    h("p", { class: "help", text: t("settings.db_hint") }),
    field("f-logs", t("settings.log_patterns"), area("f-logs", (project.log_error_patterns || []).join("\n"))),
    field("f-forbid", t("settings.forbidden"), area("f-forbid", (project.forbidden_patterns || []).join("\n"))),
    h("p", { class: "help", text: t("settings.patterns_hint") }),
    h("p", { class: "help", text: t("settings.key_hint") }),
    h("p", { class: "help", text: t("settings.health_hint") }),
    h("p", { class: "help", text: t("settings.releases_hint") }),
    h("div", { class: "row" }, [
      h("button", { class: "btn primary", type: "submit", text: t("common.save") }),
      h("button", { class: "btn", type: "button", text: t("updates.scan"), onClick: checkDir }),
      h("button", { class: "btn", type: "button", text: t("settings.export"), onClick: exportProject }),
      h("button", { class: "btn", type: "button", text: t("settings.import"), onClick: () => document.getElementById("import-file").click() }),
      h("button", { class: "btn danger", type: "button", text: t("project.delete"), onClick: () => openModal({ type: "delete" }) }),
    ]),
    h("input", { id: "import-file", type: "file", accept: "application/json", hidden: "hidden", onChange: importProject }),
    h("div", { id: "check-line", class: "check-line muted" }),
  ]);
}

function renderServer() {
  const box = h("div", { class: "stack", id: "server-box" }, [h("p", { class: "muted", text: t("common.loading") })]);
  loadServer(box);
  return h("div", { class: "stack" }, [
    h("h2", { text: t("server.title") }),
    h("p", { text: t("server.intro") }),
    box,
  ]);
}

async function loadServer(box) {
  try {
    const data = await api("GET", "/api/projects/" + state.projectId + "/helper");
    if (!data.ready) {
      box.replaceChildren(h("div", { class: "blocker", text: t("server.not_ready") }));
      return;
    }
    const scriptBlock = h("div", { class: "stack" }, [
      h("div", { class: "row" }, [
        h("strong", { text: t("server.script") }),
        h("span", { class: "mono muted", text: data.script_path }),
        h("button", { class: "btn", type: "button", text: t("common.copy"), onClick: () => copyText(data.script) }),
        h("button", { class: "btn", type: "button", text: t("common.download"), onClick: () => downloadText(data.script_name, data.script) }),
      ]),
      h("pre", { class: "script", text: data.script }),
    ]);
    const sudoBlock = h("div", { class: "stack" }, [
      h("div", { class: "row" }, [
        h("strong", { text: t("server.sudoers") }),
        h("span", { class: "mono muted", text: data.sudoers_path }),
        h("button", { class: "btn", type: "button", text: t("common.copy"), onClick: () => copyText(data.sudoers) }),
        h("button", { class: "btn", type: "button", text: t("common.download"), onClick: () => downloadText(data.sudoers_name, data.sudoers) }),
      ]),
      h("pre", { class: "script", text: data.sudoers }),
    ]);
    const steps = h("ol", {}, data.steps.map((step) =>
      h("li", {}, [
        h("div", { text: t("server.step." + step.id) }),
        h("div", { class: "mono path", text: step.command }),
      ])
    ));
    box.replaceChildren(scriptBlock, sudoBlock, h("h3", { text: t("server.steps") }), steps);
  } catch (err) {
    box.replaceChildren(h("div", { class: "error-text", text: err.message }));
  }
}

function renderHistory() {
  const prune = h("div", { class: "panel row" }, [
    h("strong", { text: t("history.prune") }),
    h("button", { class: "btn", type: "button", text: t("history.prune_go"), onClick: () => openModal({ type: "prune" }) }),
    h("button", { class: "btn", type: "button", text: t("history.backups_load"), onClick: loadBackups }),
  ]);
  const listHost = h("div", { id: "backup-list" });
  if (!state.history.length) {
    return h("div", { class: "stack" }, [
      prune,
      listHost,
      h("div", { class: "empty" }, [
        h("h2", { text: t("history.empty_title") }),
        h("p", { class: "muted", text: t("history.empty_body") }),
      ]),
    ]);
  }
  const cards = state.history.map((item) => {
    const canRoll = item.kind === "deploy" && item.status !== "error_before_changes" && item.status !== "running";
    return h("article", { class: "history-card" }, [
      h("div", { class: "row" }, [
        h("span", { class: "badge " + item.status, text: t("status." + item.status) }),
        h("span", { class: "muted", text: t("history.kind_" + item.kind) }),
        h("strong", { text: item.update_name || "—" }),
      ]),
      h("div", { class: "muted", text: formatTime(item.started_at) + " · " + (item.actor || "") }),
      h("div", { class: "mono", text: (item.update_sha256 || "").slice(0, 12) }),
      h("div", { class: "muted", text: (item.duration_ms || 0) + " ms" }),
      h("div", { class: "row" }, [
        h("button", { class: "btn", type: "button", text: t("history.log"), onClick: () => showLog(item.id) }),
        canRoll ? h("button", {
          class: "btn danger",
          type: "button",
          text: t("history.rollback"),
          onClick: () => openModal({ type: "rollback", id: item.id, release: item.release_name }),
        }) : null,
      ]),
    ]);
  });
  return h("div", { class: "stack" }, [prune, listHost, h("div", {}, cards)]);
}

function renderModal() {
  const modal = state.modal;
  let body;
  if (modal.type === "new") {
    body = [
      h("h3", { text: t("project.new") }),
      h("p", { class: "help", text: t("project.name_help") }),
      h("input", { id: "new-name", placeholder: t("project.name_prompt") }),
      h("div", { class: "row" }, [
        h("button", { class: "btn primary", type: "button", text: t("common.save"), onClick: createProject }),
        h("button", { class: "btn", type: "button", text: t("common.cancel"), onClick: closeModal }),
      ]),
    ];
  } else if (modal.type === "delete") {
    body = [
      h("h3", { text: t("project.delete") }),
      h("p", { text: t("settings.delete_confirm", { name: state.project.name }) }),
      h("div", { class: "row" }, [
        h("button", { class: "btn danger", type: "button", text: t("project.delete"), onClick: deleteProject }),
        h("button", { class: "btn", type: "button", text: t("common.cancel"), onClick: closeModal }),
      ]),
    ];
  } else if (modal.type === "hostkey") {
    body = [
      h("h3", { text: t(modal.changed ? "confirm.hostkey_changed_title" : "confirm.hostkey_title") }),
      h("p", { text: t(modal.changed ? "confirm.hostkey_changed_body" : "confirm.hostkey_body") }),
      h("div", { class: "muted", text: t("confirm.fingerprint") }),
      h("div", { class: "fp", text: modal.fingerprint + " (" + modal.key_type + ")" }),
      h("div", { class: "row" }, [
        h("button", { class: "btn primary", type: "button", text: t("confirm.hostkey_accept"), onClick: acceptHostKey }),
        h("button", { class: "btn", type: "button", text: t("common.cancel"), onClick: closeModal }),
      ]),
    ];
  } else if (modal.type === "rollback") {
    body = [
      h("h3", { text: t("history.rollback_title") }),
      h("p", { text: t("history.rollback_body", { release: modal.release }) }),
      h("div", { class: "row" }, [
        h("button", { class: "btn danger", type: "button", text: t("common.confirm"), onClick: startRollback }),
        h("button", { class: "btn", type: "button", text: t("common.cancel"), onClick: closeModal }),
      ]),
    ];
  } else if (modal.type === "prune") {
    body = [
      h("h3", { text: t("history.prune") }),
      h("p", { class: "help", text: t("history.prune_keep") }),
      h("input", { id: "prune-keep", type: "number", value: "5", min: "0" }),
      h("div", { class: "row" }, [
        h("button", { class: "btn danger", type: "button", text: t("history.prune_go"), onClick: askPrune }),
        h("button", { class: "btn", type: "button", text: t("common.cancel"), onClick: closeModal }),
      ]),
    ];
  } else if (modal.type === "prune-confirm") {
    body = [
      h("h3", { text: t("history.prune") }),
      h("p", { text: t("history.prune_confirm", { keep: modal.keep }) }),
      h("div", { class: "row" }, [
        h("button", { class: "btn danger", type: "button", text: t("common.confirm"), onClick: startPrune }),
        h("button", { class: "btn", type: "button", text: t("common.cancel"), onClick: closeModal }),
      ]),
    ];
  } else if (modal.type === "log") {
    const lines = (modal.events || []).filter((event) => event.key).map((event) =>
      h("div", { class: event.level || "", text: t(event.key, event.params) })
    );
    body = [
      h("h3", { text: t("history.log") }),
      h("div", { class: "log" }, lines.length ? lines : [h("div", { text: "—" })]),
      h("button", { class: "btn", type: "button", text: t("common.close"), onClick: closeModal }),
    ];
  }
  return h("div", { class: "modal-back", onClick: (event) => { if (event.target === event.currentTarget) closeModal(); } }, [
    h("div", { class: "modal stack", role: "dialog" }, body),
  ]);
}

function openModal(modal) {
  state.modal = modal;
  render();
}
function closeModal() {
  state.modal = null;
  render();
}

async function boot() {
  try {
    const data = await api("GET", "/api/bootstrap");
    state.dict = data.i18n;
    state.rule = data.release_rule;
    document.documentElement.lang = state.lang;
    await loadProjects();
  } catch (err) {
    state.bootError = err.message || t("toast.network");
  }
  render();
  setInterval(() => {
    if (state.tab === "updates" && state.project && !state.preview && !state.run && !state.modal) {
      refreshUpdates(true);
    }
  }, 2000);
}

async function loadProjects() {
  const data = await api("GET", "/api/projects");
  state.projects = data.projects || [];
  if (!state.projects.some((item) => item.id === state.projectId)) {
    state.projectId = state.projects.length ? state.projects[0].id : 0;
  }
  if (state.projectId) await selectProject(state.projectId, true);
  else state.project = null;
}

async function selectProject(id, silent) {
  state.projectId = id;
  sessionStorage.setItem("butler-project", String(id));
  state.preview = null;
  state.run = null;
  const data = await api("GET", "/api/projects/" + id);
  state.project = data.project;
  await refreshUpdates(true);
  await refreshHistory();
  if (!silent) render();
}

async function refreshUpdates(silent) {
  if (!state.projectId) return;
  try {
    const data = await api("GET", "/api/projects/" + state.projectId + "/updates");
    const next = JSON.stringify(data.updates || []);
    const changed = next !== state.updatesJson || (data.error || "") !== state.updatesError;
    state.updatesJson = next;
    state.updates = data.updates || [];
    state.updatesError = data.error || "";
    if (!changed && silent) return;
    if (state.tab === "updates" && !state.preview && !state.run && !state.modal) render();
  } catch (err) {
    if (!silent) toast("err", err.message);
  }
}

async function refreshHistory() {
  if (!state.projectId) return;
  const data = await api("GET", "/api/projects/" + state.projectId + "/history");
  state.history = data.history || [];
}

async function createProject() {
  const name = document.getElementById("new-name").value.trim();
  try {
    const data = await api("POST", "/api/projects", { name });
    state.modal = null;
    state.tab = "settings";
    await loadProjects();
    await selectProject(data.project.id, true);
    toast("ok", t("project.created"));
    render();
  } catch (err) {
    toast("err", validationMessage(err));
  }
}

function validationMessage(err) {
  const fields = err.data && err.data.fields;
  if (fields) {
    return Object.keys(fields).map((name) => name + ": " + t("validation." + fields[name])).join("; ");
  }
  return err.message || t("toast.network");
}

async function saveSettings(event) {
  event.preventDefault();
  const value = (id) => document.getElementById(id).value;
  const body = {
    name: value("f-name").trim(),
    updates_dir: value("f-updates").trim(),
    update_format: value("f-format"),
    deploy_mode: value("f-mode"),
    ssh_host: value("f-host").trim(),
    ssh_port: Number(value("f-port") || 22),
    ssh_user: value("f-user").trim(),
    ssh_key_path: value("f-key").trim(),
    app_dir: value("f-app").trim(),
    service_type: value("f-service"),
    service_name: value("f-service-name").trim(),
    app_owner: value("f-owner").trim(),
    backups_dir: value("f-backups").trim(),
    healthcheck_url: value("f-health").trim(),
    expected_codes: value("f-codes").split(/[\s,]+/).filter(Boolean).map(Number),
    expected_text: value("f-expect"),
    unreachable_hint: value("f-hint").trim(),
    db_backup_paths: linesOf(value("f-db")),
    log_error_patterns: linesOf(value("f-logs")),
    forbidden_patterns: linesOf(value("f-forbid")),
  };
  try {
    const data = await api("PUT", "/api/projects/" + state.projectId, body);
    state.project = data.project;
    await loadProjects();
    toast("ok", t("settings.saved"));
    render();
  } catch (err) {
    toast("err", validationMessage(err));
  }
}

async function checkDir() {
  const line = document.getElementById("check-line");
  try {
    const data = await api("POST", "/api/check-dir", {
      updates_dir: document.getElementById("f-updates").value.trim(),
      update_format: document.getElementById("f-format").value,
    });
    line.textContent = data.ok ? t("settings.check_ok", { count: data.count }) : t("settings.check_bad");
    line.className = data.ok ? "check-line muted" : "check-line error-text";
  } catch (_err) {
    line.textContent = t("settings.check_bad");
  }
}

async function exportProject() {
  const response = await fetch("/api/projects/" + state.projectId + "/export", { headers: { Accept: "application/json" } });
  if (!response.ok) {
    toast("err", t("common.error"));
    return;
  }
  const text = await response.text();
  downloadText("deploy-butler-" + state.project.name + ".json", text);
}

async function importProject(event) {
  const file = event.target.files && event.target.files[0];
  event.target.value = "";
  if (!file) return;
  try {
    const payload = JSON.parse(await file.text());
    const data = await api("POST", "/api/import", payload);
    await loadProjects();
    await selectProject(data.project.id, true);
    toast("ok", t("settings.import_ok"));
    render();
  } catch (err) {
    toast("err", validationMessage(err) || t("toast.import_fail"));
  }
}

async function deleteProject() {
  await api("DELETE", "/api/projects/" + state.projectId);
  state.modal = null;
  state.project = null;
  state.projectId = 0;
  await loadProjects();
  render();
}

async function openPreview(name) {
  try {
    const data = await api("POST", "/api/projects/" + state.projectId + "/preview", { update_name: name });
    state.preview = data;
    render();
    if (data.needs_hostkey) {
      openModal({
        type: "hostkey",
        fingerprint: data.fingerprint,
        key_type: data.key_type,
        changed: Boolean(data.changed),
        update: name,
      });
    }
  } catch (err) {
    toast("err", err.message);
  }
}

async function acceptHostKey() {
  const modal = state.modal;
  try {
    await api("POST", "/api/host-keys/confirm", { project_id: state.projectId, fingerprint: modal.fingerprint });
    const update = modal.update;
    state.modal = null;
    await openPreview(update);
  } catch (err) {
    toast("err", err.message);
  }
}

async function confirmDeploy() {
  const preview = state.preview;
  const backup = document.getElementById("backup-db");
  const notify = document.getElementById("os-notify");
  state.osNotify = Boolean(notify && notify.checked);
  localStorage.setItem("butler-notify", state.osNotify ? "1" : "0");
  try {
    const data = await api("POST", "/api/projects/" + state.projectId + "/deploys", {
      update_name: preview.name,
      backup_db: Boolean(backup && backup.checked),
      os_notify: state.osNotify,
    });
    state.preview = null;
    state.run = { id: data.deploy_id, events: [], status: "", duration: 0 };
    render();
    listen(data.deploy_id);
  } catch (err) {
    toast("err", err.message);
  }
}

function listen(id) {
  const source = new EventSource("/api/runs/" + id + "/events");
  source.onmessage = (message) => {
    const data = JSON.parse(message.data);
    if (data.event === "done") {
      state.run.status = data.status;
      state.run.duration = data.duration_ms;
      source.close();
      toast("ok", t("toast.deploy_done", { status: t("status." + data.status), name: state.project.name }));
      refreshHistory();
      render();
      return;
    }
    if (data.key) {
      state.run.events.push(data);
      const log = document.getElementById("live-log");
      if (log) {
        log.append(h("div", { class: data.level || "", text: t(data.key, data.params) }));
        log.scrollTop = log.scrollHeight;
      }
    }
  };
  source.onerror = () => {
    source.close();
  };
}

async function showLog(id) {
  try {
    const data = await api("GET", "/api/projects/" + state.projectId + "/history/" + id);
    openModal({ type: "log", events: data.deploy.log || [] });
  } catch (err) {
    toast("err", err.message);
  }
}

async function startRollback() {
  const modal = state.modal;
  try {
    const data = await api("POST", "/api/projects/" + state.projectId + "/history/" + modal.id + "/rollback", {
      confirm: true,
      os_notify: state.osNotify,
    });
    state.modal = null;
    state.tab = "updates";
    state.run = { id: data.deploy_id, events: [], status: "", duration: 0 };
    render();
    listen(data.deploy_id);
  } catch (err) {
    toast("err", err.message);
  }
}

function askPrune() {
  const keep = Number(document.getElementById("prune-keep").value);
  openModal({ type: "prune-confirm", keep });
}

async function startPrune() {
  try {
    const data = await api("POST", "/api/projects/" + state.projectId + "/prune", {
      confirm: true,
      keep: state.modal.keep,
    });
    state.modal = null;
    state.tab = "updates";
    state.run = { id: data.deploy_id, events: [], status: "", duration: 0 };
    render();
    listen(data.deploy_id);
  } catch (err) {
    toast("err", err.message);
  }
}

async function loadBackups() {
  const host = document.getElementById("backup-list");
  if (!host) return;
  host.textContent = t("common.loading");
  try {
    const data = await api("POST", "/api/projects/" + state.projectId + "/backups", {});
    if (!data.ok) {
      host.textContent = data.error || t("common.error");
      return;
    }
    if (!data.backups.length) {
      host.textContent = "—";
      return;
    }
    host.replaceChildren(h("ul", {}, data.backups.map((item) =>
      h("li", { class: "mono", text: item.name + " · " + formatBytes(item.size) })
    )));
  } catch (err) {
    host.textContent = err.message;
  }
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.modal) closeModal();
});

boot();
