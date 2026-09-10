let meta = #{
    name: "ar-coordinator",
    description: "Run an AutoResearch idea through the engine-owned unit queue on Grok (plan, code, review, run, critic, blind review)",
    when_to_use: "Execute an AutoResearch idea end-to-end, continue a project_root, or replace Claude ralph-loop. /ar-coordinator",
    phases: [
        #{ title: "Init", detail: "provenance, preflight, engine init, monitor" },
        #{ title: "Units", detail: "next-prompt then specialist agents one unit at a time" },
        #{ title: "Close", detail: "stop monitor if the engine has closed" },
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
    pause("verification", "Pass args.idea_file — path to the idea markdown or text file.");
}
let project_root_arg = "";
if args.project_root != () { project_root_arg = args.project_root; }
let max_units = 24;
if args.max_units != () && args.max_units >= 1 && args.max_units <= 40 {
    max_units = args.max_units;
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
let unit_schema = #{
    "type": "object",
    "required": ["finished", "blocked", "unit_id", "unit_type", "project_root", "engine_prompt"],
    "properties": #{
        "finished": #{ "type": "boolean" },
        "blocked": #{ "type": "boolean" },
        "unit_id": #{ "type": "string" },
        "unit_type": #{ "type": "string" },
        "cycle": #{ "type": "integer" },
        "stage": #{ "type": "string" },
        "project_root": #{ "type": "string" },
        "engine_prompt": #{ "type": "string" },
        "blocked_reason": #{ "type": "string" },
    },
};
let worker_schema = #{
    "type": "object",
    "required": ["status", "summary"],
    "properties": #{
        "status": #{ "type": "string" },
        "summary": #{ "type": "string" },
        "artifact_path": #{ "type": "string" },
    },
};

phase("Init");
let init_prompt = "Initialize an AutoResearch project. Use run_terminal_command and write/read_file.";
init_prompt += " Do not spawn subagents. REPO=`git rev-parse --show-toplevel`; AR_RUNTIME=$REPO/ar-runtime.";
init_prompt += " idea_file=" + json_encode(idea_file);
init_prompt += " optional_project_root=" + json_encode(project_root_arg);
init_prompt += " Run: python $REPO/src/idea_provenance.py inspect --idea-file <idea>;";
init_prompt += " cd $AR_RUNTIME && ./scripts/ar-preflight-mcp.sh (do not append echo);";
init_prompt += " python $REPO/src/idea_provenance.py prepare --idea-file --project-root;";
init_prompt += " start scripts/ar-gemini-monitor.py in the background unless AR_SUPERVISOR_MONITOR=1;";
init_prompt += " python scripts/ar-workflow-engine.py init --project-root --max-cycles 3.";
init_prompt += " Return {ok, idea_file, project_root, slug, error}. Command tool use.";

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
log("project_root " + project_root);

let planner_id = ();
let coder_id = ();
let runner_id = ();
let units_run = 0;
let last_summary = "";
let finished = false;
let blocked = false;
let blocked_reason = "";

