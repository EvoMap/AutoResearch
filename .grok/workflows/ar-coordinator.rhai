let meta = #{
    name: "ar-coordinator",
    description: "Scale an AutoResearch idea on Grok Build: init, then waves of parallel claim workers over the engine ready-front until close",
    when_to_use: "Run AutoResearch at Grok scale. Parallel claim workers drain the engine DAG. /ar-coordinator",
    phases: [
        #{ title: "Init", detail: "provenance, preflight, engine, monitor" },
        #{ title: "Pool", detail: "waves of parallel claim workers on the ready-front" },
        #{ title: "Close", detail: "stop monitor and emit done" },
    ],
};

fn trimmed(s) {
    if type_of(s) == "string" {
        s.trim();
        s
    } else {
        ""
    }
}

if args == () {
    pause("verification", "Pass args.idea_file (and optional args.project_root).");
}
let idea_file = args.idea_file;
if idea_file == () || idea_file == "" {
    pause("verification", "Pass args.idea_file.");
}
let project_root_arg = "";
if args.project_root != () { project_root_arg = args.project_root; }
let max_parallel = 8;
if args.max_parallel != () && args.max_parallel >= 1 && args.max_parallel <= 32 {
    max_parallel = args.max_parallel;
}
let max_waves = 24;
if args.max_waves != () && args.max_waves >= 1 && args.max_waves <= 64 {
    max_waves = args.max_waves;
}

let init_schema = #{
    "type": "object",
    "required": ["ok", "project_root", "slug"],
    "properties": #{
        "ok": #{ "type": "boolean" },
        "idea_file": #{ "type": "string" },
        "project_root": #{ "type": "string" },
        "slug": #{ "type": "string" },
        "error": #{ "type": "string" },
    },
};
let scout_schema = #{
    "type": "object",
    "required": ["finished", "blocked", "width"],
    "properties": #{
        "finished": #{ "type": "boolean" },
        "blocked": #{ "type": "boolean" },
        "width": #{ "type": "integer" },
        "pending": #{ "type": "integer" },
        "running": #{ "type": "integer" },
        "blocked_reason": #{ "type": "string" },
        "units": #{
            "type": "array",
            "maxItems": 32,
            "items": #{
                "type": "object",
                "required": ["id", "type"],
                "properties": #{
                    "id": #{ "type": "string" },
                    "type": #{ "type": "string" },
                    "cycle": #{ "type": "integer" },
                },
            },
        },
    },
};
let worker_schema = #{
    "type": "object",
    "required": ["status", "summary"],
    "properties": #{
        "status": #{ "type": "string" },
        "summary": #{ "type": "string" },
        "unit_id": #{ "type": "string" },
        "unit_type": #{ "type": "string" },
        "empty": #{ "type": "boolean" },
        "subcoder_requests": #{
            "type": "array",
            "maxItems": 16,
            "items": #{
                "type": "object",
                "required": ["file_to_write", "task"],
                "properties": #{
                    "file_to_write": #{ "type": "string" },
                    "task": #{ "type": "string" },
                    "interface": #{ "type": "string" },
                    "constraints": #{ "type": "string" },
                    "plan_excerpt": #{ "type": "string" },
                    "max_lines": #{ "type": "integer" },
                },
            },
        },
    },
};

phase("Init");
let init_prompt = "Initialize an AutoResearch project. Use run_terminal_command, write, and read_file.";
init_prompt += " You only initialize; the parent workflow fans out claim workers after you return.";
init_prompt += " REPO=`git rev-parse --show-toplevel`; AR_RUNTIME=$REPO/ar-runtime.";
init_prompt += " idea_file=" + json_encode(idea_file);
init_prompt += " optional_project_root=" + json_encode(project_root_arg);
init_prompt += " Run inspect, ./scripts/ar-preflight-mcp.sh (no trailing echo), prepare,";
init_prompt += " start ar-gemini-monitor.py in the background unless AR_SUPERVISOR_MONITOR=1,";
init_prompt += " and ar-workflow-engine.py init --max-cycles 3. Command tool use.";
init_prompt += " Return {ok, idea_file, project_root, slug, error}.";

