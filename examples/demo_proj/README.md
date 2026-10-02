# Synergo demo project

A tiny tree for showing lanes and conflicts on the dashboard. Open it with two
agents (two machines, or two checkouts with different `--name`s), each running
the Synergo daemon:

```bash
synergo hook install claude-code
synergo join <ROOM> --name hossein --server wss://<your-relay>/ws
```

Then have both agents edit `src/hello.py` or `src/utils.py`: the second agent to
touch a file is warned or blocked before the edit lands, and both lanes show it.
