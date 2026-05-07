# Using the recipe with non-Claude LLMs

`skill/migrate-bp.md` and `skill/inspect-bp.md` were authored for
Claude Code, but the body of each is plain English instructions. Other
LLM agents (GPT-4, Gemini, Llama, ...) can execute them with one
adjustment: the YAML frontmatter is Claude-specific.

## What is Claude-specific

The frontmatter at the top of each `.md`:

```yaml
---
description: "..."
argument-hint: "<...>"
allowed-tools: ["Bash", "Read", ...]
---
```

These keys are read by Claude Code's slash-command runtime. Other LLM
agents should **strip the frontmatter** (everything between the leading
`---` markers) before consuming the recipe, and instead inject the
appropriate system role themselves (see below).

The body of each `.md` (everything after the second `---`) is portable
markdown.

## Recommended system prompt for other agents

Use the following as the agent's system / instruction message before
loading the recipe body. Adjust tool naming to your agent framework.

```
You are an Unreal Engine Blueprint -> C++ migration agent. The user will
ask you to migrate or inspect a Blueprint. Follow the recipe markdown
that the user provides EXACTLY. The recipe encodes deterministic rules
(no LLM-side inference for variable defaults, type resolution, or
sanity checks). Stop at any "gate" and wait for the user's explicit
response before proceeding.

You have access to a shell/tool that runs `bpmigrate` (a Python CLI).
Pass commands to it via the shell. Read its JSON output and act on it.

You also have file read/write capability (for generating C++ headers
and source files).

Do not invent BP node behavior. If the recipe says to consult a
specific JSON field, consult exactly that field. If a value is missing
or unresolvable, use the "marked as ⚠ -- requires user input" path
described in the recipe.

When the recipe says "ask the user", actually wait for their response
before continuing.
```

## Tool surface mapping

The recipe assumes the agent can:

| Capability | Claude Code | OpenAI Assistants / GPT | Gemini | Llama / local |
|---|---|---|---|---|
| Run shell command + read stdout | `Bash` | `code_interpreter` (with shell), or function-calling tool | function-calling | function-calling, MCP, `bash`-style tool |
| Read a file | `Read` | function-calling `read_file` | function-calling | function-calling |
| Write a file | `Write` | function-calling `write_file` | function-calling | function-calling |
| Edit a file | `Edit` | function-calling `apply_diff` | function-calling | function-calling |

Wherever the recipe says `Run: <command>`, the agent executes that
command via its shell tool. Wherever it says `Generate file
<path>:` followed by code, the agent writes that file.

## A minimal harness

Pseudocode for a non-Claude agent harness:

```python
import subprocess

def run_recipe(recipe_path: str, user_args: str) -> None:
    body = strip_frontmatter(read_file(recipe_path))
    system = SYSTEM_PROMPT  # the prompt above
    msgs = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"{body}\n\nArguments: {user_args}"},
    ]
    while True:
        response = your_llm.chat(msgs, tools=[shell_tool, read_tool, write_tool, edit_tool])
        if response.is_final:
            return
        # Execute tool calls, append results, loop.
        ...
```

Replace `your_llm` with the SDK of choice. The recipe's
gate/stop-points are emitted by the model as plain text questions to
the user — your harness should pause and let the user answer.

## What still requires Claude or human-in-loop

- **Step 3 plan review**: any agent will draft the plan, but a human
  needs to approve it (especially the gate-b ⚠ variables and gate-d
  reparent decisions).
- **Step 5 manual editor work**: the agent describes what to do; the
  user does it in the UE editor.

These are not Claude-specific limitations.

## Verification status

- The recipe has not been run end-to-end against GPT-4 / Gemini /
  Llama. Portability rests on the markdown body being plain instructions
  and the CLI being LLM-agnostic. PRs with verified runs welcome.
