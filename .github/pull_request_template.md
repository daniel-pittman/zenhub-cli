### This PR introduces the following changes
- Detail
- Detail
- Add more details as needed

### Steps to Review
1. From a terminal in the project root run `git checkout develop`
2. Run `git fetch`
3. Run `git pull`
4. Check out the branch under test via `git checkout <branch name here>`
5. Install the `zh` CLI and its dependencies per the [README](https://github.com/daniel-pittman/zenhub-cli#installation) (clone + symlink onto your PATH; ensure `gh`, `jq`, and `curl` are installed and `gh auth status` is authenticated)
6. Install the test dependencies and run the suite: `python -m pip install pytest numpy` then `python -m pytest tests/ -v` (the tests mock the network entirely, so no ZenHub or GitHub credentials are needed)
7. Exercise the affected `zh` subcommand(s) against a real workspace as described above
8. Add more steps as needed
