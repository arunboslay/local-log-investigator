# Watcher evaluation

Run on 2026-11-08 01:10. No GPU is used by any of these once trained.

Each cell: faults flagged / recordings, with the median seconds from the fault going live to the first flag on an
affected endpoint. A code or config fault goes live when the app restarts on the guilty commit. A flag before that,
or on an endpoint the fault doesn't touch, counts as a false alarm.

Rules were adjusted while looking at the incident recordings, so that table is optimistic. The dataset runs were not
used for that. Both models trained on normal traffic only, so every fault type is new to them.
"Rules, already running" first reads 20 minutes of normal traffic, as a watcher that has been up a while would. Plain
"rules" starts from nothing, so it may not have learned normal speed yet.

### Dataset runs (fairer test; short normal period before each fault)

| Fault type | Recordings | rules | rules, already running | forest | sequence | rules+sequence |
|---|---|---|---|---|---|---|
| N+1 queries | 2 | 2/2 (25 s) | 2/2 (18 s) | 0/2 | 0/2 | 2/2 (25 s) |
| bad config | 4 | 4/4 (0 s) | 4/4 (0 s) | 0/4 | 4/4 (0 s) | 4/4 (0 s) |
| database down | 2 | 2/2 (0 s) | 2/2 (0 s) | 0/2 | 2/2 (0 s) | 2/2 (0 s) |
| null pointer | 6 | 6/6 (11 s) | 6/6 (11 s) | 1/6 (0 s) | 6/6 (6 s) | 6/6 (6 s) |
| payment down | 2 | 2/2 (0 s) | 2/2 (0 s) | 0/2 | 2/2 (0 s) | 2/2 (0 s) |
| payment slow | 2 | 2/2 (1 s) | 2/2 (1 s) | 0/2 | 2/2 (1 s) | 2/2 (1 s) |
| pool exhausted | 2 | 2/2 (4 s) | 2/2 (4 s) | 2/2 (0 s) | 2/2 (5 s) | 2/2 (4 s) |
| silent save | 7 | 7/7 (0 s) | 7/7 (0 s) | 2/7 (0 s) | 7/7 (0 s) | 7/7 (0 s) |
| slow query | 4 | 0/4 | 2/4 (19 s) | 0/4 | 0/4 | 0/4 |
| **All** | 31 | 27/31 (0 s) | 29/31 (1 s) | 5/31 (0 s) | 25/31 (0 s) | 27/31 (0 s) |
| False alarms in these recordings | | 0 | 0 | 55 | 0 | 0 |

### Incident recordings (rules were adjusted on these)

| Fault type | Recordings | rules | rules, already running | forest | sequence | rules+sequence |
|---|---|---|---|---|---|---|
| N+1 queries | 1 | 1/1 (16 s) | 1/1 (21 s) | 0/1 | 0/1 | 1/1 (16 s) |
| bad config | 1 | 1/1 (0 s) | 1/1 (0 s) | 0/1 | 1/1 (0 s) | 1/1 (0 s) |
| database down | 1 | 1/1 (0 s) | 1/1 (0 s) | 1/1 (54 s) | 1/1 (0 s) | 1/1 (0 s) |
| memory leak | 1 | 0/1 | 0/1 | 0/1 | 0/1 | 0/1 |
| null pointer | 1 | 1/1 (9 s) | 1/1 (9 s) | 0/1 | 1/1 (9 s) | 1/1 (9 s) |
| payment down | 1 | 1/1 (0 s) | 1/1 (0 s) | 0/1 | 1/1 (0 s) | 1/1 (0 s) |
| payment slow | 1 | 1/1 (1 s) | 1/1 (1 s) | 0/1 | 1/1 (1 s) | 1/1 (1 s) |
| pool exhausted | 1 | 1/1 (5 s) | 1/1 (5 s) | 1/1 (0 s) | 1/1 (5 s) | 1/1 (5 s) |
| silent save | 1 | 1/1 (0 s) | 1/1 (0 s) | 0/1 | 1/1 (0 s) | 1/1 (0 s) |
| slow query | 1 | 1/1 (16 s) | 1/1 (19 s) | 0/1 | 0/1 | 1/1 (16 s) |
| traffic spike | 1 | 1/1 (8 s) | 0/1 | 1/1 (9 s) | 0/1 | 1/1 (8 s) |
| **All** | 11 | 10/11 (3 s) | 9/11 (1 s) | 3/11 (9 s) | 7/11 (0 s) | 10/11 (3 s) |
| False alarms in these recordings | | 0 | 0 | 32 | 0 | 0 |

### False alarms on clean traffic (30 minutes, varied load, never used for training or adjusting)

| Detector | Flags | Per hour |
|---|---|---|
| rules | 2 | 4.0 |
| rules, already running | 2 | 4.0 |
| forest | 13 | 25.7 |
| sequence | 1 | 2.0 |
| rules+sequence | 3 | 5.9 |

### Models and speed

- Training traffic: 61429 normal requests (75 minutes, varied load, harmless deploys).
- Sequence model: tiny LSTM, 27,028 weights, 20 log message types, top-2, trained in 1.6 s on cuda; 0.1 ms per request on one CPU thread.
- Isolation Forest: trained in 0.6 s; 4.75 ms per request on CPU.
- Rules: about 124,706 log lines per second on one CPU thread.

### What the numbers mean

- **Recommendation: keep the rules, drop both models.** The sequence model caught nothing the rules missed. The Isolation
  Forest caught far less and raised far more false alarms. Plain rules are cheaper and easier to explain.
- **Clean traffic:** all the rule flags, and the sequence model's flag, came from one real event: a single payment call that
  timed out (502 after 833 ms) during the clean run. I did not inject it, so these are not really false, but one
  blip still produced 2 alerts.
- **Slow queries need a baseline.** Started from nothing, the rules can't judge speed in the short dataset runs. Already
  running, they catch the slow order query, but still miss the slow product query (2 of 4).
- **The traffic rule is relative to recent load.** "Already running" misses the incident traffic spike, likely because its
  normal traffic went as high as 30 requests per second, so the spike wasn't 3 times normal.
- **The memory leak is not found.** It doesn't show in the logs.
- **The sequence model is close to a lookup table here:** this app's normal log sequences are nearly fixed, with 20 message
  types. A real app could differ, but these results give no reason to expect the model to win.
- **An earlier version of this table was wrong.** It showed the sequence model catching the memory leak, because old
  recordings masked stock counts as phone numbers. That is corrected above.
- All of this is on one small demo app that I wrote. The rules were written with these faults in mind.

### Live demo

App, traffic and watcher running, silent-save fault injected, investigator triggered automatically on the first flag
(gpt-oss-20b through llama.cpp, without the fine-tuned add-on). One run.

| Measure | Result |
|---|---|
| Flags in 90 s of normal traffic before the fault | 0 |
| Fault live to first flag | 0.3 s |
| Fault live to the missing-step flag ("returned 201 but 'customer saved' never happened") | 0.3 s |
| Flag to finished report | 37 s (20 s gathering evidence, about 6 s starting the model server, 11 s investigating) |
| Report: category, commit, file | all right |
| Report: line | wrong (38; the bug is on 41) |
| Report: description | partly wrong: says the phone field was left empty; the customer was not saved at all |
