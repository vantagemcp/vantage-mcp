# Contributing to Vantage

Issues and pull requests are welcome. This is a small project, so the most useful contributions are the specific
ones.

**Good places to start**

- Install snippets for a client the README does not cover yet (check each one against that client's own docs).
- Example prompts and sample output, including for Gemini and Perplexity.
- README translations. Open an issue first naming the language so two people do not do the same one.
- Clearer tool descriptions: if an agent picked the wrong tool, say which and why.

**Before you open a pull request**

- Keep it small and focused. This repo is the server only; billing is not here, so changes to it cannot be
  accepted.
- Accepted changes are also applied to the maintainers' working copy, so a pull request that touches many files at
  once takes longer to land.
- Say what you ran. CI only checks that the package imports.
- Follow the rules in [AGENTS.md](AGENTS.md), especially: tools stay read-only, and metering runs before any paid
  call.

**Security issues:** email support@vantagemcp.dev instead of opening an issue (see [SECURITY.md](SECURITY.md)).

**Questions and results you want to share:** open a Discussion. If a check surprised you, say which keyword and
which engine, and include `checked_at` from the result: answers change run to run.
