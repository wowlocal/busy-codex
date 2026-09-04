/* Astra Watch — BUSY Bar JavaScript application.
 *
 * The Mac-side daemon exposes only non-secret rollout metadata. Opening this
 * app asks it to refresh the personalized Codex model catalog, then shows the
 * personal state plus a conservative X reporter-sample rollout proxy. OAuth
 * stays on the user's other Mac; this app receives aggregate, non-secret data.
 */

var DRAW_URL = "http://127.0.0.1/api/display/draw";
var STATUS_URL = "http://10.0.4.21:8765/astra";
var APP = "astra_watch_ai";
var PRIORITY = 70;

var POLL_MS = 2000;
var TEXT_TIMEOUT = 15;
var ANIM_TIMEOUT = 120;
var ANIM_REFRESH_MS = 60000;
var TEXT_KEEPALIVE_MS = 8000;

var STATE_ANIMS = {
    waiting: "watch.anim", hidden: "hidden.anim", available: "ready.anim",
    error: "error.anim", stale: "error.anim", unknown: "offline.anim",
    offline: "offline.anim"
};
var STATE_WORDS = {
    waiting: "WAIT", hidden: "HIDDEN", available: "READY",
    error: "ERROR", stale: "STALE", unknown: "NO DATA",
    offline: "NO LINK"
};
var STATE_COLORS = {
    waiting: "#809BCEFF", hidden: "#FFB000FF", available: "#20C040FF",
    error: "#FF2020FF", stale: "#FF2020FF", unknown: "#606060FF",
    offline: "#606060FF"
};

var lastAnimPath = "";
var lastAnimAt = 0;
var lastTexts = "";
var lastTextsAt = 0;

function now() { return Date.now(); }

function draw(elements) {
    return fetch({
        url: DRAW_URL,
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            application_name: APP, priority: PRIORITY, elements: elements
        })
    });
}

function textEl(id, x, y, align, text, color) {
    return { id: id, type: "text", display: "front", x: x, y: y,
             align: align, text: text, font: "small", color: color,
             timeout: TEXT_TIMEOUT };
}

function rectEl(id, x, y, w, h, color) {
    return { id: id, type: "rectangle", display: "front", x: x, y: y,
             width: w, height: h, border_width: 0, fill: "solid",
             fill_colors: [color], timeout: TEXT_TIMEOUT };
}

function ageLabel(seconds) {
    if (typeof seconds !== "number") return "?";
    if (seconds < 60) return "NOW";
    if (seconds < 3600) return Math.floor(seconds / 60) + "m";
    if (seconds < 86400) return Math.floor(seconds / 3600) + "h";
    return Math.floor(seconds / 86400) + "d";
}

function nextLabel(seconds) {
    if (typeof seconds !== "number") return "?";
    if (seconds < 60) return "<1m";
    if (seconds < 3600) return Math.ceil(seconds / 60) + "m";
    return Math.ceil(seconds / 3600) + "h";
}

function rolloutColor(pulse) {
    if (pulse.error || pulse.stale) return "#FF2020FF";
    if (pulse.official_stage === "all") return "#20C040FF";
    if (pulse.official_stage === "rolling") return "#FFB000FF";
    if (pulse.official_stage === "limited") return "#FF6A00FF";
    return "#606060FF";
}

function stageLabel(stage) {
    if (stage === "limited") return "LIMITED";
    if (stage === "rolling") return "ROLLING";
    if (stage === "all") return "ALL USERS";
    if (stage === "announced") return "ANNOUNCED";
    return "NO STAGE";
}

function stageFill(stage) {
    if (stage === "all") return 12;
    if (stage === "rolling") return 8;
    if (stage === "limited") return 4;
    if (stage === "announced") return 2;
    return 0;
}

function rolloutLabel(stage) {
    if (stage === "seed") return "SEED";
    if (stage === "early") return "EARLY";
    if (stage === "growing") return "GROWING";
    if (stage === "broad") return "BROAD";
    if (stage === "wide") return "WIDE";
    return "NO SIGNAL";
}

function rolloutFill(stage) {
    if (stage === "wide") return 12;
    if (stage === "broad") return 10;
    if (stage === "growing") return 7;
    if (stage === "early") return 4;
    if (stage === "seed") return 2;
    return 0;
}

function qualityLabel(quality) {
    if (quality === "high") return "H";
    if (quality === "medium") return "M";
    return "L";
}

function temperatureLabel(value) {
    if (value === "fire") return "FIRE";
    if (value === "hot") return "HOT";
    if (value === "warm") return "WARM";
    return "COLD";
}

function temperatureColor(value) {
    if (value === "fire") return "#FF2020FF";
    if (value === "hot") return "#FF6A00FF";
    if (value === "warm") return "#FFB000FF";
    return "#809BCEFF";
}