let init = agent(init_prompt, #{
    label: "ar-init",
    capability_mode: "all",
    output_schema: init_schema,
});
if init == () || init.success == false {
    pause("infra", "AutoResearch init agent failed.");
}
if init.output.ok == false {
    let err = "Init returned ok=false";
    if init.output.error != () { err = init.output.error; }
    pause("infra", "AutoResearch init failed: " + err);
}
let project_root = project_root_arg;
if init.output.project_root != () && trimmed(init.output.project_root) != "" {
    project_root = init.output.project_root;
}
if project_root == "" {
    pause("verification", "Init did not return project_root; pass args.project_root.");
}
log("project_root " + project_root + " max_parallel=" + max_parallel.to_string());

let units_run = 0;
let last_summary = "";
let finished = false;
let blocked = false;
let blocked_reason = "";
let prev_fp = "";
let stall = 0;

phase("Pool");
for wave in 0..max_waves {
    let b = budget();
    if b.remaining < 6 {
        log("agent budget remaining=" + b.remaining.to_string() + "; pausing pool");
        break;
    }
    log("wave " + (wave + 1).to_string() + "/" + max_waves.to_string());
    let scout_p = "Probe the AutoResearch engine ready-front. Use run_terminal_command.";
    scout_p += " cd $(git rev-parse --show-toplevel)/ar-runtime && python scripts/ar-workflow-engine.py";
    scout_p += " ready --project-root " + json_encode(project_root);
    scout_p += " Parse the JSON. Set width to the ready list length. Copy ready[] into units";
    scout_p += " as {id, type, cycle}. finished=true only if width=0 AND counts.pending=0 AND";
    scout_p += " counts.running=0 (queue drained). blocked=true if units are blocked needing a human.";
    scout_p += " Command tool use; do not invent unit ids.";

    let scout = agent(scout_p, #{
        label: "ready:" + wave.to_string(),
        capability_mode: "execute",
        output_schema: scout_schema,
    });
    if scout == () || scout.success == false {
        pause("infra", "ready scout failed under " + project_root);
    }
    if scout.output.blocked == true {
        blocked = true;
        blocked_reason = trimmed(scout.output.blocked_reason);
        break;
    }
    let width = 0;
    if scout.output.width != () { width = scout.output.width; }
    if width < 0 { width = 0; }
    let pending = 0;
    let running = 0;
    if scout.output.pending != () { pending = scout.output.pending; }
    if scout.output.running != () { running = scout.output.running; }
    if scout.output.finished == true || (width == 0 && pending == 0 && running == 0) {
        finished = true;
        break;
    }
    if width == 0 {
        log("ready width 0 with pending=" + pending.to_string() + " running=" + running.to_string());
        break;
    }

    let ids = "";
    if scout.output.units != () {
        for u in scout.output.units {
            if u.id != () { ids += trimmed(u.id) + ","; }
        }
    }
    let fp = fingerprint(ids);
    if fp == prev_fp && fp != "" {
        stall += 1;
    } else {
        stall = 0;
    }
    prev_fp = fp;
    if stall >= 2 {
        pause("no_progress", "Ready-front did not change for two waves under " + project_root);
    }

    let room = b.remaining - 4;
    let n = width;
    if n > max_parallel { n = max_parallel; }
    if n > room { n = room; }
    if n < 1 { n = 1; }
    log("claim panel n=" + n.to_string() + " width=" + width.to_string());

    let jobs = [];
    for wi in 0..n {
        let worker_id = "grok-w" + wave.to_string() + "-" + wi.to_string();
        let wp = "You are AutoResearch claim worker " + worker_id + ".";
        wp += " Use run_terminal_command, read_file, write, search_replace, grep, list_dir,";
        wp += " search_tool, and use_tool. You are a leaf this tick: do the claimed unit yourself.";
        wp += " The parent already fanned out sibling workers in this wave.";
        wp += " project_root=" + json_encode(project_root);
        wp += " ENGINE=$(git rev-parse --show-toplevel)/ar-runtime/scripts/ar-workflow-engine.py";
        wp += " 1) python $ENGINE claim --project-root <root> --worker " + worker_id + " --prompt";
        wp += " 2) If JSON status=empty, return {status:empty, summary:empty, empty:true}.";
        wp += " 3) Otherwise execute the printed unit. planning: write plan.md.";
        wp += " coding: implement the entrypoint (--stage/--artifact-dir/--run-log) and modules.";
        wp += " review: call ar-gemini-review__gemini_review with unit and cycle.";
        wp += " run: execute-run via the project .venv; write the receipt before complete.";
        wp += " result-analysis: write findings then after-result-analysis --worker " + worker_id + ".";
        wp += " critic: call ar-external-critic__external_critic then after-critic --worker.";
        wp += " blind-review: write submission.md, call blind_review, then after-blind-review --worker.";
        wp += " agent/spawn_agents: complete --status done --worker.";
        wp += " close: verify critic/blind-review then complete close --worker.";
        wp += " Always pass --worker " + worker_id + " on complete/after-*/heartbeat.";
        wp += " Heartbeat if the unit will run long. Writes stay under project_root. No sudo, no rm -rf.";
        wp += " Return {status, summary, unit_id, unit_type, empty?, subcoder_requests?}.";
        jobs.push(#{
            prompt: wp,
            label: worker_id,
            capability_mode: "all",
            output_schema: worker_schema,
        });
    }
    let results = parallel(jobs);
    let ri = 0;
    let sub_jobs = [];
    for r in results {
        if r != () && r.success && r.output.empty != true {
            units_run += 1;
            if r.output.summary != () { last_summary = trimmed(r.output.summary); }
            if r.output.subcoder_requests != () {
                for req in r.output.subcoder_requests {
                    if sub_jobs.len() >= 16 { break; }
                    let sp = "Implement one module. Write only file_to_write. Use write/read_file.";
                    sp += " Request JSON: " + json_encode(req);
                    sp += " Return {status, summary}.";
                    sub_jobs.push(#{
                        prompt: sp,
                        label: "subcoder-" + wave.to_string() + "-" + sub_jobs.len().to_string(),
                        agent_type: "ar-subcoder",
                        capability_mode: "all",
                        output_schema: worker_schema,
                    });
                }
            }
        } else if r == () || r.success == false {
            log("claim worker slot " + ri.to_string() + " failed");
        }
        ri += 1;
    }
    if sub_jobs.len() > 0 {
        log("subcoder panel " + sub_jobs.len().to_string());
        let sub_results = parallel(sub_jobs);
        log("subcoders returned " + sub_results.len().to_string());
    }
}

phase("Close");
if blocked {
    complete(#{
        summary: "Blocked: " + blocked_reason,
        project_root: project_root,
        units_run: units_run,
        finished: false,
        blocked: true,
    });
}
if finished == false {
    complete(#{
        summary: "Paused after " + units_run.to_string() + " unit(s). Last: " + last_summary,
        project_root: project_root,
        units_run: units_run,
        finished: false,
        blocked: false,
    });
}
let close_p = "Engine ready-front is drained for " + json_encode(project_root);
close_p += ". Read state.md and workflow_queue.json. If close is done, stop the monitor you started.";
close_p += " Return {status, summary}. Use tools.";
let closer = agent(close_p, #{
    label: "ar-close",
    capability_mode: "all",
    output_schema: worker_schema,
});
let close_summary = last_summary;
if closer != () && closer.success && closer.output.summary != () {
    close_summary = closer.output.summary;
}
complete(#{
    summary: close_summary,
    project_root: project_root,
    units_run: units_run,
    finished: true,
    blocked: false,
    promise: "AUTORESEARCH_DONE",
});
