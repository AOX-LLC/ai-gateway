# AI Gateway: a case study

{{ generated_notice }}

> Harborline Supply Co. and all data here are fictional. This is a portfolio project, not a client engagement.

## The problem

An AI assistant that reads text can be talked into misusing its tools. A support ticket can carry a hidden instruction to write where it should not, or to copy customer records out. Safe access needs authentication, authorization per tool, a person approving writes, and a defence against injected instructions. Each can be measured. This project builds them as a gateway and measures them.

## The design

AI Gateway sits between AI clients and Harborline's internal tools, which have no route out. Every request is authenticated with a bearer token first, and no setting turns that off. It then passes an ordered chain of layers, each set to enforce, monitor or off. The order is fixed in code.

- `scope`: the tools a client may see and call.
- `allowlist`: limits on argument values.
- `rate_limit`: a token bucket for each client.
- `schema`: arguments and results must fit the reviewed schema.
- `pinned_descriptions`: a changed tool description or schema is hidden and refused.
- `egress`: refuses a write that carries too many values the client read.
- `canary`: refuses a planted decoy value, even in base64 or hex.
- `classifier`: a small model judges free text for injected instructions.
- `approval`: a write waits for a person.

Approval is last, so a person approves exactly the call that runs. The gateway and the approvers use different database roles, so the gateway cannot resolve an approval. A write is audited before it is forwarded, and refused if the record cannot be written. Audit and approvals come from agent-core, a separate AOX library. Each request leaves a decision record that names the deciding layer and holds no arguments or results. A read-only dashboard shows them.

## How it was verified

The attacker is a script that does what a planted text says: a compliant model, one already talked into it. It runs {{ hostile_attacks }} hostile attacks under {{ columns }} columns: every layer on, every layer off, each layer off in turn, the layers that may be weakened only watching, the limits of the first release turned back on (separately, and all together), and an approver who rejects every write.

An independent oracle judges each attack, not the gateway's record: {{ oracle_databases }} by what landed in the databases, {{ oracle_ticket_diff }} by a diff of the ticketing data, {{ oracle_client_observation }} by what the client was handed, {{ oracle_lab_count }} by the lab upstream's count of the calls it ran. Each attack file states beforehand its purpose, success threshold and expected stopping layers. The scorecard reports where a run differed. {{ honest_runs }} honest runs make {{ honest_calls }} calls in each column, to measure false positives.

## Results

With every layer on, {{ all_on_succeeded }} of {{ hostile_attacks }} attacks succeed ({{ all_on_rate }}). With every layer off, {{ all_off_succeeded }} do. With the weakenable layers only watching, {{ monitor_succeeded }} do, and scope and approval still enforce. With every layer on, {{ all_on_honest_blocked }} of {{ honest_calls }} honest calls are blocked, and {{ monitor_honest_would_block }} would be when the layers only watch.

{{ layer_table }}

The first column counts the attacks that succeed only when that layer alone is off. A zero means another layer also stops them: redundancy. The scorecard's approver is a lab approver that approves every write, so a person's judgement is not measured, and `approval` stands at {{ approval_alone }}.

The `denying-approver` column rejects every write, so no attack that depends on a write can succeed. {{ denying_succeeded }} of {{ hostile_attacks }} attacks succeed there, but {{ denying_honest_blocked }} of {{ honest_calls }} honest calls are blocked ({{ denying_honest_rate }}): false positives. It bounds what a perfect reviewer could add. It is not a policy. Still succeeding: {{ denying_survivors }}. Reads never wait for an approver.

## The gaps

{{ gap_count }} attacks succeed with every layer on. They are known gaps, shown as successes. Undeclared successes: {{ undeclared_successes }}.

{{ gap_list }}

## Predicted against observed

{{ mismatch_list }}

A difference is listed, not corrected. A stop by the classifier rests on a recorded judgement of one string, not on coverage of a class of attack.

## What the scorecard does not show

- A script is not a model. How often a real model is talked into it is not measured.
- The classifier answers from recordings, not as a model meeting unseen text. A text with no recording counts as unclassified, never as clean. This run had {{ unclassified_total }}.
- The corpus is small. A rate here is a count of {{ hostile_attacks }} attacks, not a probability.
- Columns that switch off scope or approval run under a lab-only override. Latency is indicative only.

## What a production version would add

These are proposals, not shipped features. Each follows from a limit the repository states.

- Run the classifier live, with its spend limits set, and measure it.
- Replace bearer tokens with OAuth.
- Run several gateway processes, which needs shared rate-limit and egress state.
- Close the egress drip gap, and match encodings such as ROT13.
- Add an agent-core role that purges stored arguments but cannot decide an approval.
- Plan retention for larger volumes.
- Record a larger corpus, and red-team with a real model.

Repository: [github.com/AOX-LLC/ai-gateway](https://github.com/AOX-LLC/ai-gateway). The full scorecard is in [docs/scorecard.md](scorecard.md), the design in [docs/architecture.md](architecture.md) and client onboarding in [docs/integration.md](integration.md). The demo video script and clips are in the repository.