function infoElements(st) {
    var state = st.state || "unknown";
    var color = STATE_COLORS[state] || STATE_COLORS.unknown;
    var pulse = st.x_pulse || {};
    var stage = pulse.official_stage || "unknown";
    var rollout = pulse.rollout_stage || "unknown";
    var hasRollout = pulse.enabled && typeof pulse.ready_reporters === "number" &&
                     typeof pulse.reporter_sample === "number";
    var hasPulse = pulse.enabled && typeof pulse.field_count === "number";
    var fill = hasRollout ? rolloutFill(rollout) : stageFill(stage);
    var barColor = pulse.llm_enabled && pulse.llm_reviewed_count > 0
        ? temperatureColor(pulse.signal_temperature) : rolloutColor(pulse);
    var left = "";
    var right = "";
    var leftColor = pulse.error ? "#FF2020FF" : "#A8A8A8FF";

    if (hasRollout && (pulse.state === "ready" || pulse.state === "stale" ||
                      stage !== "unknown")) {
        if (pulse.llm_enabled && pulse.llm_error) {
            left = "AI ERROR";
            leftColor = "#FF2020FF";
        } else if (pulse.llm_enabled && pulse.llm_reviewed_count > 0) {
            left = "AI" + Math.min(99, pulse.llm_reviewed_count) + " " +
                   temperatureLabel(pulse.signal_temperature);
            leftColor = temperatureColor(pulse.signal_temperature);
        } else if (pulse.llm_enabled && pulse.refreshing) {
            left = "AI SCAN";
            leftColor = "#809BCEFF";
        } else {
            left = rolloutLabel(rollout) + " Q:" + qualityLabel(pulse.data_quality);
        }
        if (pulse.panel_transitions_12h > 0) {
            right = "+" + pulse.panel_transitions_12h + "/12H";
        } else if (pulse.reporter_sample > 0) {
            right = "R" + pulse.ready_reporters + "/W" + pulse.waiting_reporters +
                    (pulse.reporter_capped ? "+" : "");
        } else {
            right = "NO SAMPLE";
        }
    } else if (hasPulse && (pulse.state === "ready" || pulse.state === "stale" ||
                     stage !== "unknown")) {
        left = stageLabel(stage);
        right = "FIELD " + pulse.field_count + (pulse.field_capped ? "+" : "");
    } else if (pulse.enabled && pulse.state === "refreshing") {
        left = "X SCAN";
        right = "...";
    } else if (pulse.enabled && pulse.state === "error") {
        left = "X ERROR";
        right = "RETRY";
    } else {
        var age = ageLabel(st.age_s);
        var interval = st.check_interval_s || 600;
        var next = typeof st.next_check_s === "number" ? st.next_check_s : interval;
        left = "CHK " + age;
        right = "NXT " + nextLabel(next);
    }

    var elements = [
        textEl("title", 3, 0, "top_left", "ASTRA", "#FFFFFFFF"),
        textEl("state", 69, 0, "top_right", STATE_WORDS[state] || "UNKNOWN", color),
        rectEl("track", 29, 3, 12, 3, "#242424FF"),
        rectEl("fill", 29, 3, Math.max(1, fill), 3,
               fill > 0 ? barColor : "#242424FF"),
        textEl("client", 3, 15, "bottom_left", left,
               leftColor),
        textEl("fresh", 69, 15, "bottom_right", right, "#A8A8A8FF")
    ];
    return elements;
}

function render(st) {
    var state = st.state || "unknown";
    var animPath = STATE_ANIMS[state] || STATE_ANIMS.unknown;
    var t = now();

    var animation = null;
    if (animPath !== lastAnimPath || t - lastAnimAt > ANIM_REFRESH_MS) {
        lastAnimPath = animPath;
        lastAnimAt = t;
        animation = { id: "ring", type: "animation", display: "front", x: 0, y: 0,
                      path: animPath, loop: true, timeout: ANIM_TIMEOUT };
    }

    var texts = infoElements(st);
    var key = JSON.stringify(texts);
    if (key !== lastTexts || t - lastTextsAt > TEXT_KEEPALIVE_MS) {
        lastTexts = key;
        lastTextsAt = t;
        if (animation) texts.unshift(animation);
        return draw(texts);
    }
    if (animation) return draw([animation]);
    return Promise.resolve();
}

var pollBusy = false;
function poll() {
    if (pollBusy) return;
    pollBusy = true;
    fetch({ url: STATUS_URL, method: "GET" })
        .then(function (response) { return response.json(); })
        .then(function (status) { return render(status); })
        .then(function () { pollBusy = false; })
        .catch(function (error) {
            console.error("Astra status fetch failed: " + error);
            render({ state: "offline" }).then(function () { pollBusy = false; });
        });
}

console.info("Astra Watch app started");
/* The first status request also asks the Mac daemon for a catalog refresh.
 * Keeping one network request in flight avoids exhausting the firmware's
 * intentionally small fetch pool. */
poll();
setInterval(poll, POLL_MS);