phase("Units");
for unit_n in 0..max_units {
    log("unit tick " + (unit_n + 1).to_string() + "/" + max_units.to_string());
    let sel_prompt = "Run next-prompt and parse it. Use run_terminal_command. Do not execute the unit.";
    sel_prompt += " cd $(git rev-parse --show-toplevel)/ar-runtime && python scripts/ar-workflow-engine.py";
    sel_prompt += " next-prompt --project-root " + json_encode(project_root);
    sel_prompt += " If stdout has no next_unit or says emit AUTORESEARCH_DONE, set finished=true and empty unit_id.";
    sel_prompt += " If human intervention is required, set blocked=true. Otherwise fill unit_id, unit_type,";
    sel_prompt += " cycle, stage from the printed fields and put the full stdout in engine_prompt.";
    sel_prompt += " Command tool use; do not invent a unit id.";

    let sel = agent(sel_prompt, #{
        label: "next-prompt",
        capability_mode: "execute",
        output_schema: unit_schema,
    });
    if sel == () || sel.success == false {
        pause("infra", "next-prompt agent failed under " + project_root);
    }
    if sel.output.blocked == true {
        blocked = true;
        blocked_reason = trimmed(sel.output.blocked_reason);
        break;
    }
    let unit_id = trimmed(sel.output.unit_id);
    let unit_type = trimmed(sel.output.unit_type);
    let engine_prompt = "";
    if sel.output.engine_prompt != () { engine_prompt = sel.output.engine_prompt; }
    if sel.output.finished == true || unit_id == "" || unit_type == "" {
        finished = true;
        break;
    }
    log("unit " + unit_id + " type=" + unit_type);
    units_run += 1;

    let work_prompt = "Execute exactly one AutoResearch engine unit. Use tools; do not spawn subagents.";
    work_prompt += " Writes stay under project_root; no rm -rf; no sudo. Implement large modules yourself";
    work_prompt += " (Grok children cannot nest). Close the unit only with the engine command in the prompt.";
    work_prompt += " project_root=" + json_encode(project_root);
    work_prompt += " unit_id=" + json_encode(unit_id);
    work_prompt += " unit_type=" + json_encode(unit_type);
    work_prompt += " Engine prompt JSON: " + json_encode(engine_prompt);
    work_prompt += " Return {status, summary, artifact_path?}.";

    let agent_type = "general-purpose";
    let resume = ();
    if unit_type == "planning" {
        agent_type = "ar-planner";
        resume = planner_id;
        work_prompt += " Write or revise plan.md only, then stop. A reviewer gate runs after you.";
    } else if unit_type == "coding" {
        agent_type = "ar-coder";
        resume = coder_id;
        work_prompt += " Implement the entrypoint with --stage --artifact-dir --run-log. Write every module.";
    } else if unit_type == "review" {
        agent_type = "ar-gemini-reviewer";
        work_prompt += " mode=code_review. Call ar-gemini-review__gemini_review with unit and cycle.";
    } else if unit_type == "run" {
        agent_type = "ar-runner";
        resume = runner_id;
        work_prompt += " Use execute-run and project_root/.venv. Write the run receipt before complete.";
    } else if unit_type == "critic" {
        agent_type = "ar-critic";
        work_prompt += " Call ar-external-critic__external_critic. Do not write critic.md yourself.";
    } else if unit_type == "blind-review" {
        agent_type = "ar-blind-reviewer";
        work_prompt += " Write submission.md; call ar-external-critic__blind_review.";
    } else if unit_type == "result-analysis" {
        work_prompt += " Write findings into state.md after this claim, then after-result-analysis.";
    } else if unit_type == "agent" {
        work_prompt += " spawn_agents: persistents are created lazily. complete --status done.";
    } else if unit_type == "close" {
        work_prompt += " Verify critic and blind-review, then complete the close unit.";
    }

    let wopts = #{
        label: unit_id,
        capability_mode: "all",
        agent_type: agent_type,
        output_schema: worker_schema,
    };
    if resume != () {
        wopts.resume_from = resume;
    }
    let worker = agent(work_prompt, wopts);
    if worker == () || worker.success == false {
        pause("infra", "worker failed for " + unit_id);
    }
    last_summary = trimmed(worker.output.summary);
    if agent_type == "ar-planner" && worker.agent_id != () { planner_id = worker.agent_id; }
    if agent_type == "ar-coder" && worker.agent_id != () { coder_id = worker.agent_id; }
    if agent_type == "ar-runner" && worker.agent_id != () { runner_id = worker.agent_id; }

    if unit_type == "planning" || unit_type == "coding" || unit_type == "run" {
        let gate_mode = "plan_gate";
        if unit_type == "coding" { gate_mode = "code_gate"; }
        if unit_type == "run" { gate_mode = "run_gate"; }
        let gate_p = "mode=" + gate_mode + " project_root=" + json_encode(project_root);
        gate_p += ". Call ar-gemini-review__gemini_review for this gate. Return {status, summary} with decision.";
        let gate = agent(gate_p, #{
            label: gate_mode + ":" + unit_id,
            agent_type: "ar-gemini-reviewer",
            capability_mode: "all",
            output_schema: worker_schema,
        });
        if gate == () || gate.success == false {
            log(gate_mode + " failed for " + unit_id);
        }
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
let close_p = "Engine has no next unit for " + json_encode(project_root);
close_p += ". If close is done, stop the monitor you started. Return {status, summary}.";
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
