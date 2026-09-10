let meta = #{
    name: "ar-experiment-matrix",
    description: "Run an isomorphic AutoResearch experiment matrix in parallel under one project_root",
    when_to_use: "Independent seeds, ablations, or baselines of an already-coded experiment. /ar-experiment-matrix",
    phases: [
        #{ title: "Fan-out", detail: "one runner per item" },
        #{ title: "Synthesize", detail: "merge metrics into results/summary.md" },
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
    pause("verification", "Pass args.project_root and args.items (array of item ids).");
}
let project_root = args.project_root;
if project_root == () || project_root == "" {
    pause("verification", "Pass args.project_root.");
}
let items = args.items;
if items == () || type_of(items) != "array" || items.len() < 2 {
    pause("verification", "Pass args.items as an array of at least 2 item ids.");
}
let max_parallel = 16;
if args.max_parallel != () && args.max_parallel >= 2 && args.max_parallel <= 32 {
    max_parallel = args.max_parallel;
}
if items.len() > max_parallel {
    log("dropping items after the first " + max_parallel.to_string());
}
let stage = "pilot";
if args.stage != () && args.stage != "" {
    stage = args.stage;
}

let worker_schema = #{
    "type": "object",
    "required": ["item", "status", "summary"],
    "properties": #{
        "item": #{ "type": "string" },
        "status": #{ "type": "string" },
        "summary": #{ "type": "string" },
        "metrics": #{ "type": "string" },
        "log_path": #{ "type": "string" },
    },
};

phase("Fan-out");
let jobs = [];
let i = 0;
for item in items {
    if i >= max_parallel { break; }
    let item_s = trimmed(item);
    if item_s == "" {
        i += 1;
    } else {
        let p = "Run one independent AutoResearch matrix item. Use run_terminal_command";
        p += " and <project_root>/.venv/bin/python. Read the code first. Set CUDA_VISIBLE_DEVICES";
        p += " to the cards this item will use. Writes stay under project_root. No sudo.";
        p += " project_root=" + json_encode(project_root);
        p += " item=" + json_encode(item_s);
        p += " stage=" + json_encode(stage);
        p += " Prefer a launcher --only <item> when one exists. Write metrics to";
        p += " results/runs/<item>/ and append a milestone to results/run.log.";
        p += " Return {item, status, summary, metrics, log_path}.";
        jobs.push(#{
            prompt: p,
            label: "matrix:" + item_s,
            agent_type: "ar-runner",
            capability_mode: "all",
            output_schema: worker_schema,
        });
        i += 1;
    }
}
if jobs.len() < 2 {
    pause("verification", "Need at least two non-empty items after filtering.");
}
let b = budget();
if jobs.len() + 2 > b.remaining {
    pause("infra", "Matrix panel does not fit the remaining agent budget.");
}
let results = parallel(jobs);

phase("Synthesize");
let rows = [];
let idx = 0;
for r in results {
    if r != () && r.success && r.output.item != () {
        rows.push(r.output);
    } else {
        log("matrix slot " + idx.to_string() + " failed or empty");
    }
    idx += 1;
}
let syn = "Synthesize AutoResearch matrix results. Read artifacts under";
syn += " project_root=" + json_encode(project_root);
syn += " Rows JSON: " + json_encode(rows);
syn += " Write results/summary.md with a table of item/status/metrics.";
syn += " Do not claim success without numbers from disk. Return {status, summary}.";
let synth_schema = #{
    "type": "object",
    "required": ["status", "summary"],
    "properties": #{
        "status": #{ "type": "string" },
        "summary": #{ "type": "string" },
        "summary_path": #{ "type": "string" },
    },
};
let syn_r = agent(syn, #{
    label: "matrix-synth",
    capability_mode: "all",
    output_schema: synth_schema,
});
let summary = "matrix ran " + rows.len().to_string() + " item(s)";
if syn_r != () && syn_r.success && syn_r.output.summary != () {
    summary = syn_r.output.summary;
}
complete(#{
    summary: summary,
    project_root: project_root,
    items_ok: rows.len(),
    items_attempted: jobs.len(),
});
