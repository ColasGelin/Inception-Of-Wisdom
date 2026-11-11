# Inception-of-Wisdom (IoW)

A local autonomous agent that watches a running service, notices it is broken, finds the code
that probably caused it, asks a small local LLM for a fix, applies and commits it, restarts the
service and checks the symptom is gone, or rolls everything back through Git.

> Work in progress: full documentation lands with Phase 5.

```bash
make up PART=p1      # Observer only
make bug BUG=syntax PART=p1
open http://127.0.0.1:8042
make down PART=p1
```
